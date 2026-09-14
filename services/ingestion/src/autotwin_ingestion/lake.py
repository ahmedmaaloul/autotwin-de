"""The on-disk data zone: ``raw → bronze → silver → gold`` (ADR 007).

The operational database answers *what is true now*. The lake answers two questions it
deliberately cannot:

* **"What exactly did the source send us, and can we prove it?"** — the raw zone keeps the
  downloaded bytes next to a small JSON sidecar carrying the URL, the fetch timestamp, the
  SHA-256 and the byte count. Re-running an ingestion against an archived file reproduces a
  past run exactly, which is what makes a data-quality claim auditable rather than asserted.
* **"Which rows were thrown away, and why?"** — the bronze zone holds *every parsed record*,
  including the ones the quality rules rejected, with the rule that fired. PostgreSQL never
  sees those rows (BUILD_SPEC §6 says reject, do not write), so without bronze the rejected
  population would be invisible to analysis. That is the single most useful thing this zone
  does: ``violations`` on an ingestion run says *how many*, bronze says *which ones*.

Zone semantics, in the medallion convention:

============  ==========================================================================
zone          contents
============  ==========================================================================
``raw``       byte-identical source documents plus ``<file>.meta.json`` sidecars, and
              normalised run snapshots (clearly flagged ``kind="normalised_snapshot"``,
              never confusable with source bytes).
``bronze``    every parsed record, one row per source record, with ``quality_status``
              (``accepted`` / ``rejected`` / ``duplicate``) and ``quality_rule``.
``silver``    accepted records only, typed and analysis-ready — the join-friendly table
              a notebook actually wants.
``gold``      business-level marts. Produced by dbt into the ``analytics`` schema of
              PostgreSQL (BUILD_SPEC §16); this zone is where an export of one lands.
              Empty until something exports into it — deliberately, not by oversight.
============  ==========================================================================

Datasets are partitioned by ingestion date (``dt=YYYY-MM-DD``) and every write replaces the
parts of the partition it writes into, so re-running a pipeline on the same day is idempotent
instead of doubling the row count — the same property the database upserts give us.

Polars writes, DuckDB reads: :meth:`DataLake.query` registers every dataset as a view and runs
plain SQL over the Parquet files, which is what the research notebook and any ad-hoc analysis
use. No engine is imported until this module is.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self
from uuid import uuid4

import duckdb
import polars as pl

from autotwin_contracts import utc_now
from autotwin_core.config import Settings, get_settings
from autotwin_core.logging import get_logger

__all__ = [
    "DEFAULT_PART_ROWS",
    "PARQUET_COMPRESSION",
    "SIDECAR_SUFFIX",
    "DataLake",
    "LakeDataset",
    "LakeZone",
    "ParquetPartWriter",
    "RawArtifact",
    "default_lake",
    "sha256_of",
]

_logger = get_logger(__name__)

ParquetCompression = Literal["lz4", "uncompressed", "snappy", "gzip", "brotli", "zstd"]
"""Codecs Polars accepts for ``write_parquet``; spelled out so the constant below type-checks."""

SIDECAR_SUFFIX: Final[str] = ".meta.json"
"""Suffix of the provenance sidecar written next to every raw document."""

PARQUET_COMPRESSION: Final[ParquetCompression] = "zstd"
"""Zstandard: roughly half the size of snappy on this data and read-speed-neutral for DuckDB."""

DEFAULT_PART_ROWS: Final[int] = 50_000
"""Rows per Parquet part.

