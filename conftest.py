"""Tests import the engine directly: no GUI libraries needed (CI friendly)."""
import os, queue, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import shrinkzip_engine as app  # noqa: E402


def run_engine(archive, dest, shrink=True, delete=False, stop_after_shrinks=None):
    """Run the worker to completion and return (final_event, all_events)."""
    q = queue.Queue()
    w = app.ShrinkZipEngineWorker(str(archive), str(dest), shrink, delete, "overwrite", q)
    w.start()
    events, shrinks = [], 0
    while True:
        kind, data = q.get(timeout=180)
        events.append((kind, data))
        if kind == "log" and data.startswith("[SHRINK]") and stop_after_shrinks:
            shrinks += 1
            if shrinks == stop_after_shrinks:
                w.request_stop()
        if kind in ("done", "error", "stopped"):
            w.join(10)
            return (kind, data), events
