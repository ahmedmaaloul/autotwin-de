"""On-disk response cache — the middle link of the ``live → cache → fixture`` chain (§4).

Without this module the fallback chain is a slogan. With it, a Bundesnetzagentur download that
succeeded six hours ago keeps the charging map working through an outage, and the UI says so
because the provider returns ``ProviderMode.cache``.

Design constraints, and how they are met:

* **Must never break a request.** A read-only ``data/`` directory (a hardened container, a
  CI sandbox) is a normal condition, not an error. Every write is wrapped: on ``OSError`` the
  cache logs once and behaves as a permanent miss, so the provider simply falls through to its
  fixture.
* **Must never serve a half-written file.** Writes go to a temporary file in the same
  directory and are then moved into place with :meth:`pathlib.Path.replace`, which is atomic
  on POSIX and Windows alike. A crash mid-write leaves the previous entry intact.
* **Must be inspectable.** Keys are hashed, but the namespace becomes a real directory and
  JSON entries keep a ``.json`` suffix, so ``data/raw/bnetza/<hash>.json`` can be opened in an
  editor while debugging an adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from autotwin_core.config import get_settings
from autotwin_core.logging import get_logger

__all__ = ["FileCache"]

_logger = get_logger(__name__)

_SAFE_NAMESPACE_CHARS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
"""Namespaces become directory names, so they are restricted to an unambiguous alphabet."""

JSON_SUFFIX: Final[str] = ".json"
"""Suffix for entries written by :meth:`FileCache.set_json`."""

BYTES_SUFFIX: Final[str] = ".bin"
"""Suffix for entries written by :meth:`FileCache.set_bytes`."""


class FileCache:
    """A namespaced, TTL-bounded cache of provider responses on the local filesystem.

    Entries are addressed by ``(namespace, key)``. The namespace is the adapter
    (``bnetza``, ``dwd``, ``osrm``); the key is whatever makes the request unique — a URL, a
    bounding box, a coordinate pair. The key is hashed with SHA-256 because raw keys are URLs
    containing slashes, query strings and, for routing, hundreds of characters of coordinates.

    Instances are cheap and hold no state beyond their configuration, so a provider may create
    one per call. Concurrency is handled by the filesystem: readers see either the old file or
    the new one, never a partial write. Two processes writing the same key simultaneously is
    benign — last writer wins, and both wrote the same upstream response.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        """Configure the cache.

        Args:
            root: Directory to store entries under. Defaults to the configured cache
                directory, so callers normally pass nothing and tests pass a ``tmp_path``.
            ttl_seconds: Entry lifetime. Defaults to ``AUTOTWIN_CACHE_TTL_SECONDS``. A value
                of ``0`` or less disables expiry — entries then live until deleted, which is
                what fixture-mode snapshots want.
        """
        settings = get_settings()
        self._root = Path(root) if root is not None else Path(settings.cache_dir)
        self._ttl_seconds = (
            int(ttl_seconds) if ttl_seconds is not None else int(settings.cache_ttl_seconds)
        )
        self._write_failure_logged = False

    @property
    def root(self) -> Path:
        """Directory entries are stored under."""
        return self._root

    @property
    def ttl_seconds(self) -> int:
        """Default entry lifetime in seconds; ``<= 0`` means entries never expire."""
        return self._ttl_seconds

    def path_for(self, namespace: str, key: str, *, suffix: str = BYTES_SUFFIX) -> Path:
        """Absolute path of the entry for ``(namespace, key)``.

        Exposed because adapters occasionally need to hand a real file to a parser that only
        reads paths (large CSV and ZIP downloads), and because a failing test is far easier to
        diagnose when it can print where it expected the entry to be.
        """
        digest = hashlib.sha256(f"{namespace}\x00{key}".encode()).hexdigest()
        return self._root / _sanitise_namespace(namespace) / f"{digest}{suffix}"

    def age_seconds(self, namespace: str, key: str, *, suffix: str = BYTES_SUFFIX) -> float | None:
        """Age of the entry in seconds, or ``None`` when it does not exist.

        This is what a provider reports as ``ProviderResult.fetched_at`` when it serves from
        cache, so the UI can say how old the data is rather than implying it is fresh.
        """
        path = self.path_for(namespace, key, suffix=suffix)
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return None
        return max(0.0, time.time() - modified_at)

    def get_bytes(
        self,
        namespace: str,
        key: str,
        *,
        ttl_seconds: int | None = None,
        allow_stale: bool = False,
    ) -> bytes | None:
        """Read raw bytes, or ``None`` on a miss or an expired entry.

        Args:
            namespace: Adapter namespace.
            key: Request key.
            ttl_seconds: Override the instance TTL for this read.
            allow_stale: Return an expired entry anyway. Providers set this on their *last*
                attempt before dropping to a fixture: six-hour-old real charging stations beat
                a bundled snapshot from last month, as long as the mode still says ``cache``.
        """
        path = self.path_for(namespace, key, suffix=BYTES_SUFFIX)
        if not self._is_usable(path, ttl_seconds, allow_stale=allow_stale):
            return None
        try:
            return path.read_bytes()
        except OSError as error:
            _logger.warning(f"Cache read failed for {namespace}/{key}: {error}")
            return None

    def set_bytes(self, namespace: str, key: str, payload: bytes) -> bool:
        """Write raw bytes atomically; returns whether the entry was stored.

        A ``False`` return is not an error the caller must handle — it means the cache layer
        is unavailable (read-only directory, full disk) and the chain simply loses its middle
        link for this run.
        """
        return self._write(self.path_for(namespace, key, suffix=BYTES_SUFFIX), payload)

    def get_json(
        self,
        namespace: str,
        key: str,
        *,
        ttl_seconds: int | None = None,
        allow_stale: bool = False,
    ) -> Any | None:
        """Read a JSON entry, or ``None`` on a miss, an expired entry or corrupt content.

        A corrupt entry is treated as a miss and deleted: it can only have come from a disk
        error or a killed process, and keeping it would make every subsequent run fail the
        same way.
        """
        path = self.path_for(namespace, key, suffix=JSON_SUFFIX)
        if not self._is_usable(path, ttl_seconds, allow_stale=allow_stale):
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            _logger.warning(f"Cache read failed for {namespace}/{key}: {error}")
            return None
        except json.JSONDecodeError as error:
            _logger.warning(f"Discarding corrupt cache entry {path}: {error}")
            self._unlink(path)
            return None

    def set_json(self, namespace: str, key: str, payload: Any) -> bool:
        """Serialise ``payload`` to JSON and write it atomically.

        Non-serialisable payloads raise :class:`TypeError` rather than being silently dropped:
        that is a programming error in the adapter, not a runtime condition of the cache.
        """
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._write(self.path_for(namespace, key, suffix=JSON_SUFFIX), encoded)

    def invalidate(self, namespace: str, key: str) -> None:
        """Remove both representations of an entry, if present."""
        self._unlink(self.path_for(namespace, key, suffix=BYTES_SUFFIX))
        self._unlink(self.path_for(namespace, key, suffix=JSON_SUFFIX))

    def clear(self, namespace: str | None = None) -> int:
        """Delete every entry in a namespace, or in the whole cache; returns the count."""
        target = self._root / _sanitise_namespace(namespace) if namespace else self._root
        if not target.is_dir():
            return 0
        removed = 0
        for path in sorted(target.rglob("*")):
            if path.is_file() and self._unlink(path):
                removed += 1
        return removed

    def _is_usable(self, path: Path, ttl_seconds: int | None, *, allow_stale: bool) -> bool:
        """Whether an entry exists and is fresh enough to serve."""
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return False
        if allow_stale:
            return True
        ttl = self._ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl <= 0:
            return True
        return (time.time() - modified_at) <= ttl

    def _write(self, path: Path, payload: bytes) -> bool:
        """Write ``payload`` to ``path`` via a temporary file and an atomic replace.

        The temporary file is created in the destination directory so that
        :meth:`pathlib.Path.replace` stays a same-filesystem rename, which is the only form
        that is atomic; a temporary in ``/tmp`` would degrade into a copy across mounts.
        """
        temporary = path.parent / f".{path.name}.{uuid4().hex[:8]}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with temporary.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(path)
            except OSError:
                self._unlink(temporary)
                raise
        except OSError as error:
            self._log_write_failure(path, error)
            return False
        return True

    def _log_write_failure(self, path: Path, error: OSError) -> None:
        """Log the first write failure only — a read-only cache would otherwise flood the log."""
        if self._write_failure_logged:
            return
        self._write_failure_logged = True
        _logger.warning(
            f"Cache is not writable at {path.parent} ({error}); "
            "continuing without caching for this process"
        )

    @staticmethod
    def _unlink(path: Path) -> bool:
        """Delete a file, reporting whether it was actually removed."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True


def _sanitise_namespace(namespace: str | None) -> str:
    """Fold a namespace into a safe single directory name.

    Anything outside ``[a-z0-9-_]`` becomes ``_``, which makes path traversal through a
    namespace impossible even if one is ever derived from user input.
    """
    if not namespace:
        return "default"
    folded = namespace.strip().casefold()
    cleaned = "".join(char if char in _SAFE_NAMESPACE_CHARS else "_" for char in folded)
    return cleaned.strip("_") or "default"
