"""Helpers for the process pools the batch scripts and `check --workers` use."""
from __future__ import annotations

import ctypes
import functools
import gc


def trim_after(fn):
    """Pool-worker decorator: hand glibc's retained heap back to the OS after
    each task. segment() on a dense mesh peaks at 1-3 GB and the allocator keeps
    it resident, so a dozen long-lived workers grow to ~6 GB each; trimmed, a
    worker idles at ~0.3 GB."""
    @functools.wraps(fn)
    def wrapped(*a, **k):
        try:
            return fn(*a, **k)
        finally:
            gc.collect()
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except OSError:
                pass
    return wrapped
