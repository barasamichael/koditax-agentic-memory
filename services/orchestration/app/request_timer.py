"""Per-request wall-clock timer for orchestration instrumentation.

Usage
-----
At the start of a request handler (or any entry point):

    from services.orchestration.app.request_timer import install_request_timer, timed_print

    install_request_timer()          # resets the timer for this thread
    timed_print("first log line")    # [TIMER] +0.000s (Δ0.000s) first log line

Every subsequent timed_print() in any module called from the same thread will
automatically include elapsed and delta timings, without needing to pass an object
around.  If no timer has been installed for the current thread, timed_print() falls
back to a plain print() so nothing breaks in tests or cold paths.
"""

from __future__ import annotations

import time
import threading

_local: threading.local = threading.local()


def install_request_timer() -> None:
    """Reset the per-request timer for the current thread."""
    now = time.perf_counter()
    _local.start = now
    _local.last = now


def timed_print(message: str) -> None:
    """Print *message* prefixed with [TIMER] total and delta elapsed times.

    Falls back to a plain print when no timer is installed for this thread.
    """
    start: float | None = getattr(_local, "start", None)
    if start is None:
        print(message)
        return
    now = time.perf_counter()
    last: float = getattr(_local, "last", start)
    total = now - start
    delta = now - last
    _local.last = now
    print(f"[TIMER] +{total:.3f}s (Δ{delta:.3f}s) {message}")
