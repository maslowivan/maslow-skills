"""Test sandbox: temporary origin/canonical repos, isolated Git config and wsp config.

Tests never touch real repositories: every path lives under a temporary directory.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WSP = os.path.join(ROOT, "skills", "task-workspaces", "scripts", "wsp.py")


def sh(cmd, cwd=None, env=None, check=True, input=None):
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, input=input,
                          shell=isinstance(cmd, str))
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


class Sandbox(unittest.TestCase):
    repos = ("app",)

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wsp-test-"))
        self.addCleanup(self._cleanup)
        gitconfig = os.path.join(self.tmp, "gitconfig")
        with open(gitconfig, "w") as fh:
            fh.write("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
                     "[commit]\n\tgpgsign = false\n[advice]\n\tdetachedHead = false\n")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "GIT_DIR", "GIT_WORK_TREE",
                                 "GIT_INDEX_FILE") and not k.startswith("CLAUDE_PLUGIN_OPTION_")}
        self.env.update({"GIT_CONFIG_GLOBAL": gitconfig, "GIT_CONFIG_NOSYSTEM": "1",
                         "WSP_CONFIG": os.path.join(self.tmp, "config", "config.json"),
                         "HOME": os.environ.get("HOME", self.tmp)})
        self.origins, self.canon = {}, {}
        for name in self.repos:
            self.make_repo(name)
        self.worktrees_root = os.path.join(self.tmp, "wt", "tasks")
        self.state_dir = os.path.join(self.tmp, "state")
        self.write_config()

    def _cleanup(self):
        # unlock and remove trees so that tempdir cleanup cannot fail on read-only files
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, cwd, *args, check=True):
        return sh(["git", "-C", cwd, *args], env=self.env, check=check)

    def make_repo(self, name):
        origin = os.path.join(self.tmp, "origin", f"{name}.git")
        os.makedirs(origin)
        self.git(origin, "init", "--bare", "-q", "-b", "main")
        canon = os.path.join(self.tmp, "src", name)
        os.makedirs(os.path.dirname(canon), exist_ok=True)
        sh(["git", "clone", "-q", origin, canon], env=self.env, check=False)
        with open(os.path.join(canon, "README.md"), "w") as fh:
            fh.write(f"# {name}\n")
        with open(os.path.join(canon, "a.txt"), "w") as fh:
            fh.write("alpha\n")
        with open(os.path.join(canon, "b.txt"), "w") as fh:
            fh.write("bravo\n")
        with open(os.path.join(canon, ".gitignore"), "w") as fh:
            fh.write("node_modules/\n.dev.vars\nlocal.cfg\n*.weird\ndist/\n")
        self.git(canon, "add", "-A")
        self.git(canon, "commit", "-q", "-m", "initial")
        self.git(canon, "push", "-q", "-u", "origin", "main")
        self.git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(canon, "remote", "set-head", "origin", "main")
        with open(os.path.join(canon, ".dev.vars"), "w") as fh:
            fh.write("SECRET=from-canonical\n")
        self.origins[name], self.canon[name] = origin, canon

    def write_config(self, **overrides):
        cfg = {
            "version": 1,
            "worktrees_root": self.worktrees_root,
            "state_dir": self.state_dir,
            "branch_prefix": "wsp/",
            "limits": {"min_free_gib": 0, "warn_free_gib": 0, "emergency_free_gib": 0, "manager_quota_gib": 100000,
                       "max_tasks_with_trees": 50, "max_trees": 100, "default_tree_estimate_gib": 0},
            "fetch": {"retries": 1, "backoff_seconds": 0.1, "timeout_seconds": 60},
            "repos": {name: {"path": self.canon[name], "origin": self.origins[name],
                             "profile": {"secrets": [{"path": ".dev.vars", "source": "canonical"}],
                                         "preserve_ignored": ["local.cfg"]}}
                      for name in self.repos},
        }
        for key, value in overrides.items():
            cfg[key] = value
        os.makedirs(os.path.dirname(self.env["WSP_CONFIG"]), exist_ok=True)
        with open(self.env["WSP_CONFIG"], "w") as fh:
            json.dump(cfg, fh)
        self.cfg = cfg

    def wsp(self, *args, ok=True, input=None, env=None, raw=False):
        run_env = dict(self.env)
        if env:
            run_env.update(env)
        args = list(args)
        if not raw:
            cut = args.index("--") if "--" in args else len(args)
            args.insert(cut, "--json")
        proc = sh(["python3", WSP, *args], env=run_env, check=False, input=input)
        if raw:
            return proc
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise AssertionError(f"wsp {args} produced non-JSON:\n{proc.stdout}\n{proc.stderr}")
        if ok is True and not data.get("ok"):
            raise AssertionError(f"wsp {args} failed: {json.dumps(data, indent=2)}\n{proc.stderr}")
        if ok is False and data.get("ok"):
            raise AssertionError(f"wsp {args} unexpectedly succeeded: {json.dumps(data, indent=2)}")
        data["_exit"] = proc.returncode
        return data

    def ensure(self, task, *repos, holder="cli:test", extra=()):
        args = ["ensure", "--task", task, "--holder", holder]
        for repo in repos or ("app",):
            args += ["--repo", repo]
        return self.wsp(*args, *extra)

    def tree_path(self, task, repo="app"):
        return os.path.join(self.worktrees_root, task, repo)

    def write(self, path, content, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(content, bytes) else "w") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(path, mode)
