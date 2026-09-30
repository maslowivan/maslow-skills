import os
import sqlite3
import stat
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Sandbox, sh  # noqa: E402


class EnsureTests(Sandbox):
    repos = ("app", "lib")

    def test_create_is_idempotent_and_isolated(self):
        first = self.ensure("t1")
        tree = first["trees"][0]
        path = self.tree_path("t1")
        self.assertEqual(tree["path"], path)
        self.assertEqual(tree["action"], "created")
        self.assertEqual(tree["branch"], "wsp/t1")
        self.assertTrue(os.path.isdir(path))
        # no upstream: a bare `git pull/push` can never touch main
        self.assertNotEqual(self.git(path, "rev-parse", "--abbrev-ref", "@{upstream}", check=False).returncode, 0)
        # locked registration protects against foreign prune
        listing = self.git(self.canon["app"], "worktree", "list", "--porcelain").stdout
        self.assertIn("locked wsp:t1", listing)
        # ownership marker lives in the private git dir, not in the working files
        gitdir = self.git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
        self.assertTrue(os.path.exists(os.path.join(gitdir, "wsp-owner.json")))
        self.assertEqual(self.git(path, "status", "--porcelain").stdout, "")
        # secret materialised from the canonical checkout, ignored by git
        with open(os.path.join(path, ".dev.vars")) as fh:
            self.assertEqual(fh.read(), "SECRET=from-canonical\n")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(path, ".dev.vars")).st_mode), 0o600)
        again = self.ensure("t1")
        self.assertEqual(again["trees"][0]["action"], "reused")
        self.assertEqual(again["trees"][0]["path"], path)

    def test_add_second_repo_to_existing_task(self):
        self.ensure("t2", "app")
        both = self.ensure("t2", "app", "lib")
        actions = {t["repo"]: t["action"] for t in both["trees"]}
        self.assertEqual(actions, {"app": "reused", "lib": "created"})
        self.assertTrue(os.path.isdir(self.tree_path("t2", "lib")))

    def test_source_checkout_is_not_touched(self):
        canon = self.canon["app"]
        self.git(canon, "checkout", "-q", "-b", "someone-else")
        self.write(os.path.join(canon, "a.txt"), "dirty\n")
        self.write(os.path.join(canon, "untracked.txt"), "u\n")
        before = (self.git(canon, "status", "--porcelain").stdout,
                  self.git(canon, "rev-parse", "--abbrev-ref", "HEAD").stdout)
        self.ensure("t3")
        after = (self.git(canon, "status", "--porcelain").stdout,
                 self.git(canon, "rev-parse", "--abbrev-ref", "HEAD").stdout)
        self.assertEqual(before, after)
        with open(os.path.join(self.tree_path("t3"), "a.txt")) as fh:
            self.assertEqual(fh.read(), "alpha\n")

    def test_branch_conflicts(self):
        self.git(self.canon["app"], "branch", "wsp/t4")
        err = self.wsp("ensure", "--task", "t4", "--repo", "app", ok=False)
        self.assertEqual(err["error"]["code"], "BRANCH_EXISTS")
        other = os.path.join(self.tmp, "other")
        sh(["git", "clone", "-q", self.origins["app"], other], env=self.env)
        self.git(other, "checkout", "-q", "-b", "wsp/t5")
        self.write(os.path.join(other, "c.txt"), "c\n")
        self.git(other, "add", "c.txt")
        self.git(other, "commit", "-q", "-m", "remote work")
        self.git(other, "push", "-q", "origin", "wsp/t5")
        err = self.wsp("ensure", "--task", "t5", "--repo", "app", ok=False)
        self.assertEqual(err["error"]["code"], "BRANCH_EXISTS")
        res = self.wsp("ensure", "--task", "t5", "--repo", "app", "--from-remote-branch")
        self.assertTrue(os.path.exists(os.path.join(res["trees"][0]["path"], "c.txt")))

    def test_invalid_ids_rejected(self):
        for bad in ("../x", "A B", "-x", "x--y", "a" * 60):
            err = self.wsp("ensure", f"--task={bad}", "--repo", "app", ok=False)
            self.assertEqual(err["error"]["code"], "INVALID_ID", bad)

    def test_parallel_ensure_same_task(self):
        cmd = ["python3", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "skills", "task-workspaces", "scripts", "wsp.py"),
               "ensure", "--task", "par", "--repo", "app", "--holder", "cli:test", "--json"]
        procs = [subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        outputs = [p.communicate() for p in procs]
        codes = [p.returncode for p in procs]
        self.assertEqual(codes, [0, 0, 0], outputs)
        listing = self.git(self.canon["app"], "worktree", "list").stdout
        self.assertEqual(listing.count(self.tree_path("par")), 1)


class RollbackTests(Sandbox):
    repos = ("app",)

    def test_partial_failure_rolls_back_new_pristine_trees(self):
        cfg = dict(self.cfg)
        cfg["repos"]["broken"] = {"path": os.path.join(self.tmp, "nope"), "profile": {}}
        self.write_config(repos=cfg["repos"])
        err = self.wsp("ensure", "--task", "rb", "--repo", "app", "--repo", "broken", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "SOURCE_MISSING")
        self.assertEqual(err["error"]["details"]["rolled_back"], ["app"])
        self.assertFalse(os.path.exists(self.tree_path("rb")))
        self.assertNotEqual(self.git(self.canon["app"], "show-ref", "--verify", "refs/heads/wsp/rb", check=False).returncode, 0)


class CheckpointTests(Sandbox):
    repos = ("app",)

    def _mess_up(self, path):
        # local-only commit
        self.write(os.path.join(path, "committed.txt"), "local commit\n")
        self.git(path, "add", "committed.txt")
        self.git(path, "commit", "-q", "-m", "local only")
        # staged and unstaged change of the same file
        self.write(os.path.join(path, "a.txt"), "alpha staged\n")
        self.git(path, "add", "a.txt")
        self.write(os.path.join(path, "a.txt"), "alpha staged\nplus unstaged\n")
        # rename + delete + binary + exec bit + symlink + untracked + preserved ignored + secret
        self.git(path, "mv", "b.txt", "b-renamed.txt")
        self.git(path, "rm", "-q", "README.md")
        self.write(os.path.join(path, "bin.dat"), bytes(range(256)) * 4)
        self.write(os.path.join(path, "run.sh"), "#!/bin/sh\necho hi\n", mode=0o755)
        os.symlink("a.txt", os.path.join(path, "link-to-a"))
        self.write(os.path.join(path, "notes", "untracked.md"), "draft\n")
        self.write(os.path.join(path, "local.cfg"), "keep me\n")
        self.write(os.path.join(path, ".dev.vars"), "SECRET=changed-in-tree\n")

    def _snapshot(self, path):
        files = {}
        for base, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in names + [d for d in dirs if os.path.islink(os.path.join(base, d))]:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, path)
                if rel == ".git":
                    continue
                if os.path.islink(full):
                    files[rel] = ("link", os.readlink(full))
                else:
                    with open(full, "rb") as fh:
                        files[rel] = ("file", fh.read(), stat.S_IMODE(os.stat(full).st_mode) & 0o111)
        return {"files": files,
                "status": self.git(path, "status", "--porcelain=v1", "--untracked-files=all").stdout,
                "head": self.git(path, "rev-parse", "HEAD").stdout.strip(),
                "diff_cached": self.git(path, "diff", "--cached").stdout,
                "diff": self.git(path, "diff").stdout}

    def test_evict_restore_roundtrip(self):
        res = self.ensure("ck")
        path = res["trees"][0]["path"]
        self._mess_up(path)
        before = self._snapshot(path)
        evicted = self.wsp("evict", "--task", "ck", "--holder", "cli:test")
        self.assertEqual(evicted["trees"][0]["state"], "evicted")
        self.assertIsNotNone(evicted["trees"][0]["checkpoint"])
        self.assertFalse(os.path.exists(path))
        # secrets never enter checkpoint objects or archives
        refs = self.git(self.canon["app"], "for-each-ref", "--format=%(refname)", "refs/wsp/").stdout.split()
        self.assertEqual(len(refs), 1)
        listing = self.git(self.canon["app"], "ls-tree", "-r", "--name-only", refs[0]).stdout.split()
        self.assertNotIn(".dev.vars", listing)
        self.assertIn("notes/untracked.md", listing)
        restored = self.ensure("ck")
        self.assertEqual(restored["trees"][0]["action"], "restored")
        after = self._snapshot(path)
        # the secret is re-materialised from its source, not from the checkpoint
        self.assertEqual(after["files"].pop(".dev.vars")[1], b"SECRET=from-canonical\n")
        before["files"].pop(".dev.vars")
        self.assertEqual(before, after)

    def test_clean_pushed_tree_evicts_without_checkpoint(self):
        path = self.ensure("pushed")["trees"][0]["path"]
        self.write(os.path.join(path, "feature.txt"), "f\n")
        self.git(path, "add", "feature.txt")
        self.git(path, "commit", "-q", "-m", "feature")
        self.git(path, "push", "-q", "origin", "wsp/pushed")
        head = self.git(path, "rev-parse", "HEAD").stdout.strip()
        evicted = self.wsp("evict", "--task", "pushed", "--holder", "cli:test")
        self.assertIsNone(evicted["trees"][0]["checkpoint"])
        self.assertEqual(self.git(self.canon["app"], "for-each-ref", "refs/wsp/").stdout, "")
        restored = self.ensure("pushed")
        self.assertEqual(restored["trees"][0]["head"], head)

    def test_unexpected_deletion_and_foreign_prune(self):
        path = self.ensure("gone")["trees"][0]["path"]
        self.write(os.path.join(path, "work.txt"), "important\n")
        self.wsp("checkpoint", "--task", "gone")
        sh(["rm", "-rf", path])
        self.git(self.canon["app"], "worktree", "prune")  # somebody else's cleanup
        self.assertIn(path, self.git(self.canon["app"], "worktree", "list").stdout)  # lock kept it
        status = self.wsp("status", "--task", "gone")
        self.assertEqual(status["trees"][0]["state"], "missing")
        self.assertIn("checkpoint", status["trees"][0]["recoverable_to"])
        restored = self.ensure("gone")
        self.assertEqual(restored["trees"][0]["action"], "restored")
        with open(os.path.join(path, "work.txt")) as fh:
            self.assertEqual(fh.read(), "important\n")

    def test_restore_refuses_occupied_path(self):
        path = self.ensure("occ")["trees"][0]["path"]
        self.wsp("evict", "--task", "occ", "--holder", "cli:test")
        self.write(os.path.join(path, "foreign.txt"), "not ours\n")
        err = self.wsp("ensure", "--task", "occ", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "RESTORE_CONFLICT")
        self.assertTrue(os.path.exists(os.path.join(path, "foreign.txt")))

    def test_unclassified_ignored_blocks_evict(self):
        path = self.ensure("unc")["trees"][0]["path"]
        self.write(os.path.join(path, "data.weird"), "?\n")
        err = self.wsp("evict", "--task", "unc", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "UNCLASSIFIED_IGNORED")
        self.assertTrue(os.path.exists(path))
        self.wsp("evict", "--task", "unc", "--holder", "cli:test", "--accept-unclassified")
        self.assertFalse(os.path.exists(path))

    def test_running_process_blocks_evict(self):
        path = self.ensure("busy")["trees"][0]["path"]
        proc = subprocess.Popen(["sleep", "30"], cwd=path)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.5)
        err = self.wsp("evict", "--task", "busy", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "PROCESS_RUNNING")
        self.assertTrue(os.path.isdir(path))

    def test_other_lease_blocks_evict(self):
        self.ensure("lease", holder="codex:thread-1")
        err = self.wsp("evict", "--task", "lease", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "LEASE_HELD")
        self.wsp("release", "--task", "lease", "--holder", "codex:thread-1")
        self.wsp("evict", "--task", "lease", "--holder", "cli:test")

    def test_pin_blocks_evict(self):
        self.ensure("pinned")
        self.wsp("pin", "--task", "pinned")
        err = self.wsp("evict", "--task", "pinned", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "PINNED")


