import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Sandbox, sh  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "skills", "task-workspaces", "scripts"))
from wsp_core import disk  # noqa: E402

INSTALL = ("mkdir -p node_modules/dep && printf v1 > node_modules/dep/index.js && "
           "ln -s ../pkgs/local node_modules/local")


class DepsTests(Sandbox):
    repos = ("app",)

    def setUp(self):
        super().setUp()
        canon = self.canon["app"]
        self.write(os.path.join(canon, "package.json"), '{"name": "app", "version": "1.0.0"}\n')
        self.write(os.path.join(canon, "package-lock.json"), '{"lockfileVersion": 3}\n')
        self.write(os.path.join(canon, "pkgs", "local", "index.js"), "module.exports = 'canonical'\n")
        self.git(canon, "add", "-A")
        self.git(canon, "commit", "-q", "-m", "packages")
        self.git(canon, "push", "-q", "origin", "main")
        repos = self.cfg["repos"]
        repos["app"]["profile"]["packages"] = {".": {"install_command": INSTALL}}
        self.write_config(repos=repos)
        self.clone = disk.clone_mode(self.tmp)

    def test_clone_reuse_is_isolated_and_reads_current_tree(self):
        p1 = self.ensure("d1")["trees"][0]["path"]
        first = self.wsp("deps", "--task", "d1", "--repo", "app")
        self.assertEqual(first["mode"], "install")
        if not self.clone:
            self.skipTest("no copy-on-write on this volume")
        self.assertTrue(first["cached"])
        p2 = self.ensure("d2")["trees"][0]["path"]
        second = self.wsp("deps", "--task", "d2", "--repo", "app")
        self.assertEqual(second["mode"], "clone")
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        # writes stay inside one tree
        with open(os.path.join(p2, "node_modules", "dep", "index.js"), "w") as fh:
            fh.write("patched")
        with open(os.path.join(p1, "node_modules", "dep", "index.js")) as fh:
            self.assertEqual(fh.read(), "v1")
        # workspace links resolve into the current task's code, not the cache or another tree
        self.assertEqual(os.path.realpath(os.path.join(p2, "node_modules", "local")),
                         os.path.realpath(os.path.join(p2, "pkgs", "local")))
        # changed inputs are detected before running anything
        with open(os.path.join(p2, "package-lock.json"), "w") as fh:
            fh.write('{"lockfileVersion": 3, "changed": true}\n')
        check = self.wsp("deps", "--task", "d2", "--repo", "app", "--check")
        self.assertEqual(check["dependencies"][0]["state"], "stale")
        err = self.wsp("run", "--task", "d2", "--repo", "app", "--holder", "cli:test", "--", "true", ok=False)
        self.assertEqual(err["error"]["code"], "DEPS_INCOMPATIBLE")
        again = self.wsp("deps", "--task", "d2", "--repo", "app")
        self.assertEqual(again["mode"], "install")
        self.assertNotEqual(again["fingerprint"], first["fingerprint"])

    def test_absolute_links_are_not_cached(self):
        repos = self.cfg["repos"]
        repos["app"]["profile"]["packages"] = {".": {
            "install_command": 'mkdir -p node_modules && ln -s "$PWD/pkgs/local" node_modules/local'}}
        self.write_config(repos=repos)
        self.ensure("abs")
        res = self.wsp("deps", "--task", "abs", "--repo", "app")
        self.assertEqual(res["mode"], "install")
        self.assertFalse(res["cached"])

    def test_evict_restore_reprepares_dependencies(self):
        path = self.ensure("dr")["trees"][0]["path"]
        self.wsp("deps", "--task", "dr", "--repo", "app")
        self.wsp("evict", "--task", "dr", "--holder", "cli:test")
        restored = self.ensure("dr")
        self.assertTrue(os.path.exists(os.path.join(path, "node_modules", "dep", "index.js")))
        self.assertEqual(restored["trees"][0]["action"], "restored")


