"""Process-level file locks with a fixed acquisition order.

Locks are advisory flock()s on files under <state_dir>/locks; the OS releases
them when the process exits. The durable operation journal lives in the registry.
Order: task -> repo -> tree -> cache, then by name.
"""

import contextlib
import fcntl
import os
import time

from .errors import WspError

_RANK = {"task": 0, "repo": 1, "tree": 2, "cache": 3, "registry": 4}


def _key(name):
    kind = name.split(":", 1)[0]
    return (_RANK.get(kind, 9), name)


@contextlib.contextmanager
def locked(state_dir, *names, timeout=120):
    lock_dir = os.path.join(state_dir, "locks")
    os.makedirs(lock_dir, exist_ok=True)
    handles = []
    try:
        for name in sorted(set(names), key=_key):
            safe = name.replace("/", "_").replace(":", "-")
            fh = open(os.path.join(lock_dir, f"{safe}.lock"), "a+")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        fh.close()
                        raise WspError("LOCKED", f"another wsp operation holds lock {name}", lock=name)
                    time.sleep(0.2)
            handles.append(fh)
        yield
    finally:
        for fh in reversed(handles):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
