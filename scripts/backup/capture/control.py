"""Bounded cleanup shields duplicate cancellation; SIGKILL still needs journal recovery."""
from contextlib import contextmanager
import signal
import threading


@contextmanager
def cleanup_signals():
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
