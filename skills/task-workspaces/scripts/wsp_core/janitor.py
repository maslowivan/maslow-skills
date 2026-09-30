"""Optional background janitor: a deterministic `wsp gc --apply` on a timer (no LLM).

launchd on macOS, systemd user timer on Linux. Installing it is a separate,
explicit step (`wsp janitor install`); `wsp init` never installs it silently.
"""

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


def status(cfg):
    info = {"enabled_in_config": bool(cfg.data["janitor"].get("enabled")), "last_run": util.read_json(last_run_path(cfg))}
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
    util.write_json_atomic(last_run_path(cfg), {"at": util.now_iso(), "applied": result.get("applied"),
                                                "actions": [p["action"] for p in result.get("plan", [])],
                                                "free": result.get("disk", {}).get("free")})
