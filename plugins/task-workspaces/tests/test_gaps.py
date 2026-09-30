import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Sandbox, sh  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "skills", "task-workspaces", "scripts"))
from wsp_core import disk  # noqa: E402


class RecloneSourceTests(Sandbox):
    repos = ("app",)

    def test_restore_after_source_checkout_is_lost(self):
        path = self.ensure("lost")["trees"][0]["path"]
        self.write(os.path.join(path, "feature.txt"), "local commit\n")
        self.git(path, "add", "feature.txt")
        self.git(path, "commit", "-q", "-m", "unpushed work")
        self.write(os.path.join(path, "draft.txt"), "uncommitted\n")
        self.wsp("evict", "--task", "lost", "--holder", "cli:test")
        sh(["rm", "-rf", self.canon["app"]])
        err = self.wsp("ensure", "--task", "lost", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "SOURCE_MISSING")
        res = self.wsp("ensure", "--task", "lost", "--repo", "app", "--holder", "cli:test", "--reclone-source")
        tree = res["trees"][0]
        self.assertEqual(tree["action"], "restored")
        self.assertTrue(any("re-cloned" in w for w in res["warnings"] + tree.get("warnings", [])))
        with open(os.path.join(path, "feature.txt")) as fh:
            self.assertEqual(fh.read(), "local commit\n")
        with open(os.path.join(path, "draft.txt")) as fh:
            self.assertEqual(fh.read(), "uncommitted\n")
        self.assertEqual(self.git(path, "log", "-1", "--format=%s").stdout.strip(), "unpushed work")
        self.assertTrue(os.path.isdir(os.path.join(self.canon["app"], ".git")))

    def test_reclone_never_overwrites_a_non_repo_directory(self):
        self.ensure("occupied")
        self.wsp("evict", "--task", "occupied", "--holder", "cli:test")
        sh(["rm", "-rf", self.canon["app"]])
        self.write(os.path.join(self.canon["app"], "someone-elses.txt"), "keep\n")
        err = self.wsp("ensure", "--task", "occupied", "--repo", "app", "--holder", "cli:test",
                       "--reclone-source", ok=False)
        self.assertEqual(err["error"]["code"], "SOURCE_INVALID")
        self.assertTrue(os.path.exists(os.path.join(self.canon["app"], "someone-elses.txt")))


