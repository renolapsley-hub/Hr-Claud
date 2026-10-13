"""Tiny background-job runner so long operations don't time out the phone browser.
One task at a time (protects API quotas). Runs in-process, so use a single gunicorn worker."""
import threading
import uuid

import db

_lock = threading.Lock()
_tasks = {}
_busy = None


class Busy(Exception):
    pass


def start(kind, fn):
    """fn(log, progress) -> summary string. Returns task id."""
    global _busy
    with _lock:
        if _busy and _tasks[_busy]["state"] == "running":
            raise Busy("Another task is still running - wait for it to finish.")
        tid = uuid.uuid4().hex[:8]
        _tasks[tid] = {"id": tid, "kind": kind, "state": "running", "log": [], "errors": [], "done": 0, "total": 0,
                       "summary": "", "started": db.now()}
        _busy = tid
    t = _tasks[tid]

    def log(msg):
        t["log"] = (t["log"] + [msg])[-40:]

    def progress(done, total):
        t["done"], t["total"] = done, total

    def work():
        try:
            t["summary"] = fn(log, progress, t["errors"]) or "Done"
            t["state"] = "done"
        except Exception as e:  # shown to the user in plain words
            t["state"], t["summary"] = "error", f"Stopped unexpectedly: {e.__class__.__name__}: {e}"
        finally:
            t["finished"] = db.now()

    threading.Thread(target=work, daemon=True).start()
    return tid


def get(tid):
    return _tasks.get(tid)


def current():
    return _tasks.get(_busy) if _busy else None