class CloseTests(Sandbox):
    repos = ("app",)

    def test_close_needs_discard_for_unpushed_work_and_detects_squash(self):
        path = self.ensure("sq")["trees"][0]["path"]
        self.write(os.path.join(path, "feature.txt"), "feature\n")
        self.git(path, "add", "feature.txt")
        self.git(path, "commit", "-q", "-m", "feature work")
        err = self.wsp("close", "--task", "sq", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "UNIQUE_STATE")
        # the same change lands on main through a squash merge by someone else
        other = os.path.join(self.tmp, "merger")
        sh(["git", "clone", "-q", self.origins["app"], other], env=self.env)
        self.write(os.path.join(other, "feature.txt"), "feature\n")
        self.git(other, "add", "feature.txt")
        self.git(other, "commit", "-q", "-m", "Feature (#1)")
        self.git(other, "push", "-q", "origin", "main")
        self.git(self.canon["app"], "fetch", "-q", "origin")
        closed = self.wsp("close", "--task", "sq", "--holder", "cli:test")
        self.assertEqual(closed["status"], "completed")
        self.assertFalse(os.path.exists(path))
        self.assertNotEqual(self.git(self.canon["app"], "show-ref", "--verify", "refs/heads/wsp/sq",
                                     check=False).returncode, 0)

    def test_discard_after_confirmation(self):
        path = self.ensure("disc")["trees"][0]["path"]
        self.write(os.path.join(path, "x.txt"), "x\n")
        self.wsp("close", "--task", "disc", "--status", "cancelled", "--holder", "cli:test", ok=False)
        self.wsp("close", "--task", "disc", "--status", "cancelled", "--discard", "--holder", "cli:test")
        self.assertFalse(os.path.exists(path))


