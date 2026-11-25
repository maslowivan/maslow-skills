"""Cleanup policy: merged pull requests, idle tasks, and making room before `ensure` fails."""

import json
import os
import sqlite3
import stat
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Sandbox, sh  # noqa: E402

FAKE_GH = """#!/bin/sh
# prints the canned `gh pr list` answer for the branch given after --head
branch=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--head" ]; then branch="$2"; fi
  shift
done
file="$WSP_TEST_GH_DIR/$(printf %s "$branch" | tr / _).json"
echo "$*$branch" >> "$WSP_TEST_GH_DIR/calls.log"
if [ -f "$file" ]; then cat "$file"; else echo "[]"; fi
"""

OLD = "2020-01-01T00:00:00+00:00"


class CleanupSandbox(Sandbox):
    repos = ("app",)

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        self.gh_dir = os.path.join(self.tmp, "gh")
        os.makedirs(bindir)
        os.makedirs(self.gh_dir)
        gh = os.path.join(bindir, "gh")
        self.write(gh, FAKE_GH, mode=stat.S_IRWXU)
        self.env["PATH"] = bindir + os.pathsep + self.env.get("PATH", "")
        self.env["WSP_TEST_GH_DIR"] = self.gh_dir
        self.configure()

    def configure(self, **overrides):
        self.write_config(**overrides)
        self.cfg["repos"]["app"]["github"] = "acme/app"
        with open(self.env["WSP_CONFIG"], "w") as fh:
            json.dump(self.cfg, fh)

    def prs(self, branch, *rows):
        self.write(os.path.join(self.gh_dir, branch.replace("/", "_") + ".json"), json.dumps(list(rows)))

    def gh_calls(self):
        path = os.path.join(self.gh_dir, "calls.log")
        if not os.path.exists(path):
            return 0
        with open(path) as fh:
            return len(fh.read().splitlines())

    def commit(self, path, filename, content, push=True):
        self.write(os.path.join(path, filename), content)
        self.git(path, "add", filename)
        self.git(path, "commit", "-q", "-m", f"change {filename}")
        if push:
            self.git(path, "push", "-q", "origin", "HEAD")
        return self.git(path, "rev-parse", "HEAD").stdout.strip()

    def push_to_main(self, filename, content):
        other = os.path.join(self.tmp, "merger")
        if not os.path.isdir(other):
            sh(["git", "clone", "-q", self.origins["app"], other], env=self.env)
        self.git(other, "pull", "-q", "origin", "main")
        self.write(os.path.join(other, filename), content)
        self.git(other, "add", filename)
        self.git(other, "commit", "-q", "-m", f"main: {filename}")
        self.git(other, "push", "-q", "origin", "main")

    def squash_then_rewrite(self, filename):
        """The pull request is squash-merged, then main changes the same lines again:
        from now on merging the branch into main conflicts."""
        self.push_to_main(filename, "feature\n")
        self.push_to_main(filename, "rewritten later on main\n")

    def age(self, task, path=None):
        """Make the task look untouched for years: registry timestamps and the tree's own files."""
        db = sqlite3.connect(os.path.join(self.state_dir, "registry.sqlite"))
        db.execute("UPDATE sessions SET last_seen=? WHERE task_id=?", (OLD, task))
        db.execute("UPDATE trees SET created_at=? WHERE task_id=?", (OLD, task))
        db.execute("UPDATE leases SET heartbeat_at=?, released_at=CASE WHEN released_at IS NULL THEN NULL ELSE ? END"
                   " WHERE tree_id IN (SELECT id FROM trees WHERE task_id=?)", (OLD, OLD, task))
        db.commit()
        db.close()
        if path:
            admin = self.git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
            targets = [os.path.join(admin, n) for n in ("index", "HEAD", "ORIG_HEAD", os.path.join("logs", "HEAD"))]
            for root, _, files in os.walk(path):
                targets += [os.path.join(root, f) for f in files]
            for target in targets:
                if os.path.exists(target):
                    os.utime(target, (1577836800, 1577836800))

    def actions(self, result, task):
        return [p for p in result["plan"] if p.get("task") == task and p["action"] != "mark_lease_uncertain"]


