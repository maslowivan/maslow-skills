"""Parsers and analysis run on fixtures; one smoke test runs the real collector."""

import importlib.util
import json
import os
import platform
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "heat-check", "scripts", "heat_check.py")

spec = importlib.util.spec_from_file_location("heat_check", SCRIPT)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

TOP = """Processes: 700 total, 4 running, 696 sleeping, 3000 threads
Load Avg: 3.10, 3.20, 4.00
CPU usage: 3.10% user, 5.00% sys, 91.90% idle
PhysMem: 20G used (5000M wired, 1000M compressor), 4G unused.

PID    %CPU MEM    COMMAND
417    0.0  2090M  WindowServer
Processes: 700 total, 4 running, 696 sleeping, 3000 threads
Load Avg: 3.23, 3.29, 4.36
CPU usage: 11.64% user, 9.73% sys, 78.61% idle
SharedLibs: 574M resident, 83M data, 79M linkedit.
PhysMem: 23G used (5535M wired, 8747M compressor), 171M unused.
VM: 505T vsize, 57G framework vsize, 6018055(4) swapins, 8327112(0) swapouts.

PID    %CPU MEM    COMMAND
417    89.4 2090M+ WindowServer
0      30.2 146M-  kernel_task
25734  10.9 52M    SkyComputerUseSe
93275  2.5  561M-  Claude Helper (R
"""

PS = """  417 151:02.33 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer
    0 37:09.46 kernel_task
25734 03:18.06 /Applications/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService
93275 01:53.77 /Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)
"""

DISPLAYS = {"SPDisplaysDataType": [{"spdisplays_ndrvs": [
    {"_name": "Color LCD", "_spdisplays_pixels": "2560 x 1664", "_spdisplays_resolution": "1280 x 832 @ 60.00Hz",
     "spdisplays_connection_type": "spdisplays_internal", "spdisplays_pixelresolution": "spdisplays_2560x1664Retina"},
    {"_name": "External 4K", "_spdisplays_pixels": "5120 x 2880", "_spdisplays_resolution": "2560 x 1440 @ 60.00Hz",
     "spdisplays_pixelresolution": "spdisplays_5k-uhgplus"},
]}]}


def snapshot(**over):
    top = hc.parse_top(TOP)
    ps = hc.parse_ps(PS)
    procs = []
    for p in top["procs"]:
        path = ps[p["pid"]]["path"]
        procs.append({"pid": p["pid"], "name": os.path.basename(path), "app": hc.app_of(path),
                      "cpu": p["cpu"], "mem": p["mem"], "cputime": ps[p["pid"]]["cputime"]})
    snap = {"os": "macos", "ncpu": 10, "load": [3.23, 3.29, 4.36], "cpu": top["cpu"],
            "memory": {"total": 24 << 30, "physmem": top["physmem"], "pressure_level": "normal",
                       "swap": hc.parse_swap("total = 11264.00M  used = 10626.88M  free = 637.12M  (encrypted)")},
            "thermal": hc.parse_therm("Note: No thermal warning level has been recorded\n"
                                      "Note: No performance warning level has been recorded"),
            "temperatures": None, "power": {}, "displays": hc.parse_displays(DISPLAYS), "procs": procs}
    snap.update(over)
    return snap


def codes(findings):
    return [f["code"] for f in findings]


class Parsers(unittest.TestCase):
    def test_top_uses_last_sample(self):
        top = hc.parse_top(TOP)
        self.assertEqual(top["cpu"], {"user": 11.64, "sys": 9.73, "idle": 78.61})
        self.assertEqual(top["procs"][0], {"pid": 417, "cpu": 89.4, "mem": 2090 << 20, "name": "WindowServer"})
        self.assertEqual(top["procs"][3]["name"], "Claude Helper (R")
        self.assertEqual(top["physmem"]["compressor"], 8747 << 20)
        self.assertEqual(top["physmem"]["unused"], 171 << 20)

    def test_ps_keeps_spaces_and_app(self):
        ps = hc.parse_ps(PS)
        path = ps[93275]["path"]
        self.assertTrue(path.endswith("Claude Helper (Renderer)"))
        self.assertEqual(hc.app_of(path), "Claude")
        self.assertEqual(hc.app_of(ps[25734]["path"]), "Codex Computer Use")
        self.assertEqual(hc.app_of("kernel_task"), "kernel_task")

    def test_sizes(self):
        self.assertEqual(hc.parse_size("2090M+"), 2090 << 20)
        self.assertEqual(hc.parse_size("7488K"), 7488 << 10)
        self.assertEqual(hc.parse_size("23G"), 23 << 30)
        self.assertIsNone(hc.parse_size("-"))

    def test_therm_and_batt(self):
        t = hc.parse_therm("Note: No thermal warning level has been recorded\nCPU_Speed_Limit \t= 70")
        self.assertIs(t["thermal_warning"], False)
        self.assertEqual(t["cpu_speed_limit"], 70)
        t = hc.parse_therm("Thermal warning level: 2")
        self.assertEqual(t["thermal_warning"], "2")
        b = hc.parse_batt("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true")
        self.assertEqual(b, {"source": "AC Power", "percent": 80, "charging": "AC attached"})

    def test_displays_scaling(self):
        d = hc.parse_displays(DISPLAYS)
        self.assertFalse(d[0]["scaled"])
        self.assertTrue(d[1]["scaled"])
        self.assertEqual(d[1]["native"], (3840, 2160))
        self.assertEqual(d[1]["looks_like"], (2560, 1440))


