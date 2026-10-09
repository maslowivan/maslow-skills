#!/usr/bin/env python3
"""heat-check: read-only snapshot of what makes a computer hot or busy.

Samples CPU per process over a short interval, groups processes by app, reads
load, memory pressure and swap, thermal/throttling state, power source and (on
macOS) display scaling, then turns that into findings with evidence and advice.

Needs no root and changes nothing. macOS is the main target; Linux is supported
through /proc and /sys. Python 3.9+, standard library only.

  heat_check.py              human-readable report
  heat_check.py --json       machine-readable snapshot + findings
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time

VERSION = "0.1.1"

# Process names (basename, case-sensitive) that explain themselves. The hint is
# shown next to the process in findings so the user knows what it is and what helps.
KNOWN = {
    "WindowServer": ("macOS compositor: draws every window on every display",
                     "Heavy with scaled (non-native) resolutions on large external displays, many "
                     "displays, screen recording/sharing, transparency and animations, or an app "
                     "redrawing constantly."),
    "kernel_task": ("macOS kernel; also holds the CPU back on purpose to cool the machine",
                    "High kernel_task is usually a symptom of heat, not its cause: fix the other "
                    "heat sources, check ventilation and the charger."),
    "mds": ("Spotlight index server", "Indexing after updates, big copies or new disks; usually "
            "settles. Exclude build/dependency folders in Spotlight privacy settings."),
    "mds_stores": ("Spotlight index store", "See mds."),
    "mdworker_shared": ("Spotlight file importer", "See mds."),
    "corespotlightd": ("Spotlight app content indexer", "See mds."),
    "photoanalysisd": ("Photos library analysis", "Runs after imports; pauses on battery. Let it finish on AC."),
    "mediaanalysisd": ("Photos/media analysis", "See photoanalysisd."),
    "backupd": ("Time Machine backup", "Finishes on its own; exclude large rebuildable folders."),
    "cloudd": ("iCloud sync", "Large syncs settle; check iCloud Drive for a stuck upload."),
    "bird": ("iCloud Drive sync", "See cloudd."),
    "fileproviderd": ("File Provider (iCloud/Dropbox/Drive sync)", "A sync client is busy or stuck."),
    "softwareupdated": ("macOS software update", "Downloading or preparing an update; settles after it."),
    "XprotectService": ("macOS malware scan", "Scans new files; settles."),
    "syspolicyd": ("Gatekeeper checks", "Often busy after installing or running many new binaries."),
    "replayd": ("macOS screen capture service", "Something is recording or sharing the screen; "
                "every captured frame also loads WindowServer."),
    "fseventsd": ("file change notification service", "Busy when many files change at once: git checkouts and "
                  "worktrees, dependency installs, builds and file watchers, sync clients, backups. Stop "
                  "the bulk job or watcher; exclude churning folders from Spotlight."),
    "spindump": ("writes a report about an app that stopped responding", "Temporary. The hung app is named in "
                 "recent hang reports (Console → Diagnostic Reports); restart it if it keeps hanging."),
    "coreaudiod": ("Core Audio", "Busy with active audio streams, calls or virtual audio devices."),
    "Google Chrome Helper (Renderer)": ("a Chrome tab", "Find the tab in Chrome's Task Manager (Window → Task Manager)."),
    "node": ("Node.js process", "Dev servers, watchers and builds; stop the ones you do not use."),
    "com.apple.Virtualization.VirtualMachine": ("a virtual machine (Docker, UTM, ...)",
                                                "Stop containers/VMs you do not need or lower their CPU limit."),
    "qemu-system-aarch64": ("a virtual machine", "See com.apple.Virtualization.VirtualMachine."),
}

SPOTLIGHT = {"mds", "mds_stores", "mdworker", "mdworker_shared", "corespotlightd"}
OWN_CODE = {"WindowServer", "kernel_task", "fseventsd", "spindump"}  # have their own findings
REPORT_DIRS = ("/Library/Logs/DiagnosticReports", "~/Library/Logs/DiagnosticReports")
REPORT_HOURS = 3
CAPTURE_RE = re.compile(r"replayd|screencapture|ScreenCapture|ComputerUse|CptHost|obs$", re.I)


# ---------------------------------------------------------------- helpers

def run(cmd, timeout=20):
    """Run a command, return stdout ('' on any failure)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def parse_size(text):
    """'2090M+', '7488K', '23G', '512B', '1.5T' -> bytes (int) or None."""
    m = re.match(r"^\s*([\d.]+)\s*([BKMGT]?)", text or "")
    if not m:
        return None
    mult = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2)]
    return int(float(m.group(1)) * mult)


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024.0
    return "?"