class MergedPullRequestTests(CleanupSandbox):
    def test_merged_pull_request_closes_task_when_main_changed_the_same_lines(self):
        path = self.ensure("pr")["trees"][0]["path"]
        head = self.commit(path, "feature.txt", "feature\n")
        self.squash_then_rewrite("feature.txt")
        self.wsp("release", "--task", "pr", "--holder", "cli:test")
        self.assertEqual(self.actions(self.wsp("gc", "--apply"), "pr"), [])   # GitHub knows no pull request
        self.assertTrue(os.path.isdir(path))
        self.prs("wsp/pr", {"number": 7, "state": "MERGED", "headRefOid": head, "baseRefName": "main"})
        applied = self.wsp("gc", "--apply")
        self.assertEqual(self.actions(applied, "pr"), [
            {"action": "close_merged", "task": "pr",
             "why": "pull request #7 was merged into the default branch"}])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.wsp("status", "--task", "pr")["task"]["status"], "completed")

    def test_commits_made_after_the_merged_head_keep_the_task(self):
        path = self.ensure("more")["trees"][0]["path"]
        head = self.commit(path, "feature.txt", "feature\n")
        self.commit(path, "extra.txt", "not in the pull request\n", push=False)
        self.squash_then_rewrite("feature.txt")
        self.prs("wsp/more", {"number": 8, "state": "MERGED", "headRefOid": head, "baseRefName": "main"})
        self.wsp("release", "--task", "more", "--holder", "cli:test")
        applied = self.wsp("gc", "--apply")
        self.assertNotIn("close_merged", [p["action"] for p in applied["plan"]])
        self.assertTrue(os.path.isfile(os.path.join(path, "extra.txt")))

    def test_pull_request_merged_into_another_branch_does_not_count(self):
        path = self.ensure("stacked")["trees"][0]["path"]
        head = self.commit(path, "feature.txt", "feature\n")
        self.prs("wsp/stacked", {"number": 9, "state": "MERGED", "headRefOid": head, "baseRefName": "release"})
        self.wsp("release", "--task", "stacked", "--holder", "cli:test")
        applied = self.wsp("gc", "--apply")
        self.assertNotIn("close_merged", [p["action"] for p in applied["plan"]])
        self.assertTrue(os.path.isdir(path))

    def test_local_head_behind_the_merged_head_closes_task(self):
        path = self.ensure("behind")["trees"][0]["path"]
        self.commit(path, "feature.txt", "feature\n")
        # a reviewer pushed one more commit to the branch before the merge
        other = os.path.join(self.tmp, "reviewer")
        sh(["git", "clone", "-q", "-b", "wsp/behind", self.origins["app"], other], env=self.env)
        pr_head = self.commit(other, "review.txt", "fix\n")
        self.git(self.origins["app"], "update-ref", "refs/pull/10/head", pr_head)
        self.squash_then_rewrite("feature.txt")
        self.prs("wsp/behind", {"number": 10, "state": "MERGED", "headRefOid": pr_head, "baseRefName": "main"})
        self.wsp("release", "--task", "behind", "--holder", "cli:test")
        applied = self.wsp("gc", "--apply")
        self.assertEqual([p["action"] for p in self.actions(applied, "behind")], ["close_merged"])
        self.assertFalse(os.path.exists(path))

    def test_fresh_task_does_not_ask_github_and_lookup_can_be_turned_off(self):
        self.ensure("fresh")
        self.wsp("release", "--task", "fresh", "--holder", "cli:test")
        self.wsp("gc", "--apply")
        self.assertEqual(self.gh_calls(), 0)
        path = self.ensure("off")["trees"][0]["path"]
        head = self.commit(path, "feature.txt", "feature\n")
        self.squash_then_rewrite("feature.txt")
        self.prs("wsp/off", {"number": 11, "state": "MERGED", "headRefOid": head, "baseRefName": "main"})
        self.wsp("release", "--task", "off", "--holder", "cli:test")
        self.configure(policy={"pr_lookup": False})
        self.wsp("gc", "--apply")
        self.assertEqual(self.gh_calls(), 0)
        self.assertTrue(os.path.isdir(path))

    def test_closed_pull_request_is_reported_not_removed(self):
        path = self.ensure("dropped")["trees"][0]["path"]
        head = self.commit(path, "feature.txt", "feature\n")
        self.prs("wsp/dropped", {"number": 12, "state": "CLOSED", "headRefOid": head, "baseRefName": "main"})
        self.wsp("release", "--task", "dropped", "--holder", "cli:test")
        applied = self.wsp("gc", "--apply")
        entry = self.actions(applied, "dropped")[0]
        self.assertEqual(entry["action"], "review")
        self.assertIn("#12 was closed without merge", entry["why"])
        self.assertIn("wsp close --task dropped --status cancelled", entry["why"])
        self.assertTrue(os.path.isdir(path))