class Analysis(unittest.TestCase):
    def test_hot_mac(self):
        f = hc.analyze(snapshot())
        c = codes(f)
        self.assertEqual(f[0]["code"], "WINDOWSERVER_BUSY")
        self.assertEqual(f[0]["severity"], "high")
        self.assertIn("5120x2880", f[0]["evidence"])
        self.assertIn("SkyComputerUseService", f[0]["evidence"])
        self.assertIn("KERNEL_TASK_COOLING", c)
        self.assertIn("HEAVY_SWAP", c)
        self.assertNotIn("SCALED_DISPLAY", c)  # folded into WINDOWSERVER_BUSY
        self.assertNotIn("HOT_PROCESS", c)  # WindowServer/kernel_task have their own codes

    def test_quiet_machine(self):
        f = hc.analyze({"os": "linux", "ncpu": 8, "load": [0.2, 0.3, 0.3],
                        "cpu": {"user": 1.0, "sys": 1.0, "idle": 98.0}, "memory": {}, "thermal": {}, "procs": []})
        self.assertEqual(codes(f), ["NOTHING_HOT"])

    def test_hot_process_and_app_group(self):
        procs = [{"pid": 1, "name": "node", "app": "node", "cpu": 140.0, "mem": 1 << 30},
                 {"pid": 2, "name": "Slack Helper (Renderer)", "app": "Slack", "cpu": 20.0, "mem": 1 << 29},
                 {"pid": 3, "name": "Slack Helper (GPU)", "app": "Slack", "cpu": 15.0, "mem": 1 << 28},
                 {"pid": 4, "name": "Slack", "app": "Slack", "cpu": 10.0, "mem": 1 << 28}]
        f = hc.analyze({"os": "macos", "ncpu": 4, "load": [9.0, 5.0, 4.0], "cpu": {"user": 80, "sys": 10, "idle": 10},
                        "memory": {"pressure_level": "critical", "swap": {}}, "thermal": {"cpu_speed_limit": 75},
                        "procs": procs})
        c = codes(f)
        for code in ("HOT_PROCESS", "BUSY_APP", "HIGH_LOAD", "MEMORY_PRESSURE", "CPU_SPEED_LIMITED", "CPU_SATURATED"):
            self.assertIn(code, c)
        hot = next(x for x in f if x["code"] == "HOT_PROCESS")
        self.assertIn("Node.js", hot["title"])
        self.assertEqual([x["severity"] for x in f], sorted((x["severity"] for x in f),
                                                             key={"high": 0, "medium": 1, "info": 2}.get))

    def test_linux_temperature(self):
        f = hc.analyze({"os": "linux", "ncpu": 4, "load": [1, 1, 1], "cpu": {"user": 5, "sys": 5, "idle": 90},
                        "memory": {}, "thermal": {}, "temperatures": [{"sensor": "x86_pkg_temp", "celsius": 93.0}],
                        "procs": []})
        self.assertEqual(f[0]["code"], "HIGH_TEMPERATURE")
        self.assertEqual(f[0]["severity"], "high")

    def test_report_renders(self):
        snap = snapshot()
        text = hc.report(snap, hc.analyze(snap), 10)
        self.assertIn("WindowServer", text)
        self.assertIn("Findings:", text)


@unittest.skipUnless(platform.system() in ("Darwin", "Linux"), "collector supports macOS and Linux")
class Smoke(unittest.TestCase):
    def test_real_run_json(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--json", "--interval", "1", "--top", "5", "--skip", "displays"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertGreater(len(data["snapshot"]["procs"]), 0)
        self.assertLessEqual(len(data["snapshot"]["procs"]), 5)
        self.assertTrue(data["findings"])
        for f in data["findings"]:
            self.assertIn(f["severity"], ("high", "medium", "info"))


if __name__ == "__main__":
    unittest.main()