class SharedProfileTrustTests(Sandbox):
    repos = ("app",)

    def _commit_shared_profile(self):
        canon = self.canon["app"]
        self.write(os.path.join(canon, ".wsp", "profile.json"), json.dumps({
            "secrets": [{"path": ".env.local", "source": "command:echo X=1"}],
            "packages": {".": {"install_command": "touch INSTALL_RAN"}},
        }))
        self.git(canon, "add", "-A")
        self.git(canon, "commit", "-q", "-m", "shared profile")
        self.git(canon, "push", "-q", "origin", "main")

    def test_repo_profile_commands_are_ignored_unless_trusted(self):
        self._commit_shared_profile()
        path = self.ensure("untrusted")["trees"][0]["path"]
        self.assertFalse(os.path.exists(os.path.join(path, ".env.local")))
        repos = self.cfg["repos"]
        repos["app"]["trust_shared_commands"] = True
        self.write_config(repos=repos)
        path2 = self.ensure("trusted")["trees"][0]["path"]
        with open(os.path.join(path2, ".env.local")) as fh:
            self.assertEqual(fh.read(), "X=1\n")


class ClaudeHookTests(Sandbox):
    repos = ("app", "other")

    def setUp(self):
        super().setUp()
        repos = self.cfg["repos"]
        repos.pop("other")  # "other" is a repository wsp does not manage
        self.write_config(repos=repos)

    def hook(self, name, event, env=None):
        return self.wsp("hook", "claude", name, raw=True, input=json.dumps(event), env=env)

    def head(self, repo):
        return self.git(self.canon[repo], "rev-parse", "HEAD").stdout.strip()

    def test_managed_repo_create_and_clean_remove(self):
        proc = self.hook("worktree-create", {"session_id": "s1", "cwd": self.canon["app"],
                                             "parent_worktree_path": self.canon["app"],
                                             "branch": "feature-x", "commit": self.head("app")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = proc.stdout.strip()
        self.assertEqual(path, self.tree_path("feature-x"))
        self.assertTrue(os.path.isdir(path))
        status = self.wsp("status", "--task", "feature-x")
        self.assertIn("claude:s1", [l["holder"] for l in status["trees"][0]["leases"]])
        proc = self.hook("worktree-remove", {"session_id": "s1", "worktree_path": path})
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(path))  # clean tree evicted, restorable

    def test_remove_with_unsaved_work_keeps_tree_and_checkpoints(self):
        path = self.hook("worktree-create", {"session_id": "s2", "parent_worktree_path": self.canon["app"],
                                             "branch": "wip", "commit": self.head("app")}).stdout.strip()
        self.write(os.path.join(path, "unsaved.txt"), "do not lose me\n")
        proc = self.hook("worktree-remove", {"session_id": "s2", "worktree_path": path})
        self.assertEqual(proc.returncode, 0)  # never non-zero: Claude would rm -rf
        self.assertTrue(os.path.exists(os.path.join(path, "unsaved.txt")))
        status = self.wsp("status", "--task", "wip")
        self.assertIsNotNone(status["trees"][0]["last_checkpoint"])

    def test_unmanaged_repo_gets_standard_behaviour(self):
        other = self.canon["other"]
        proc = self.hook("worktree-create", {"session_id": "s3", "parent_worktree_path": other,
                                             "branch": "try-it", "commit": self.head("other")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = proc.stdout.strip()
        self.assertEqual(path, os.path.join(other, ".claude", "worktrees", "try-it"))
        self.write(os.path.join(path, "dirty.txt"), "x\n")
        self.assertEqual(self.hook("worktree-remove", {"session_id": "s3", "worktree_path": path}).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(path, "dirty.txt")))
        os.unlink(os.path.join(path, "dirty.txt"))
        self.assertEqual(self.hook("worktree-remove", {"session_id": "s3", "worktree_path": path}).returncode, 0)
        self.assertFalse(os.path.exists(path))

    def test_remove_never_fails_even_with_broken_config(self):
        path = self.hook("worktree-create", {"session_id": "s4", "parent_worktree_path": self.canon["app"],
                                             "branch": "cfg", "commit": self.head("app")}).stdout.strip()
        with open(self.env["WSP_CONFIG"], "w") as fh:
            fh.write("{broken")
        proc = self.hook("worktree-remove", {"session_id": "s4", "worktree_path": path})
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.isdir(path))

    def test_session_start_context_and_end_release(self):
        path = self.hook("worktree-create", {"session_id": "s5", "parent_worktree_path": self.canon["app"],
                                             "branch": "ctx", "commit": self.head("app")}).stdout.strip()
        proc = self.hook("session-start", {"session_id": "s6", "cwd": path, "source": "resume"})
        payload = json.loads(proc.stdout)
        text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn(path, text)
        self.assertIn("observer", text)  # s5 still holds the write lease
        self.hook("session-end", {"session_id": "s5", "reason": "other"})
        status = self.wsp("status", "--task", "ctx")
        self.assertNotIn("claude:s5", [l["holder"] for l in status["trees"][0]["leases"]])

    def test_plugin_options_sync_into_shared_config(self):
        self.hook("session-start", {"session_id": "s7", "cwd": self.tmp},
                  env={"CLAUDE_PLUGIN_OPTION_DISK_RESERVE_GIB": "7", "CLAUDE_PLUGIN_OPTION_DEPENDENCY_STRATEGY": "install"})
        with open(self.env["WSP_CONFIG"]) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["limits"]["min_free_gib"], 7.0)
        self.assertEqual(cfg["dependency_strategy"], "install")