class QuotaTests(Sandbox):
    repos = ("app",)

    def test_checkpoint_subquota_blocks_growth_but_keeps_work(self):
        limits = dict(self.cfg["limits"], checkpoints_gib=0.000001)
        self.write_config(limits=limits)
        path = self.ensure("saved")["trees"][0]["path"]
        self.write(os.path.join(path, "work.txt"), "unpublished\n")
        ck = self.wsp("checkpoint", "--task", "saved")
        self.assertTrue(ck["trees"][0]["created"])
        self.assertTrue(ck["warnings"])
        err = self.wsp("ensure", "--task", "another", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(err["error"]["details"]["holders"][0]["task"], "saved")
        self.assertTrue(os.path.exists(os.path.join(path, "work.txt")))

    def test_dependency_cache_subquota_skips_caching(self):
        canon = self.canon["app"]
        self.write(os.path.join(canon, "package.json"), '{"name": "app"}\n')
        self.write(os.path.join(canon, "package-lock.json"), '{"lockfileVersion": 3}\n')
        self.git(canon, "add", "-A")
        self.git(canon, "commit", "-q", "-m", "pkg")
        self.git(canon, "push", "-q", "origin", "main")
        repos = self.cfg["repos"]
        repos["app"]["profile"]["packages"] = {".": {"install_command": "mkdir -p node_modules/x && echo 1 > node_modules/x/i.js"}}
        limits = dict(self.cfg["limits"], dependency_cache_gib=0.000001)
        self.write_config(repos=repos, limits=limits)
        self.ensure("dq")
        res = self.wsp("deps", "--task", "dq", "--repo", "app")
        self.assertEqual(res["mode"], "install")
        self.assertFalse(res["cached"])
        if disk.clone_mode(self.tmp) is None:
            # without copy-on-write (e.g. ext4) nothing is cached regardless of the quota
            self.assertIn("copy-on-write", res["cache_skip_reason"])
        else:
            self.assertIn("sub-quota", res["cache_skip_reason"])


class SetManifestTests(Sandbox):
    repos = ("app", "lib")

    def test_multi_repo_task_has_set_manifest(self):
        self.ensure("multi", "app", "lib")
        self.write(os.path.join(self.tree_path("multi", "lib"), "wip.txt"), "x\n")
        self.wsp("checkpoint", "--task", "multi")
        status = self.wsp("status", "--task", "multi")
        with open(status["set_manifest"]) as fh:
            data = json.load(fh)
        self.assertEqual(data["verify_order"], ["app", "lib"])
        by_repo = {t["repo"]: t for t in data["trees"]}
        self.assertEqual(by_repo["app"]["recoverable_from"], "branch")
        self.assertEqual(by_repo["lib"]["recoverable_from"], "checkpoint")
        self.assertEqual(data["incomplete"], [])
        # the set manifest is not mistaken for a tree manifest during registry rebuild
        self.wsp("registry", "rebuild")


class RunBudgetTests(Sandbox):
    repos = ("app",)

    def test_command_over_budget_is_stopped_and_checkpointed(self):
        self.ensure("budget")
        script = ("import os,time\n"
                  "f=open('big.bin','wb'); f.write(os.urandom(40_000_000)); f.flush(); os.fsync(f.fileno())\n"
                  "time.sleep(30)\n")
        started = time.monotonic()
        err = self.wsp("run", "--task", "budget", "--repo", "app", "--holder", "cli:test",
                       "--max-growth-gib", "0.01", "--", "python3", "-c", script,
                       ok=False, env={"WSP_RUN_POLL_SECONDS": "0.3"})
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(err["error"]["code"], "DISK_LOW")
        self.assertIn("budget", err["error"]["message"])
        status = self.wsp("status", "--task", "budget")
        self.assertIsNotNone(status["trees"][0]["last_checkpoint"])


class StaleLeaseTests(Sandbox):
    repos = ("app",)

    def test_expired_heartbeat_becomes_uncertain_but_still_protects(self):
        self.ensure("idle", holder="codex:old-thread")
        db = sqlite3.connect(os.path.join(self.state_dir, "registry.sqlite"))
        db.execute("UPDATE leases SET heartbeat_at='2020-01-01T00:00:00+00:00'")
        db.commit()
        db.close()
        plan = self.wsp("gc", "--apply")
        self.assertIn("mark_lease_uncertain", [p["action"] for p in plan["plan"]])
        status = self.wsp("status", "--task", "idle")
        self.assertEqual(status["trees"][0]["leases"][0]["state"], "uncertain")
        err = self.wsp("evict", "--task", "idle", "--holder", "cli:someone-else", ok=False)
        self.assertEqual(err["error"]["code"], "LEASE_HELD")
        self.assertTrue(os.path.isdir(self.tree_path("idle")))
        again = self.ensure("idle", holder="codex:old-thread")
        self.assertEqual(again["trees"][0]["lease"], "owner")
        status = self.wsp("status", "--task", "idle")
        self.assertEqual(status["trees"][0]["leases"][0]["state"], "held")


class OnUseJanitorTests(Sandbox):
    repos = ("app",)

    def _pause_and_age(self, task):
        path = self.ensure(task)["trees"][0]["path"]
        self.write(os.path.join(path, "wip.txt"), "wip\n")
        self.wsp("release", "--task", task, "--holder", "cli:test", "--pause")
        db = sqlite3.connect(os.path.join(self.state_dir, "registry.sqlite"))
        db.execute("UPDATE tasks SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (task,))
        db.commit()
        db.close()
        return path

    def _wait_gone(self, path, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not os.path.exists(path):
                return True
            time.sleep(0.3)
        return False

    def test_cleanup_runs_in_background_on_normal_use(self):
        self.write_config(janitor={"mode": "on-use", "interval_minutes": 0})
        path = self._pause_and_age("idle-task")
        self.wsp("list", env={"WSP_NO_JANITOR": ""})
        self.assertTrue(self._wait_gone(path), "paused task was not evicted in the background")
        last_file = os.path.join(self.state_dir, "janitor-last-run.json")
        deadline = time.monotonic() + 30
        last = {}
        while time.monotonic() < deadline:  # the run records its result after the eviction finishes
            with open(last_file) as fh:
                last = json.load(fh)
            if "actions" in last:
                break
            time.sleep(0.3)
        self.assertEqual(last["trigger"], "on-use")
        self.assertIn("evict", last["actions"])
        restored = self.ensure("idle-task")  # restorable with the unpublished file
        with open(os.path.join(restored["trees"][0]["path"], "wip.txt")) as fh:
            self.assertEqual(fh.read(), "wip\n")

    def test_interval_throttles_and_off_disables(self):
        self.write_config(janitor={"mode": "on-use", "interval_minutes": 60})
        last_file = os.path.join(self.state_dir, "janitor-last-run.json")
        os.makedirs(self.state_dir, exist_ok=True)
        with open(last_file, "w") as fh:
            json.dump({"started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}, fh)
        path = self._pause_and_age("throttled")
        self.wsp("list", env={"WSP_NO_JANITOR": ""})
        time.sleep(2)
        self.assertTrue(os.path.isdir(path))
        self.write_config(janitor={"mode": "off", "interval_minutes": 0})
        self.wsp("list", env={"WSP_NO_JANITOR": ""})
        time.sleep(2)
        self.assertTrue(os.path.isdir(path))
        status = self.wsp("janitor", "status")
        self.assertEqual(status["mode"], "off")


if __name__ == "__main__":
    unittest.main()
