"""Janitor: runs the cleanup policy (`wsp gc --apply`) — deterministic, no LLM.

Modes (config `janitor.mode`):
  on-use     default. No service: when wsp is used (CLI commands, Claude Code hooks)
             and the last run is older than `interval_minutes`, a detached background
             process runs the policy. Nothing runs while wsp is not used — but then
             nothing grows either.
  scheduled  a launchd (macOS) / systemd user timer (Linux) job runs it on a timer,
             also when wsp is idle. Installed only by `wsp janitor install`.
  off        only `wsp gc --apply` run by hand.
"""

import fcntl
import os
import platform
import subprocess
import sys

from . import util

LABEL = "dev.wsp.janitor"


def _wsp_command():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wsp.py")
    return [sys.executable, script]


def plist(cfg):
    interval = int(cfg.data["janitor"].get("interval_minutes", 60)) * 60
    args = _wsp_command() + ["janitor", "run"]
    env = {"WSP_CONFIG": cfg.path, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    items = "".join(f"<string>{a}</string>" for a in args)
    env_items = "".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())
    log = os.path.join(cfg.state_dir, "logs", "janitor.log")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{items}</array>
<key>EnvironmentVariables</key><dict>{env_items}</dict>
<key>StartInterval</key><integer>{interval}</integer>
<key>StandardOutPath</key><string>{log}</string>
<key>StandardErrorPath</key><string>{log}</string>
<key>ProcessType</key><string>Background</string>
</dict></plist>
"""


def _plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def last_run_path(cfg):
    return os.path.join(cfg.state_dir, "janitor-last-run.json")


def mode(cfg):
    return cfg.data["janitor"].get("mode", "on-use")


def due(cfg):
    last = util.read_json(last_run_path(cfg)) or {}
    stamp = last.get("started_at") or last.get("at")
    age = util.age_hours(stamp)
    return age is None or age * 60 >= float(cfg.data["janitor"].get("interval_minutes", 60))


def maybe_run_on_use(cfg):
    """Called after normal wsp usage. Spawns a detached cleanup run when due; never blocks."""
    if os.environ.get("WSP_NO_JANITOR") or not cfg.exists or mode(cfg) != "on-use" or not due(cfg):
        return False
    # claim the slot first so parallel commands do not all spawn a run
    util.write_json_atomic(last_run_path(cfg), {**(util.read_json(last_run_path(cfg)) or {}),
                                                "started_at": util.now_iso(), "trigger": "on-use"})
    log = os.path.join(cfg.state_dir, "logs", "janitor.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = {**os.environ, "WSP_CONFIG": cfg.path, "WSP_NO_JANITOR": "1"}
    with open(log, "a") as out:
        subprocess.Popen(_wsp_command() + ["janitor", "run"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         env=env, start_new_session=True, close_fds=True)
    return True


def run(cfg, reg, gc_fn):
    """Run the policy once; a file lock makes concurrent runs a no-op."""
    lock_path = os.path.join(cfg.state_dir, "locks", "janitor.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"applied": False, "skipped": "another janitor run is in progress", "plan": []}
        apply = mode(cfg) != "off"
        result = gc_fn(apply=apply)
        record_run(cfg, result)
        return result


def status(cfg):
    info = {"mode": mode(cfg), "interval_minutes": cfg.data["janitor"].get("interval_minutes", 60),
            "last_run": util.read_json(last_run_path(cfg)), "due": due(cfg)}
    if platform.system() == "Darwin":
        info["installed"] = os.path.exists(_plist_path())
        proc = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
        info["loaded"] = proc.returncode == 0
    return info


def install(cfg):
    if platform.system() != "Darwin":
        return {"installed": False, "hint": "Linux: create a systemd --user timer running `wsp janitor run`",
                "command": " ".join(_wsp_command() + ["janitor", "run"])}
    path = _plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.join(cfg.state_dir, "logs"), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(plist(cfg))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", path], capture_output=True, text=True)
    return {"installed": proc.returncode == 0, "plist": path, "stderr": proc.stderr.strip() or None}


def uninstall(cfg):
    if platform.system() != "Darwin":
        return {"uninstalled": False, "hint": "remove the systemd --user timer"}
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    path = _plist_path()
    if os.path.exists(path):
        os.unlink(path)
    return {"uninstalled": True}


def record_run(cfg, result):
    previous = util.read_json(last_run_path(cfg)) or {}
    util.write_json_atomic(last_run_path(cfg), {"started_at": previous.get("started_at") or util.now_iso(),
                                                "at": util.now_iso(), "applied": result.get("applied"),
                                                "trigger": previous.get("trigger"),
                                                "actions": [p["action"] for p in result.get("plan", [])],
                                                "free": result.get("disk", {}).get("free")})