class GcTests(Sandbox):
    repos = ("app",)

    def _age_task(self, task, hours):
        db = sqlite3.connect(os.path.join(self.state_dir, "registry.sqlite"))
        db.execute("UPDATE tasks SET updated_at=datetime('now', ?) || '+00:00' WHERE id=?",
                   (f"-{hours} hours", task))
        db.commit()
        db.close()

    def test_paused_task_is_evicted_after_threshold_and_restorable(self):
        path = self.ensure("pz")["trees"][0]["path"]
        self.write(os.path.join(path, "wip.txt"), "wip\n")
        self.wsp("release", "--task", "pz", "--holder", "cli:test", "--pause")
        plan = self.wsp("gc")
        self.assertNotIn("evict", [p["action"] for p in plan["plan"]])
        self._age_task("pz", 30)
        applied = self.wsp("gc", "--apply")
        self.assertIn("evict", [p["action"] for p in applied["plan"]])
        self.assertFalse(os.path.exists(path))
        self.ensure("pz")
        with open(os.path.join(path, "wip.txt")) as fh:
            self.assertEqual(fh.read(), "wip\n")

    def test_old_in_progress_task_is_only_reviewed(self):
        path = self.ensure("old")["trees"][0]["path"]
        db = sqlite3.connect(os.path.join(self.state_dir, "registry.sqlite"))
        db.execute("UPDATE trees SET updated_at='2020-01-01T00:00:00+00:00'")
        db.execute("UPDATE leases SET heartbeat_at='2020-01-01T00:00:00+00:00'")
        db.commit()
        db.close()
        applied = self.wsp("gc", "--apply")
        self.assertIn("review", [p["action"] for p in applied["plan"]])
        self.assertTrue(os.path.isdir(path))

    def test_pinned_is_kept(self):
        self.ensure("keep")
        self.wsp("release", "--task", "keep", "--holder", "cli:test", "--pause")
        self.wsp("pin", "--task", "keep")
        self._age_task("keep", 100)
        applied = self.wsp("gc", "--apply")
        self.assertTrue(os.path.isdir(self.tree_path("keep")))
        self.assertIn({"action": "keep", "task": "keep", "why": "pinned"}, applied["plan"])


