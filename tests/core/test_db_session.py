"""Lazy engine and session-factory construction in :mod:`autotwin_core.db.session`.

These tests guard one property: the *first* database call a process makes must return, whichever
entry point it happens to be. That is not a given — the singletons are guarded by a single
module-level lock, and :func:`get_sessionmaker` builds the engine while holding it, so
``get_engine()`` re-enters the lock on the same thread. With a non-reentrant ``threading.Lock``
that deadlocked silently: no error, no log line, no timeout, just a process parked forever on its
first query (observed 2026-09-14 against a running PostGIS container).

Each case therefore runs in a **fresh interpreter**. The singletons are process-wide, so once any
earlier test has built the engine the re-entrant branch never runs again and an in-process test
would pass against the broken lock. Nothing here connects to a database: ``create_async_engine``
only parses the DSN, and a session connects lazily, so these run in the default suite rather than
behind ``@pytest.mark.integration`` — a deadlock nobody can run is a deadlock nobody catches.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_TIMEOUT_S = 30.0
"""Generous on purpose. The failure this guards against is an *infinite* hang, so the bound only
has to beat a cold import (~0.6s here); a tight one would turn a slow CI box into a flake."""

_REACQUIRE_TIMEOUT_S = 5.0
"""How long :func:`test_lock_is_reentrant` waits before calling a re-acquire hopeless. Uncontended
in a single-threaded test, so any wait at all already means the lock is not reentrant."""


_PROLOGUE = f"""
import faulthandler
# Print where every thread is parked before we are killed, so a regression names its own line.
faulthandler.dump_traceback_later({_TIMEOUT_S / 2}, exit=True)
"""

_FIRST_CALLS = {
    "get_sessionmaker": """
        from autotwin_core.db.session import get_sessionmaker

        assert get_sessionmaker() is not None
    """,
    "session_scope": """
        import asyncio

        from autotwin_core.db.session import session_scope

        async def main():
            async with session_scope() as session:
                assert session is not None

        asyncio.run(main())
    """,
    "get_session": """
        import asyncio

        from autotwin_core.db.session import get_session

        async def main():
            agen = get_session()
            assert await agen.asend(None) is not None
            await agen.aclose()

        asyncio.run(main())
    """,
    "get_sync_engine": """
        from autotwin_core.db.session import get_sync_engine

        # Guarded by the same lock as the async half, so it gets the same first-call check.
        assert get_sync_engine() is not None
    """,
    "get_engine": """
        from autotwin_core.db.session import get_engine

        assert get_engine() is not None
    """,
}


def _run_in_fresh_interpreter(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a new interpreter, failing the test if it hangs instead of returning."""
    script = _PROLOGUE + textwrap.dedent(body) + "\nprint('returned')\n"
    try:
        # S603: the "untrusted input" is this module's own literals, run by the interpreter
        # already running the suite.
        return subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = (
            (exc.stderr or b"").decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        pytest.fail(
            f"the first database call never returned within {_TIMEOUT_S}s — the module-level "
            f"lock is most likely not reentrant again.\n{stderr}"
        )


@pytest.mark.parametrize("entry_point", sorted(_FIRST_CALLS))
def test_first_database_call_returns(entry_point: str) -> None:
    """Every public entry point works as the very first database call of a process."""
    result = _run_in_fresh_interpreter(_FIRST_CALLS[entry_point])

    assert result.returncode == 0, f"{entry_point} failed:\n{result.stdout}\n{result.stderr}"
    assert "returned" in result.stdout


def test_lock_is_reentrant() -> None:
    """The guard itself must be reentrant, stated directly so the reason survives a refactor.

    :func:`get_sessionmaker` calls :func:`get_engine` while holding this lock; a plain
    ``threading.Lock`` makes that a self-deadlock.

    The re-acquire is timed rather than a second ``with`` block: against a non-reentrant lock a
    blocking re-acquire would hang *this* interpreter — pytest included — and a regression that
    hangs the runner reports nothing at all. Failing is the whole point of the test.

    Deliberately white-box, and the only test here that is. ``test_first_database_call_returns``
    is the actual contract; this one exists to name the *cause* in its failure message instead of
    leaving it to be read out of a faulthandler dump. If the locking scheme is ever reworked this
    test is expected to break — re-derive it against whatever replaces ``_lock`` rather than
    deleting it, because the deadlock it guards is silent and the behavioural tests above cost a
    subprocess each to notice it.
    """
    from autotwin_core.db import session as session_module

    with session_module._lock:
        reacquired = session_module._lock.acquire(timeout=_REACQUIRE_TIMEOUT_S)
        if reacquired:
            session_module._lock.release()

    assert reacquired, (
        "the lock guarding the db singletons is not reentrant; get_sessionmaker() will deadlock"
    )