Chosen so the 117 000-row Ladesäulenregister becomes three files rather than sixty: large
enough for row-group statistics to prune usefully, small enough that a part is buffered in
memory without a spike.
"""

_TEMP_PREFIX: Final[str] = "."
"""Prefix of in-progress files, so a glob for ``*.parquet`` never picks up a partial write."""

_PARTITION_KEY: Final[str] = "dt"
"""Hive partition key. Ingestion date, because that is how every question about the lake is
scoped: *what did the register look like on the 14th?*"""


class LakeZone(StrEnum):
    """The four zones of the medallion layout, in refinement order."""

    raw = "raw"
    """Byte-identical source documents and their sidecars."""

    bronze = "bronze"
    """Every parsed record, accepted or not, exactly as the adapter produced it."""

    silver = "silver"
    """Accepted records only, typed and analysis-ready."""

    gold = "gold"
    """Business marts; produced by dbt (BUILD_SPEC §16), exported here on demand."""


@dataclass(frozen=True, slots=True)
class RawArtifact:
    """A source document in the raw zone, together with its sidecar facts.

    Frozen, and every field is a statement that can be checked against the file on disk. The
    SHA-256 is what makes "this ingestion run read *that* file" verifiable months later, and
    it is the value written to ``data_ingestion_runs.source_file_sha256``.
    """

    path: Path
    """Absolute path of the document."""

    sidecar_path: Path
    """Absolute path of the ``.meta.json`` written beside it."""

    sha256: str
    """Hex digest of the document's bytes."""

    size_bytes: int
    """Size of the document in bytes."""

    source_url: str | None
    """URL the bytes came from, when the pipeline knows it."""

    fetched_at: datetime
    """When the bytes were obtained (UTC) — not when the sidecar was written."""

    content_type: str | None
    """Media type as served, e.g. ``text/csv``."""

    kind: str
    """``source_download`` for upstream bytes, ``normalised_snapshot`` for a run snapshot."""

    def as_dict(self) -> dict[str, Any]:
        """Render the sidecar document."""
        return {
            "kind": self.kind,
            "file": self.path.name,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(),
            "sha256": self.sha256,
            "bytes": self.size_bytes,
            "content_type": self.content_type,
        }


@dataclass(frozen=True, slots=True)
class LakeDataset:
    """One Parquet dataset in one zone, as :meth:`DataLake.describe` reports it."""

    zone: LakeZone
    """Zone the dataset lives in."""

    name: str
    """Dataset name — also the DuckDB view name once prefixed with the zone."""

    partitions: tuple[str, ...]
    """Partition values present, e.g. ``("2026-09-14",)``, newest last."""

    files: int
    """Number of Parquet parts across all partitions."""

    size_bytes: int
    """Total size of those parts."""

    @property
    def view_name(self) -> str:
        """Name this dataset is registered under in :meth:`DataLake.query`."""
        return f"{self.zone.value}_{self.name}"