class SafetyTests(Sandbox):
    repos = ("app",)

    def test_symlinked_root_is_rejected(self):
        real = os.path.join(self.tmp, "real-root")
        os.makedirs(real)
        link = os.path.join(self.tmp, "link-root")
        os.symlink(real, link)
        self.write_config(worktrees_root=link)
        err = self.wsp("ensure", "--task", "s1", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "PATH_UNSAFE")

    def test_node_modules_above_root_is_rejected(self):
        base = os.path.join(self.tmp, "polluted")
        os.makedirs(os.path.join(base, "node_modules"))
        self.write_config(worktrees_root=os.path.join(base, "tasks"))
        err = self.wsp("ensure", "--task", "s2", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "PATH_UNSAFE")

    def test_origin_mismatch(self):
        repos = self.cfg["repos"]
        repos["app"]["origin"] = "git@github.com:someone/else.git"
        self.write_config(repos=repos)
        err = self.wsp("ensure", "--task", "s3", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "ORIGIN_MISMATCH")


class RunAndRegistryTests(Sandbox):
    repos = ("app",)

    def test_run_uses_tree_cwd_and_propagates_exit_code(self):
        path = self.ensure("run")["trees"][0]["path"]
        proc = self.wsp("run", "--task", "run", "--repo", "app", "--holder", "cli:test", "--",
                        "sh", "-c", "pwd > where.txt; exit 3", raw=True)
        self.assertEqual(proc.returncode, 3)
        with open(os.path.join(path, "where.txt")) as fh:
            self.assertEqual(os.path.realpath(fh.read().strip()), path)

    def test_registry_rebuild_from_manifests(self):
        path = self.ensure("rebuild")["trees"][0]["path"]
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(self.state_dir, "registry.sqlite" + suffix)
            if os.path.exists(p):
                os.unlink(p)
        result = self.wsp("registry", "rebuild")
        self.assertIn("rebuild", result["tasks"])
        status = self.wsp("status", "--task", "rebuild")
        self.assertEqual(status["trees"][0]["state"], "ready")
        self.assertEqual(status["trees"][0]["path"], path)

    def test_inventory_is_read_only_and_classifies(self):
        self.ensure("inv")
        foreign = os.path.join(self.tmp, "foreign-tree")
        self.git(self.canon["app"], "worktree", "add", "-q", "--detach", foreign)
        before = self.git(self.canon["app"], "worktree", "list", "--porcelain").stdout
        inv = self.wsp("inventory")
        owners = {t["path"]: t["owner"] for t in inv["repos"][0]["trees"]}
        self.assertEqual(owners[self.tree_path("inv")], "wsp")
        self.assertEqual(owners[foreign], "unowned")
        self.assertEqual(before, self.git(self.canon["app"], "worktree", "list", "--porcelain").stdout)


if __name__ == "__main__":
    unittest.main()
