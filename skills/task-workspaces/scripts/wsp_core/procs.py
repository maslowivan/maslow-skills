"""Process discovery: which processes use a tree, and PID identity checks.

We look at each process's current working directory (`lsof -a -d cwd`), never
at a recursive `lsof +D`, which is far too slow on node_modules.
"""

import os
import subprocess


def cwd_processes(prefix):
    """Return [{pid, command, cwd}] for processes whose cwd is inside prefix."""
    prefix = os.path.realpath(prefix)
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpcn"], capture_output=True, text=True, timeout=60
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None  # unknown: callers must treat as "cannot prove no process"
    found = []
    current = {}
    for line in proc.stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            current = {"pid": int(value)}
        elif tag == "c":
            current["command"] = value
        elif tag == "n":
            path = value
            if prefix == os.sep or path == prefix or path.startswith(prefix + os.sep):
                if current.get("pid") != os.getpid():
                    found.append({"pid": current.get("pid"), "command": current.get("command"), "cwd": path})
    return found


def pid_start_time(pid):
    try:
        proc = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    value = proc.stdout.strip()
    return value or None


def pid_alive(pid, start=None):
    """True when pid exists and (if start given) has the same start time."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if start:
        return pid_start_time(pid) == start
    return True