class DataLake:
    """Reads and writes the ``data/`` zone.

    Cheap to construct and holds no open handles, so a pipeline creates one per run. All
    paths are resolved at construction: a pipeline started by Airflow has a different working
    directory from one started by ``make``, and a relative ``./data`` would quietly produce
    two lakes.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path | str | None = None, *, settings: Settings | None = None) -> None:
        """Configure the lake.

        Args:
            root: Lake root. Defaults to ``AUTOTWIN_DATA_DIR``.
            settings: Configuration to read the data directory from when ``root`` is omitted.
        """
        base = Path(root) if root is not None else Path((settings or get_settings()).data_dir)
        self._root = base.expanduser().resolve()

    @property
    def root(self) -> Path:
        """Absolute lake root."""
        return self._root

    def zone_dir(self, zone: LakeZone) -> Path:
        """Directory of one zone, created if missing."""
        path = self._root / zone.value
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dataset_dir(self, zone: LakeZone, dataset: str) -> Path:
        """Directory of one dataset within a zone, created if missing."""
        path = self.zone_dir(zone) / _safe_name(dataset)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ raw zone

    def write_raw(
        self,
        payload: bytes,
        *,
        source: str,
        filename: str,
        source_url: str | None,
        fetched_at: datetime | None = None,
        content_type: str | None = None,
        kind: str = "source_download",
    ) -> RawArtifact:
        """Write source bytes into ``raw/<source>/`` and describe them in a sidecar.

        The write is atomic (temporary file plus ``replace``) because a half-written 55 MB CSV
        that *looks* complete is worse than no file: the next run would parse it, reject most
        of it and report a data-quality collapse that never happened upstream.
        """
        directory = self.zone_dir(LakeZone.raw) / _safe_name(source)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        _atomic_write(target, payload)
        return self._write_sidecar(
            target,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            source_url=source_url,
            fetched_at=fetched_at or utc_now(),
            content_type=content_type,
            kind=kind,
        )

    def write_snapshot(
        self,
        payload: Any,
        *,
        source: str,
        filename: str,
        source_url: str | None,
        fetched_at: datetime | None = None,
    ) -> RawArtifact:
        """Write a *normalised* run snapshot as JSON, flagged as such in its sidecar.

        Used by the adapters whose HTTP layer never hands the pipeline raw bytes (the Autobahn
        and DWD adapters fan out over dozens of requests and return parsed records). The
        snapshot is what the pipeline actually saw, which is enough to replay a run offline —
        but its sidecar says ``normalised_snapshot`` so it can never be mistaken for the
        upstream document.
        """
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.write_raw(
            encoded,
            source=source,
            filename=filename,
            source_url=source_url,
            fetched_at=fetched_at,
            content_type="application/json",
            kind="normalised_snapshot",
        )

    def adopt_raw(
        self,
        path: Path,
        *,
        source: str,
        source_url: str | None,
        fetched_at: datetime | None = None,
        content_type: str | None = None,
    ) -> RawArtifact:
        """Describe a file that is already on disk, copying it into the raw zone if needed.

        The Bundesnetzagentur adapter archives its download itself (it must: 55 MB should pass
        through memory once, not twice). When the file already lives under ``raw/`` only the
        sidecar is written; a file from anywhere else — a bundled fixture, an operator's
        manual download — is copied in first, so the raw zone is always a complete record of
        what a run read.

        Raises:
            FileNotFoundError: ``path`` does not exist.
        """
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            msg = f"cannot adopt {resolved}: not a file"
            raise FileNotFoundError(msg)
        raw_root = self.zone_dir(LakeZone.raw)
        if raw_root not in resolved.parents:
            directory = raw_root / _safe_name(source)
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / resolved.name
            if target != resolved:
                shutil.copyfile(resolved, target)
            resolved = target
        return self._write_sidecar(
            resolved,
            sha256=sha256_of(resolved),
            size_bytes=resolved.stat().st_size,
            source_url=source_url,
            fetched_at=fetched_at or utc_now(),
            content_type=content_type,
            kind="source_download",
        )

    def read_sidecar(self, path: Path) -> dict[str, Any] | None:
        """Read the sidecar of a raw document, or ``None`` when it has none."""
        sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
        if not sidecar.is_file():
            return None
        try:
            loaded: Any = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _logger.warning(f"Unreadable raw sidecar {sidecar}: {error}")
            return None
        return loaded if isinstance(loaded, dict) else None

    def _write_sidecar(
        self,
        path: Path,
        *,
        sha256: str,
        size_bytes: int,
        source_url: str | None,
        fetched_at: datetime,
        content_type: str | None,
        kind: str,
    ) -> RawArtifact:
        """Write ``<file>.meta.json`` and return the artefact it describes."""
        artifact = RawArtifact(
            path=path,
            sidecar_path=path.with_name(path.name + SIDECAR_SUFFIX),
            sha256=sha256,
            size_bytes=size_bytes,
            source_url=source_url,
            fetched_at=fetched_at,
            content_type=content_type,
            kind=kind,
        )
        _atomic_write(
            artifact.sidecar_path,
            json.dumps(artifact.as_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _logger.debug(
            "lake.raw.written",
            path=str(path),
            bytes=size_bytes,
            sha256=sha256[:16],
            kind=kind,
        )
        return artifact

    # ------------------------------------------------------------------ parquet zones

    def write_table(
        self,
        rows: Sequence[Mapping[str, Any]] | pl.DataFrame,
        *,
        zone: LakeZone,
        dataset: str,
        partition: date | str | None = None,
        schema: Mapping[str, Any] | None = None,
    ) -> Path | None:
        """Write one Parquet part, replacing anything already in that partition.

        Returns the part's path, or ``None`` for an empty input — an empty result is written
        as *nothing* rather than as a zero-row file, so ``describe()`` showing a dataset always
        means the dataset has data.
        """
        writer = ParquetPartWriter(
            directory=self.partition_dir(zone, dataset, partition),
            schema=schema,
        )
        writer.extend(rows if isinstance(rows, pl.DataFrame) else list(rows))
        writer.close()
        return writer.parts[0] if writer.parts else None

    def partition_dir(
        self,
        zone: LakeZone,
        dataset: str,
        partition: date | str | None = None,
    ) -> Path:
        """Directory of one Hive partition, e.g. ``bronze/charging_stations/dt=2026-09-14``."""
        value = partition.isoformat() if isinstance(partition, date) else partition
        value = value or utc_now().date().isoformat()
        return self.dataset_dir(zone, dataset) / f"{_PARTITION_KEY}={_safe_name(value)}"

    def scan(self, zone: LakeZone, dataset: str) -> pl.LazyFrame:
        """Lazily scan every part of a dataset across all partitions.

        Raises:
            FileNotFoundError: the dataset holds no Parquet parts.
        """
        parts = self.parts(zone, dataset)
        if not parts:
            msg = f"no Parquet parts under {self.dataset_dir(zone, dataset)}"
            raise FileNotFoundError(msg)
        return pl.scan_parquet(parts)

    def read(self, zone: LakeZone, dataset: str) -> pl.DataFrame:
        """Read a whole dataset into memory. Convenience over :meth:`scan` for small tables."""
        return self.scan(zone, dataset).collect()

    def parts(self, zone: LakeZone, dataset: str) -> list[Path]:
        """Every Parquet part of a dataset, sorted so reads are deterministic."""
        directory = self.dataset_dir(zone, dataset)
        return sorted(path for path in directory.rglob("*.parquet") if path.is_file())

    def datasets(self, zone: LakeZone) -> list[str]:
        """Names of the datasets in a zone that actually hold parts."""
        root = self.zone_dir(zone)
        names = [
            child.name
            for child in sorted(root.iterdir())
            if child.is_dir() and any(child.rglob("*.parquet"))
        ]
        return names

    def describe(self) -> list[LakeDataset]:
        """Inventory of every Parquet dataset in the lake — what ``quality report`` prints."""
        found: list[LakeDataset] = []
        for zone in (LakeZone.bronze, LakeZone.silver, LakeZone.gold):
            for name in self.datasets(zone):
                parts = self.parts(zone, name)
                partitions = sorted(
                    {
                        part.parent.name.removeprefix(f"{_PARTITION_KEY}=")
                        for part in parts
                        if part.parent.name.startswith(f"{_PARTITION_KEY}=")
                    }
                )
                found.append(
                    LakeDataset(
                        zone=zone,
                        name=name,
                        partitions=tuple(partitions),
                        files=len(parts),
                        size_bytes=sum(part.stat().st_size for part in parts),
                    )
                )
        return found

    # ------------------------------------------------------------------ duckdb

    def query(self, sql: str, *, parameters: Sequence[Any] | None = None) -> pl.DataFrame:
        """Run analytical SQL over the Parquet zones and return the result as a DataFrame.

        Every dataset is registered as a view named ``<zone>_<dataset>``, so the notebook
        writes ordinary SQL::

            lake.query("SELECT bundesland, count(*) FROM silver_charging_stations GROUP BY 1")

        The connection is in-memory and lives for the length of the call: DuckDB reads the
        Parquet files directly, so there is no database file to keep in sync with the lake and
        no state that can go stale between calls.
        """
        connection = duckdb.connect(database=":memory:")
        try:
            for view, glob in self._view_globs().items():
                # Built through the relational API rather than a CREATE VIEW string: the view
                # name is derived from a directory name, and composing DDL out of filesystem
                # input is exactly the shape that turns a stray character into an injection.
                connection.read_parquet(glob, hive_partitioning=True).create_view(
                    view,
                    replace=True,
                )
            relation = connection.execute(sql, list(parameters) if parameters else None)
            frame = relation.pl()
        finally:
            connection.close()
        return frame

    def views(self) -> list[str]:
        """View names :meth:`query` exposes — useful in an error message or a notebook cell."""
        return sorted(self._view_globs())

    def _view_globs(self) -> dict[str, str]:
        """Map every non-empty dataset to the glob DuckDB should read it through."""
        globs: dict[str, str] = {}
        for zone in (LakeZone.bronze, LakeZone.silver, LakeZone.gold):
            for name in self.datasets(zone):
                pattern = self.dataset_dir(zone, name) / "**" / "*.parquet"
                globs[f"{zone.value}_{name}"] = str(pattern)
        return globs


class ParquetPartWriter:
    """Buffers rows and flushes them as numbered Parquet parts into one partition.

    Exists so that a 117 000-row ingestion never holds 117 000 dictionaries *and* an Arrow
    table in memory at once: rows accumulate to :attr:`part_rows`, become a part, and the
    buffer is dropped.
    """

    __slots__ = (
        "_buffer",
        "_cleared",
        "_directory",
        "_parts",
        "_rows_flushed",
        "_schema",
        "part_rows",
    )

    def __init__(
        self,
        *,
        directory: Path,
        part_rows: int = DEFAULT_PART_ROWS,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        """Configure the writer.

        Args:
            directory: Partition directory to write parts into.
            part_rows: Rows per part.
            schema: Optional Polars schema. Worth passing for columns that are all-``None`` in
                the first part, which Polars would otherwise infer as ``Null`` and then fail to
                concatenate with a later part that has values.

        Raises:
            ValueError: ``part_rows`` is not positive.
        """
        if part_rows < 1:
            msg = f"part_rows must be at least 1, got {part_rows!r}"
            raise ValueError(msg)
        self._directory = directory
        self.part_rows = part_rows
        self._schema = dict(schema) if schema else None
        self._buffer: list[Mapping[str, Any]] = []
        self._parts: list[Path] = []
        self._cleared = False
        self._rows_flushed = 0

    @property
    def parts(self) -> list[Path]:
        """Paths of the parts written so far."""
        return list(self._parts)

    @property
    def rows_written(self) -> int:
        """Rows flushed to disk so far, excluding anything still buffered."""
        return self._rows_flushed

    def add(self, row: Mapping[str, Any]) -> None:
        """Buffer one row, flushing a part when the buffer is full."""
        self._buffer.append(row)
        if len(self._buffer) >= self.part_rows:
            self._flush()

    def extend(self, rows: Sequence[Mapping[str, Any]] | pl.DataFrame) -> None:
        """Buffer many rows at once."""
        if isinstance(rows, pl.DataFrame):
            self._write_frame(rows)
            return
        for row in rows:
            self.add(row)

    def close(self) -> None:
        """Flush whatever is buffered. Idempotent."""
        self._flush()

    def __enter__(self) -> Self:
        """Support ``with``; the writer is already configured."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Flush on the way out, including on an exception — partial data still has evidence."""
        self.close()

    def _flush(self) -> None:
        """Turn the buffer into a Parquet part."""
        if not self._buffer:
            return
        frame = pl.DataFrame(self._buffer, schema=self._schema, strict=False)
        self._buffer = []
        self._write_frame(frame)

    def _write_frame(self, frame: pl.DataFrame) -> None:
        """Write one frame as the next part, clearing stale parts on the first write."""
        if frame.is_empty():
            return
        self._prepare_directory()
        target = self._directory / f"part-{len(self._parts):04d}.parquet"
        temporary = self._directory / f"{_TEMP_PREFIX}{target.name}.{uuid4().hex[:8]}.tmp"
        frame.write_parquet(temporary, compression=PARQUET_COMPRESSION)
        temporary.replace(target)
        self._parts.append(target)
        self._rows_flushed += frame.height

    def _prepare_directory(self) -> None:
        """Create the partition directory and, once per writer, drop the previous parts."""
        self._directory.mkdir(parents=True, exist_ok=True)
        if self._cleared:
            return
        for stale in self._directory.glob("*.parquet"):
            stale.unlink(missing_ok=True)
        self._cleared = True


def default_lake() -> DataLake:
    """The lake rooted at the configured ``AUTOTWIN_DATA_DIR``."""
    return DataLake()


def sha256_of(path: Path, *, chunk_bytes: int = 1 << 20) -> str:
    """Hex SHA-256 of a file, read in chunks so a 55 MB CSV never becomes a 55 MB ``bytes``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` via a temporary file, flushed and fsynced."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f"{_TEMP_PREFIX}{target.name}.{uuid4().hex[:8]}.tmp"
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)


def _safe_name(value: str) -> str:
    """Fold a name into something safe as a single path segment.

    Dataset and partition names come from enum values and ISO dates today, but a notebook may
    pass anything; a separator sneaking through would write outside the zone.
    """
    cleaned = "".join(char if char.isalnum() or char in "-_=." else "_" for char in value.strip())
    return cleaned.strip("._") or "unnamed"