def app_of(path):
    """Group key for a process: the outermost '<X>.app' in its path, else its basename."""
    for part in (path or "").split("/"):
        if part.endswith(".app") and len(part) > 4:
            return part[:-4]
    return os.path.basename(path or "") or "?"


# ---------------------------------------------------------------- macOS parsers (pure, tested)

def parse_top(text):
    """Parse `top -l 2 -stats pid,cpu,mem,command`; use the last sample (the first has no CPU deltas)."""
    blocks = re.split(r"^Processes:", text, flags=re.M)
    block = blocks[-1] if len(blocks) > 1 else text
    out = {"cpu": None, "physmem": None, "procs": []}
    m = re.search(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", block)
    if m:
        out["cpu"] = {"user": float(m.group(1)), "sys": float(m.group(2)), "idle": float(m.group(3))}
    m = re.search(r"PhysMem:\s*(\S+) used(?: \((\S+) wired(?:, (\S+) compressor)?\))?,\s*(\S+) unused", block)
    if m:
        out["physmem"] = {"used": parse_size(m.group(1)), "wired": parse_size(m.group(2)),
                          "compressor": parse_size(m.group(3)), "unused": parse_size(m.group(4))}
    rows = False
    for line in block.splitlines():
        if re.match(r"^\s*PID\s+%CPU", line):
            rows = True
            continue
        if not rows:
            continue
        m = re.match(r"^\s*(\d+)\s+([\d.]+)\s+(\S+)\s+(.*\S)\s*$", line)
        if m:
            out["procs"].append({"pid": int(m.group(1)), "cpu": float(m.group(2)),
                                 "mem": parse_size(m.group(3)), "name": m.group(4)})
    return out


def parse_ps(text):
    """`ps -Ao pid=,time=,comm=` -> {pid: {"path", "cputime"}} (comm may contain spaces)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(.+?)\s*$", line)
        if m:
            out[int(m.group(1))] = {"cputime": m.group(2), "path": m.group(3)}
    return out


def parse_swap(text):
    m = re.search(r"total = ([\d.]+[MGK]?)\s+used = ([\d.]+[MGK]?)\s+free = ([\d.]+[MGK]?)", text or "")
    if not m:
        return None
    return {"total": parse_size(m.group(1)), "used": parse_size(m.group(2)), "free": parse_size(m.group(3))}


def parse_therm(text):
    """`pmset -g therm` -> warning flags and speed limit (100 = not throttled)."""
    out = {"thermal_warning": None, "performance_warning": None, "cpu_speed_limit": None}
    for line in (text or "").splitlines():
        low = line.lower()
        m = re.search(r"cpu_speed_limit\s*=\s*(\d+)", low)
        if m:
            out["cpu_speed_limit"] = int(m.group(1))
        for key, word in (("thermal_warning", "thermal warning level"),
                          ("performance_warning", "performance warning level")):
            if word in low:
                if "no " + word in low:
                    out[key] = False
                else:
                    out[key] = line.split(":", 1)[-1].strip() or True
    return out


def parse_batt(text):
    out = {"source": None, "percent": None, "charging": None}
    m = re.search(r"drawing from '([^']+)'", text or "")
    if m:
        out["source"] = m.group(1)
    m = re.search(r"(\d+)%;\s*([^;]+);", text or "")
    if m:
        out["percent"] = int(m.group(1))
        out["charging"] = m.group(2).strip()
    return out


def parse_displays(data):
    """system_profiler SPDisplaysDataType -json -> list of displays with scaling info."""
    out = []
    for gpu in (data or {}).get("SPDisplaysDataType", []):
        for d in gpu.get("spdisplays_ndrvs", []) or []:
            fb = _wh(d.get("_spdisplays_pixels"))
            looks = _wh(d.get("_spdisplays_resolution"))
            hz = re.search(r"@\s*([\d.]+)Hz", d.get("_spdisplays_resolution", "") or "")
            internal = d.get("spdisplays_connection_type") == "spdisplays_internal"
            native = _wh((d.get("spdisplays_pixelresolution") or "").replace("spdisplays_", ""))
            name = d.get("_name") or "?"
            if not native and not internal and re.search(r"4k|uhd|2160", name, re.I):
                native = (3840, 2160)
            scaled = None
            if fb and native:
                scaled = fb != native
            out.append({"name": name, "internal": internal, "framebuffer": fb, "looks_like": looks,
                        "native": native, "scaled": scaled, "refresh_hz": float(hz.group(1)) if hz else None})
    return out


def parse_reports(entries, now, hours=REPORT_HOURS):
    """[(filename, mtime)] of DiagnosticReports -> recent hang / CPU / memory-kill reports, newest first.

    Only file names are read (process, kind, age); the reports themselves are not opened."""
    out = []
    for name, mtime in entries:
        age = (now - mtime) / 60.0
        if age < 0 or age > hours * 60:
            continue
        if name.startswith("JetsamEvent"):
            kind, proc = "memory_kill", None
        else:
            m = re.match(r"^(.+?)[_-]\d{4}-\d{2}-\d{2}-\d{6}(?:_[^.]*)?\.(.+)$", name)
            if not m:
                continue
            proc, ext = m.group(1), m.group(2)
            if ext in ("hang", "spin") or ext.startswith("hang."):
                kind = "hang"
            elif ext.startswith("cpu_resource"):
                kind = "cpu_resource"
            else:
                continue
        out.append({"kind": kind, "process": proc, "minutes_ago": int(age)})
    out.sort(key=lambda r: r["minutes_ago"])
    return out


def list_reports():
    entries = []
    for d in REPORT_DIRS:
        d = os.path.expanduser(d)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            try:
                entries.append((n, os.stat(os.path.join(d, n)).st_mtime))
            except OSError:
                pass
    return parse_reports(entries, time.time())


def _wh(text):
    m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


# ---------------------------------------------------------------- collectors

def sysctl(name):
    return run(["sysctl", "-n", name]).strip()


def collect_macos(interval, top_n, skip):
    n = max(top_n, 60)
    top = parse_top(run(["top", "-l", "2", "-s", str(max(1, int(round(interval)))), "-n", str(n),
                         "-o", "cpu", "-stats", "pid,cpu,mem,command"], timeout=interval + 30))
    ps = parse_ps(run(["ps", "-Ao", "pid=,time=,comm="]))
    procs = []
    for p in top["procs"]:
        info = ps.get(p["pid"], {})
        path = info.get("path") or p["name"]
        procs.append({"pid": p["pid"], "name": os.path.basename(path) or p["name"], "app": app_of(path),
                      "cpu": p["cpu"], "mem": p["mem"], "cputime": info.get("cputime")})
    load = [float(x) for x in re.findall(r"[\d.]+", sysctl("vm.loadavg"))[:3]]
    pressure = sysctl("kern.memorystatus_vm_pressure_level")
    snap = {
        "os": "macos",
        "model": sysctl("hw.model") or None,
        "cpu_model": sysctl("machdep.cpu.brand_string") or None,
        "ncpu": int(sysctl("hw.ncpu") or os.cpu_count() or 1),
        "load": load,
        "cpu": top["cpu"],
        "memory": {
            "total": int(sysctl("hw.memsize") or 0) or None,
            "physmem": top["physmem"],
            "pressure_level": {"1": "normal", "2": "warning", "4": "critical"}.get(pressure, pressure or None),
            "swap": parse_swap(sysctl("vm.swapusage")),
        },
        "thermal": parse_therm(run(["pmset", "-g", "therm"])),
        "temperatures": None,  # Apple Silicon/Intel sensors need root (powermetrics); see SKILL.md
        "power": parse_batt(run(["pmset", "-g", "batt"])),
        "low_power_mode": _low_power(run(["pmset", "-g"])),
        "displays": None,
        "reports": list_reports(),
        "procs": procs,
    }
    if "displays" not in skip:
        raw = run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=30)
        try:
            snap["displays"] = parse_displays(json.loads(raw)) if raw else None
        except ValueError:
            snap["displays"] = None
    return snap


def _low_power(text):
    m = re.search(r"^\s*(?:lowpowermode|powermode)\s+(\d+)", text or "", re.M)
    return None if not m else m.group(1) != "0"


def _proc_ticks():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as fh:
                raw = fh.read()
        except OSError:
            continue
        name = raw[raw.find("(") + 1:raw.rfind(")")]
        f = raw[raw.rfind(")") + 2:].split()
        # after "(comm) ": state=0 ... utime=11 stime=12 ... rss=21
        out[int(pid)] = (name, int(f[11]) + int(f[12]), int(f[21]))
    return out


def _cpu_times():
    with open("/proc/stat") as fh:
        vals = [int(x) for x in fh.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle, vals[0] + vals[1], vals[2]


def collect_linux(interval, top_n, skip):
    hz = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    a, ca = _proc_ticks(), _cpu_times()
    t0 = time.time()
    time.sleep(interval)
    b, cb = _proc_ticks(), _cpu_times()
    dt = max(time.time() - t0, 0.001)
    procs = []
    for pid, (name, ticks, rss) in b.items():
        if pid in a:
            cpu = (ticks - a[pid][1]) / hz / dt * 100.0
            procs.append({"pid": pid, "name": name, "app": name, "cpu": round(cpu, 1), "mem": rss * page,
                          "cputime": None})
    procs.sort(key=lambda p: -p["cpu"])
    total, idle = cb[0] - ca[0], cb[1] - ca[1]
    cpu = None
    if total > 0:
        cpu = {"user": round((cb[2] - ca[2]) / total * 100, 2), "sys": round((cb[3] - ca[3]) / total * 100, 2),
               "idle": round(idle / total * 100, 2)}
    mem = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    with open("/proc/loadavg") as fh:
        load = [float(x) for x in fh.read().split()[:3]]
    swap = None
    if "SwapTotal" in mem:
        swap = {"total": mem["SwapTotal"], "used": mem["SwapTotal"] - mem.get("SwapFree", 0),
                "free": mem.get("SwapFree")}
    avail = mem.get("MemAvailable")
    level = None
    if avail is not None and mem.get("MemTotal"):
        frac = avail / mem["MemTotal"]
        level = "critical" if frac < 0.05 else "warning" if frac < 0.15 else "normal"
    return {
        "os": "linux",
        "model": _read("/sys/class/dmi/id/product_name"),
        "cpu_model": _cpu_model(),
        "ncpu": os.cpu_count() or 1,
        "load": load,
        "cpu": cpu,
        "memory": {"total": mem.get("MemTotal"), "available": avail, "pressure_level": level, "swap": swap},
        "thermal": {"cpu_speed_limit": _cpu_speed_limit()},
        "temperatures": _temps(),
        "power": _linux_power(),
        "low_power_mode": None,
        "displays": None,
        "procs": procs[:max(top_n, 60)],
    }


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _cpu_speed_limit():
    cur, top = [], []
    base = "/sys/devices/system/cpu"
    try:
        cpus = [c for c in os.listdir(base) if re.match(r"cpu\d+$", c)]
    except OSError:
        return None
    for c in cpus:
        a = _read(f"{base}/{c}/cpufreq/scaling_max_freq")
        b = _read(f"{base}/{c}/cpufreq/cpuinfo_max_freq")
        if a and b:
            cur.append(int(a))
            top.append(int(b))
    if not top:
        return None
    return round(100 * sum(cur) / sum(top))


def _temps():
    out = []
    base = "/sys/class/thermal"
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        zones = []
    for z in zones:
        t = _read(f"{base}/{z}/temp")
        if t and t.lstrip("-").isdigit():
            out.append({"sensor": _read(f"{base}/{z}/type") or z, "celsius": int(t) / 1000.0})
    return out or None


def _linux_power():
    base = "/sys/class/power_supply"
    out = {"source": None, "percent": None, "charging": None}
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for n in names:
        kind = _read(f"{base}/{n}/type")
        if kind == "Mains":
            out["source"] = "AC Power" if _read(f"{base}/{n}/online") == "1" else "Battery Power"
        elif kind == "Battery":
            cap = _read(f"{base}/{n}/capacity")
            out["percent"] = int(cap) if cap and cap.isdigit() else None
            out["charging"] = (_read(f"{base}/{n}/status") or "").lower() or None
    return out


# ---------------------------------------------------------------- analysis (pure, tested)

def group_apps(procs):
    apps = {}
    for p in procs:
        g = apps.setdefault(p["app"], {"app": p["app"], "cpu": 0.0, "mem": 0, "count": 0})
        g["cpu"] += p["cpu"]
        g["mem"] += p["mem"] or 0
        g["count"] += 1
    out = sorted(apps.values(), key=lambda g: (-g["cpu"], -g["mem"]))
    for g in out:
        g["cpu"] = round(g["cpu"], 1)
    return out


def _sentence(text):
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def _f(severity, code, title, evidence, advice):
    return {"severity": severity, "code": code, "title": title, "evidence": evidence, "advice": advice}


def analyze(snap):
    """Turn a snapshot into findings, most severe first."""
    findings = []
    procs = snap.get("procs") or []
    by_name = {}
    for p in procs:
        by_name.setdefault(p["name"], []).append(p)

    def cpu_of(name):
        return sum(p["cpu"] for p in by_name.get(name, []))

    ncpu = snap.get("ncpu") or 1
    load = snap.get("load") or []
    cpu = snap.get("cpu") or {}
    displays = snap.get("displays") or []
    capture = [p for p in procs if CAPTURE_RE.search(p["name"]) and p["cpu"] >= 1.0]

    therm = snap.get("thermal") or {}
    limit = therm.get("cpu_speed_limit")
    if therm.get("thermal_warning") not in (None, False) or therm.get("performance_warning") not in (None, False):
        findings.append(_f("high", "THERMAL_WARNING", "The OS reports a thermal/performance warning",
                           f"thermal: {therm.get('thermal_warning')}, performance: {therm.get('performance_warning')}",
                           "The machine is throttling. Reduce the load below, keep vents free, use a hard surface."))
    if limit is not None and limit < 100:
        findings.append(_f("high" if limit < 80 else "medium", "CPU_SPEED_LIMITED",
                           f"CPU speed is limited to {limit}%", "cpu_speed_limit < 100",
                           "Thermal or power throttling is active; everything runs slower until it cools down."))

    temps = snap.get("temperatures") or []
    hot = max((t["celsius"] for t in temps), default=None)
    if hot is not None and hot >= 80:
        findings.append(_f("high" if hot >= 90 else "medium", "HIGH_TEMPERATURE", f"Hottest sensor at {hot:.0f} °C",
                           ", ".join(f"{t['sensor']} {t['celsius']:.0f}°C" for t in temps if t["celsius"] >= 70),
                           "Reduce the CPU load below; check fans and dust."))

    ws = cpu_of("WindowServer")
    if ws >= 30:
        scaled = [d for d in displays if d.get("scaled")]
        bits = [f"WindowServer {ws:.0f}% CPU"]
        advice = []
        if scaled:
            bits.append("scaled displays: " + ", ".join(
                f"{d['name']} renders {d['framebuffer'][0]}x{d['framebuffer'][1]} for a "
                f"{d['native'][0]}x{d['native'][1]} panel" for d in scaled))
            advice.append("set scaled displays to their default/native-friendly resolution "
                          "(System Settings → Displays → Default)")
        big = sum(d["framebuffer"][0] * d["framebuffer"][1] for d in displays if d.get("framebuffer"))
        if big:
            bits.append(f"{big / 1e6:.1f} MP drawn across {len(displays)} display(s)")
        if capture:
            bits.append("screen capture active: " + ", ".join(f"{p['name']} {p['cpu']:.0f}%" for p in capture))
            advice.append("stop screen recording/sharing or agent screen control you do not need")
        advice.append("close apps that animate constantly; consider Reduce transparency/motion")
        findings.append(_f("high" if ws >= 60 else "medium", "WINDOWSERVER_BUSY",
                           "The window compositor is the biggest CPU consumer" if procs and procs[0]["name"] == "WindowServer"
                           else "The window compositor is busy",
                           "; ".join(bits), _sentence("; ".join(advice))))
    elif capture:
        findings.append(_f("info", "SCREEN_CAPTURE", "Something is capturing the screen",
                           ", ".join(f"{p['name']} {p['cpu']:.0f}%" for p in capture),
                           "Screen recording/sharing/agent control costs CPU and GPU; stop it when not needed."))

    kt = cpu_of("kernel_task")
    if kt >= 15:
        findings.append(_f("high" if kt >= 50 else "medium", "KERNEL_TASK_COOLING",
                           "kernel_task is holding the CPU back (heat or charger symptom)",
                           f"kernel_task {kt:.0f}% CPU",
                           "This is how macOS cools down; it is a symptom. Fix the other findings, keep vents free, "
                           "prefer a charger on the left ports of Intel Macs."))

    hot_apps = set()
    for p in procs:
        if p["name"] in OWN_CODE or p["cpu"] < 50:
            continue
        hot_apps.add(p["app"])
        known = KNOWN.get(p["name"])
        what = f" — {known[0]}" if known else ""
        findings.append(_f("high" if p["cpu"] >= 90 else "medium", "HOT_PROCESS",
                           f"{p['name']} uses {p['cpu']:.0f}% CPU{what}",
                           f"pid {p['pid']}, app {p['app']}, cpu time {p.get('cputime') or '?'}",
                           known[1] if known else "Check what it is doing; quit or restart it if it is not needed "
                                                  "(ask before killing anything)."))

    spot = sum(p["cpu"] for p in procs if p["name"] in SPOTLIGHT or p["name"].startswith("mdworker"))
    fse = cpu_of("fseventsd")
    if fse >= 20:
        ev = f"fseventsd {fse:.0f}% CPU" + (f", Spotlight {spot:.0f}% CPU indexing the same changes" if spot >= 10 else "")
        findings.append(_f("high" if fse >= 60 else "medium", "FILE_CHURN",
                           "Many files are changing at once (fseventsd is busy)", ev,
                           KNOWN["fseventsd"][1] + " To see who writes, run yourself: "
                           "sudo fs_usage -w -f filesys | head -200"))

    reports = snap.get("reports") or []
    hangs = [r for r in reports if r["kind"] == "hang"]
    hung = ", ".join(f"{r['process']} {r['minutes_ago']} min ago" for r in hangs[:5])
    sd = cpu_of("spindump")
    if sd >= 5:
        findings.append(_f("medium" if sd >= 20 else "info", "APP_HANG",
                           "An app recently stopped responding (spindump is writing a report)",
                           f"spindump {sd:.0f}% CPU" + (f"; recent hang reports: {hung}" if hung else ""),
                           KNOWN["spindump"][1]))
    elif hangs:
        findings.append(_f("info", "APP_HANG", "Apps stopped responding recently", f"hang reports: {hung}",
                           KNOWN["spindump"][1]))
    flagged = {}
    for r in reports:
        if r["kind"] == "cpu_resource":
            flagged.setdefault(r["process"], r["minutes_ago"])
    if flagged:
        findings.append(_f("info", "CPU_RESOURCE_REPORTS",
                           f"macOS flagged heavy CPU use in the last {REPORT_HOURS} h",
                           ", ".join(f"{k} {v} min ago" for k, v in list(flagged.items())[:6]),
                           "These processes exceeded macOS CPU limits recently; if they are not busy now, the heat "
                           "may come in bursts from them."))
    kills = [r for r in reports if r["kind"] == "memory_kill"]
    if kills:
        findings.append(_f("medium", "MEMORY_KILLS", "macOS killed processes for lack of memory",
                           f"{len(kills)} JetsamEvent report(s), latest {kills[0]['minutes_ago']} min ago",
                           "Memory ran out recently; quit memory-heavy apps."))

    if spot >= 20:
        findings.append(_f("medium", "SPOTLIGHT_INDEXING", "Spotlight is indexing", f"Spotlight processes {spot:.0f}% CPU",
                           KNOWN["mds"][1]))

    for g in group_apps(procs):
        if g["count"] >= 3 and g["cpu"] >= 40 and g["app"] not in hot_apps:
            findings.append(_f("medium", "BUSY_APP", f"{g['app']} uses {g['cpu']:.0f}% CPU across {g['count']} processes",
                               f"{g['count']} processes, {human(g['mem'])} memory",
                               "Close unused windows/tabs of this app or restart it."))

    if len(load) == 3 and load[0] > ncpu:
        findings.append(_f("high" if load[0] > 2 * ncpu else "medium", "HIGH_LOAD",
                           f"Load average {load[0]:.1f} exceeds {ncpu} CPU cores",
                           f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}", "More work is queued than cores can run."))

    mem = snap.get("memory") or {}
    swap = mem.get("swap") or {}
    level = mem.get("pressure_level")
    if level in ("warning", "critical"):
        findings.append(_f("high" if level == "critical" else "medium", "MEMORY_PRESSURE", f"Memory pressure is {level}",
                           f"swap used {human(swap.get('used'))} of {human(swap.get('total'))}",
                           "Quit memory-heavy apps; compression and swapping cost CPU and heat."))
    elif swap.get("total") and swap["total"] >= (1 << 30) and (swap.get("used") or 0) / swap["total"] >= 0.75:
        phys = mem.get("physmem") or {}
        findings.append(_f("medium", "HEAVY_SWAP", "Swap is nearly full",
                           f"swap used {human(swap['used'])} of {human(swap['total'])}"
                           + (f", compressor {human(phys.get('compressor'))}" if phys.get("compressor") else ""),
                           "Memory was short for a while: quit apps you do not need; a restart of long-running "
                           "apps frees leaked memory."))

    if cpu and cpu.get("idle") is not None and cpu["idle"] < 20:
        findings.append(_f("medium", "CPU_SATURATED", f"CPU is {100 - cpu['idle']:.0f}% busy",
                           f"user {cpu['user']}%, sys {cpu['sys']}%", "See the processes above."))

    for d in displays:
        if d.get("scaled") and not any(f["code"] == "WINDOWSERVER_BUSY" for f in findings):
            findings.append(_f("info", "SCALED_DISPLAY", f"{d['name']} uses a scaled resolution",
                               f"renders {d['framebuffer'][0]}x{d['framebuffer'][1]} for a "
                               f"{d['native'][0]}x{d['native'][1]} panel",
                               "Scaled modes cost GPU/WindowServer time; the default mode is cheapest."))

    if not findings:
        findings.append(_f("info", "NOTHING_HOT", "Nothing stands out right now",
                           f"CPU idle {cpu.get('idle', '?')}%, load {load[0] if load else '?'}",
                           "If it was hot a moment ago, the spike has passed; run again while it is hot."))
    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return findings


# ---------------------------------------------------------------- output

def report(snap, findings, top_n):
    lines = []
    cpu = snap.get("cpu") or {}
    load = snap.get("load") or []
    head = f"{snap.get('model') or snap['os']}"
    if snap.get("cpu_model"):
        head += f" · {snap['cpu_model']}"
    head += f" · {snap['ncpu']} cores"
    lines.append(head)
    if load:
        lines.append(f"Load: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
    if cpu:
        lines.append(f"CPU: user {cpu['user']}%  sys {cpu['sys']}%  idle {cpu['idle']}%")
    mem = snap.get("memory") or {}
    swap = mem.get("swap") or {}
    parts = [f"total {human(mem.get('total'))}"]
    if mem.get("physmem"):
        pm = mem["physmem"]
        parts.append(f"used {human(pm['used'])}, compressor {human(pm.get('compressor'))}, free {human(pm['unused'])}")
    if mem.get("available") is not None:
        parts.append(f"available {human(mem['available'])}")
    if swap:
        parts.append(f"swap {human(swap.get('used'))}/{human(swap.get('total'))}")
    if mem.get("pressure_level"):
        parts.append(f"pressure {mem['pressure_level']}")
    lines.append("Memory: " + ", ".join(parts))
    therm = snap.get("thermal") or {}
    t = []
    if therm.get("thermal_warning") is not None:
        t.append("thermal warning: " + ("none" if therm["thermal_warning"] is False else str(therm["thermal_warning"])))
    if therm.get("cpu_speed_limit") is not None:
        t.append(f"CPU speed limit {therm['cpu_speed_limit']}%")
    for s in snap.get("temperatures") or []:
        t.append(f"{s['sensor']} {s['celsius']:.0f}°C")
    if t:
        lines.append("Thermal: " + ", ".join(t))
    pw = snap.get("power") or {}
    if pw.get("source") or pw.get("percent") is not None:
        lines.append(f"Power: {pw.get('source') or '?'}, battery {pw.get('percent')}% ({pw.get('charging')})"
                     + (", Low Power Mode on" if snap.get("low_power_mode") else ""))
    for d in snap.get("displays") or []:
        fb, lk = d.get("framebuffer"), d.get("looks_like")
        desc = f"Display {d['name']}: "
        desc += f"renders {fb[0]}x{fb[1]}" if fb else "?"
        if lk:
            desc += f", looks like {lk[0]}x{lk[1]}"
        if d.get("scaled"):
            desc += " (scaled)"
        lines.append(desc)
    lines.append("")
    lines.append(f"{'PID':>7} {'%CPU':>6} {'MEM':>7}  PROCESS (APP)")
    for p in (snap.get("procs") or [])[:top_n]:
        app = f" ({p['app']})" if p["app"] != p["name"] else ""
        lines.append(f"{p['pid']:>7} {p['cpu']:>6.1f} {human(p['mem']):>7}  {p['name']}{app}")
    lines.append("")
    lines.append("By app:")
    for g in group_apps(snap.get("procs") or [])[:8]:
        lines.append(f"  {g['cpu']:>6.1f}% {human(g['mem']):>7}  {g['app']} ({g['count']})")
    lines.append("")
    lines.append("Findings:")
    for f in findings:
        lines.append(f"  [{f['severity']}] {f['title']}")
        lines.append(f"      evidence: {f['evidence']}")
        lines.append(f"      advice:   {f['advice']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="heat-check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--interval", type=float, default=2.0, help="CPU sampling interval, seconds (default 2)")
    ap.add_argument("--top", type=int, default=15, help="processes to list (default 15)")
    ap.add_argument("--skip", action="append", default=[], choices=["displays"],
                    help="skip a slow collector (repeatable)")
    ap.add_argument("--version", action="version", version=f"heat-check {VERSION}")
    args = ap.parse_args(argv)
    system = platform.system()
    try:
        if system == "Darwin":
            snap = collect_macos(args.interval, args.top, set(args.skip))
        elif system == "Linux":
            snap = collect_linux(args.interval, args.top, set(args.skip))
        else:
            raise RuntimeError(f"unsupported OS: {system}")
    except Exception as exc:  # report, do not crash with a traceback
        err = {"ok": False, "error": {"code": "COLLECT_FAILED", "message": f"{type(exc).__name__}: {exc}"}}
        print(json.dumps(err) if args.json else f"heat-check: {err['error']['message']}", file=sys.stdout)
        return 1
    if not snap.get("procs"):
        snap.setdefault("warnings", []).append("no per-process CPU data collected")
    findings = analyze(snap)
    if args.json:
        apps = group_apps(snap.get("procs") or [])
        snap["procs"] = (snap.get("procs") or [])[:max(args.top, 1)]
        print(json.dumps({"ok": True, "version": VERSION, "snapshot": snap, "apps": apps[:max(args.top, 1)],
                          "findings": findings}, indent=1, default=list))
    else:
        print(report(snap, findings, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