class InitTests(Sandbox):
    repos = ("app", "lib")

    def test_repo_map_import(self):
        table = os.path.join(self.tmp, "repos.md")
        with open(table, "w") as fh:
            fh.write("| Repo | Local path | Origin |\n| --- | --- | --- |\n"
                     f"| team/app | `{self.canon['app']}` | `git@example.com:team/app.git` |\n"
                     "| team/gone | `/nonexistent/gone` | `git@example.com:team/gone.git` |\n")
        env = {"WSP_CONFIG": os.path.join(self.tmp, "fresh2", "config.json")}
        detected = self.wsp("init", "--detect", "--search-dir", os.path.join(self.tmp, "none"),
                            "--repo-map", table, env=env)
        self.assertEqual(detected["environment"]["repo_map"], table)
        integration = [q for q in detected["questions"] if q["id"] == "integration"][0]
        self.assertEqual(integration["recommended"], "repo-map")
        self.assertEqual([r["name"] for r in detected["repos_detected"]], ["app"])

    def test_detect_and_apply_answers(self):
        new_config = os.path.join(self.tmp, "fresh", "config.json")
        env = {"WSP_CONFIG": new_config}
        self.write(os.path.join(self.canon["app"], ".dev.vars.example"), "X=\n")
        self.git(self.canon["app"], "add", ".dev.vars.example")
        self.git(self.canon["app"], "commit", "-q", "-m", "example")
        detected = self.wsp("init", "--detect", "--search-dir", os.path.join(self.tmp, "src"), env=env)
        ids = [q["id"] for q in detected["questions"]]
        for expected in ("worktrees_root", "state_dir", "repos", "dependency_strategy", "secrets_source",
                         "disk_reserve_gib", "claude_hooks", "exclusions", "janitor", "integration"):
            self.assertIn(expected, ids)
        names = {r["name"] for r in detected["repos_detected"]}
        self.assertTrue({"app", "lib"} <= names)
        app = [r for r in detected["repos_detected"] if r["name"] == "app"][0]
        self.assertEqual(app["env_files"], [".dev.vars"])
        answers = {"worktrees_root": os.path.join(self.tmp, "wt2", "tasks"),
                   "state_dir": os.path.join(self.tmp, "state2"), "repos": ["app"], "exclusions": False,
                   "integration": "none", "disk_reserve_gib": 5}
        answers_file = os.path.join(self.tmp, "answers.json")
        with open(answers_file, "w") as fh:
            json.dump(answers, fh)
        res = self.wsp("init", "--answers", answers_file, "--search-dir", os.path.join(self.tmp, "src"), env=env)
        self.assertTrue(res["written"])
        with open(new_config) as fh:
            cfg = json.load(fh)
        self.assertEqual(sorted(cfg["repos"]), ["app"])
        self.assertEqual(cfg["repos"]["app"]["profile"]["secrets"], [{"path": ".dev.vars", "source": "canonical"}])
        again = self.wsp("init", "--answers", answers_file, "--search-dir", os.path.join(self.tmp, "src"), env=env)
        self.assertEqual(again["diff"], "")
        doctor = self.wsp("doctor", env=env, ok=None)
        self.assertIn("repo app", [c["check"] for c in doctor["checks"]])


if __name__ == "__main__":
    unittest.main()