class IdleTaskTests(CleanupSandbox):
    def test_idle_task_is_evicted_with_its_unpublished_work_and_restored(self):
        path = self.ensure("idle", holder="codex:gone")["trees"][0]["path"]   # never released
        self.commit(path, "wip.txt", "committed, not pushed\n", push=False)
        self.write(os.path.join(path, "notes.txt"), "uncommitted\n")
        self.age("idle", path)
        plan = self.wsp("gc")
        self.assertEqual([p["action"] for p in self.actions(plan, "idle")], ["evict_idle"])
        self.assertTrue(os.path.isdir(path))                                   # a dry run removes nothing
        applied = self.wsp("gc", "--apply")
        self.assertNotIn("result", self.actions(applied, "idle")[0])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(applied["disk"]["tasks_with_trees"], 0)
        self.ensure("idle", holder="codex:gone")
        for name, content in (("wip.txt", "committed, not pushed\n"), ("notes.txt", "uncommitted\n")):
            with open(os.path.join(path, name)) as fh:
                self.assertEqual(fh.read(), content)

    def test_work_in_the_tree_without_wsp_calls_counts_as_activity(self):
        path = self.ensure("quiet")["trees"][0]["path"]
        self.age("quiet", path)
        self.write(os.path.join(path, "today.txt"), "edited by an agent that never called wsp again\n")
        applied = self.wsp("gc", "--apply")
        self.assertEqual(self.actions(applied, "quiet"), [])
        self.assertTrue(os.path.isdir(path))

    def test_pinned_and_disabled_policy_keep_idle_trees(self):
        pinned = self.ensure("pinned")["trees"][0]["path"]
        self.wsp("pin", "--task", "pinned")
        self.age("pinned", pinned)
        self.wsp("gc", "--apply")
        self.assertTrue(os.path.isdir(pinned))
        kept = self.ensure("kept")["trees"][0]["path"]
        self.age("kept", kept)
        self.configure(policy={"idle_evict_after_days": 0})
        applied = self.wsp("gc", "--apply")
        self.assertEqual([p["action"] for p in self.actions(applied, "kept")], ["review"])
        self.assertTrue(os.path.isdir(kept))


class MakeRoomTests(CleanupSandbox):
    def limit(self, tasks):
        limits = dict(self.cfg["limits"], max_tasks_with_trees=tasks)
        self.configure(limits=limits)

    def test_ensure_cleans_up_before_refusing_a_new_task(self):
        self.limit(1)
        old = self.ensure("old", holder="codex:gone")["trees"][0]["path"]
        self.age("old", old)
        created = self.ensure("new")
        self.assertTrue(os.path.isdir(created["trees"][0]["path"]))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(any("LIMIT_REACHED: ran the cleanup policy first (evict_idle old)" in w
                            for w in created["warnings"]), created["warnings"])

    def test_refusal_lists_tasks_that_can_be_evicted(self):
        self.limit(1)
        self.ensure("busy")
        self.wsp("release", "--task", "busy", "--holder", "cli:test")
        err = self.wsp("ensure", "--task", "second", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "LIMIT_REACHED")
        self.assertIn("only when the user asks", err["error"]["message"])
        self.assertEqual([c["task"] for c in err["error"]["details"]["idle_tasks"]], ["busy"])
        self.assertTrue(os.path.isdir(self.tree_path("busy")))

    def test_cleanup_mode_off_does_not_clean_on_ensure(self):
        self.limit(1)
        old = self.ensure("old", holder="codex:gone")["trees"][0]["path"]
        self.age("old", old)
        self.configure(limits=dict(self.cfg["limits"]), janitor={"mode": "off"})
        err = self.wsp("ensure", "--task", "new", "--repo", "app", "--holder", "cli:test", ok=False)
        self.assertEqual(err["error"]["code"], "LIMIT_REACHED")
        self.assertTrue(os.path.isdir(old))


if __name__ == "__main__":
    unittest.main()
