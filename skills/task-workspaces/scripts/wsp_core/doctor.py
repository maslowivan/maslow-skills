"""Environment and state diagnostics. Read-only."""

import os
import platform
import shutil
import sys

from . import disk, gitutil, procs, util, workspace


def run(cfg, reg=None):
    checks = []

    def add(name, ok, detail=None, level="error"):
        checks.append({"check": name, "ok": bool(ok), "level": "ok" if ok else level, "detail": detail})

    add("python>=3.11", sys.version_info >= (3, 11), platform.python_version())
    gv = gitutil.version()
    add("git>=2.38 (merge-tree --write-tree)", gv >= (2, 38, 0), ".".join(map(str, gv)))
    add("lsof available", shutil.which("lsof") is not None, None)
    add("platform supported", platform.system() in ("Darwin", "Linux"), platform.system())
    add("config present", cfg.exists, cfg.path)
    if not cfg.exists:
        return {"ok": False, "checks": checks, "hint": "run `wsp init`"}

    configured_root = util.expand(cfg.data["worktrees_root"])
    add("worktrees_root is not a symlink", not os.path.islink(configured_root), configured_root)
    root = cfg.worktrees_root
    problems = workspace.root_ancestor_problems(root)
    add("no node_modules/package.json above worktrees_root", not problems, problems or None)
    fs = disk.fs_type(root)
    clone = disk.clone_mode(root)
    add("filesystem", True, fs)
    add("copy-on-write clones available", clone is not None, clone or "dependencies will be installed per tree",
        level="warning")
    add("dependency cache on the same volume as trees",
        disk.same_volume(cfg.dependency_cache_dir, root), cfg.dependency_cache_dir, level="warning")
    add("state_dir outside worktrees_root",
        not (cfg.state_dir + os.sep).startswith(root + os.sep), cfg.state_dir)
    for path, label in ((root, "worktrees_root"), (cfg.state_dir, "state_dir")):
        base = disk.existing_ancestor(path)
        add(f"{label} writable", os.access(base, os.W_OK), path)
    free = disk.free_bytes(root)
    limits = cfg.limits
    add("free space above reserve", free >= limits["min_free_gib"] * util.GIB, util.human_bytes(free), level="warning")

    for name in cfg.repo_names():
        repo = cfg.repo(name)
        ok = repo["path"] and gitutil.is_repo(repo["path"])
        detail = repo["path"]
        if ok and repo.get("origin"):
            actual = gitutil.origin_url(repo["path"])
            if not gitutil.same_remote(actual, repo["origin"]):
                ok, detail = False, f"origin {actual} != configured {repo['origin']}"
        add(f"repo {name}", ok, detail)

    state = {}
    if reg is not None:
        stale_ops = [o for o in reg.running_operations() if not procs.pid_alive(o["pid"], o["pid_start"])]
        add("no abandoned operations", not stale_ops, [f"{o['kind']}#{o['id']}" for o in stale_ops] or None,
            level="warning")
        ctx = workspace.Ctx(cfg, reg)
        counts = {}
        for tree in reg.all_trees():
            tree = workspace.reconcile(ctx, tree)
            counts[tree["state"]] = counts.get(tree["state"], 0) + 1
        state = {"trees_by_state": counts}
        add("no trees in unknown state", not counts.get("unknown"), counts.get("unknown"), level="warning")
    agents = {"claude_plugin": bool(os.environ.get("CLAUDE_PLUGIN_ROOT")),
              "claude_session": bool(os.environ.get("CLAUDE_CODE_SESSION_ID")),
              "codex_thread": bool(os.environ.get("CODEX_THREAD_ID"))}
    ok = all(c["ok"] or c["level"] == "warning" for c in checks)
    return {"ok": ok, "checks": checks, "state": state, "agents": agents}
