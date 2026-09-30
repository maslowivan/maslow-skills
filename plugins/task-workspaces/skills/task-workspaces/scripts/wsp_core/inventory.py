"""Read-only inventory of every worktree of the configured repositories.

Nothing here changes a tree, a registration or a branch. Ownership is proven by
the wsp marker in the tree's git dir; other trees are classified by location.
"""

import os

from . import disk, gitutil, manifests, procs, util


def _owner(path, marker):
    if marker and marker.get("manager") == "wsp":
        return "wsp"
    home = os.path.expanduser("~")
    if path.startswith(os.path.join(home, ".codex", "worktrees") + os.sep):
        return "codex"
    if f"{os.sep}.claude{os.sep}worktrees{os.sep}" in path:
        return "claude"
    return "unowned"


def scan(cfg, repo_paths=None, sizes=False, check_processes=True):
    sources = {}
    for name in cfg.repo_names():
        path = cfg.repo(name)["path"]
        if path:
            sources[name] = path
    for extra in repo_paths or []:
        extra = util.expand(extra)
        sources[os.path.basename(extra.rstrip("/"))] = extra
    all_cwds = None
    if check_processes:
        all_cwds = procs.cwd_processes("/")
    repos = []
    for name, src in sorted(sources.items()):
        entry = {"repo": name, "source": src, "trees": []}
        if not gitutil.is_repo(src):
            entry["error"] = "source is not a Git repository"
            repos.append(entry)
            continue
        default_branch = gitutil.default_branch(src)
        default_ref = f"refs/remotes/origin/{default_branch}"
        items = gitutil.worktree_list(src)
        for index, wt in enumerate(items):
            path = wt["path"]
            info = {"path": path, "main": index == 0, "branch": wt["branch"], "detached": wt["detached"],
                    "head": wt["head"], "locked": wt["locked"], "lock_reason": wt["lock_reason"],
                    "prunable": wt["prunable"], "exists": os.path.isdir(path)}
            if info["main"]:
                info["owner"] = "canonical"
            if info["exists"]:
                marker = manifests.read_marker(path) if not info["main"] else None
                info.setdefault("owner", _owner(path, marker))
                if marker:
                    info["wsp_task"] = marker.get("task_id")
                try:
                    info["dirty_entries"] = len(gitutil.status_entries(path))
                    cov = gitutil.covered_by_remote(src, wt["head"], default_ref) if wt["head"] else {"covered": None}
                    info["covered_by_remote"] = cov["covered"]
                    info["coverage"] = cov.get("how")
                    info["last_commit"] = gitutil.out(path, "log", "-1", "--format=%cs")
                except Exception as exc:  # report, never fail the whole inventory
                    info["error"] = str(exc)[:300]
                if all_cwds is not None:
                    real = os.path.realpath(path)
                    info["processes"] = sum(1 for p in all_cwds if p["cwd"] == real or p["cwd"].startswith(real + os.sep))
                if sizes:
                    info["size"] = util.human_bytes(disk.dir_size(path))
                if not info["main"]:
                    info["assessment"] = _assess(info)
            else:
                info.setdefault("owner", "unknown")
                info["assessment"] = "missing folder (registration only)"
            entry["trees"].append(info)
        repos.append(entry)
    counts = {}
    for entry in repos:
        for tree in entry["trees"]:
            if not tree["main"]:
                counts[tree.get("owner", "unknown")] = counts.get(tree.get("owner", "unknown"), 0) + 1
    return {"measured_at": util.now_iso(), "coverage": "configured repositories only", "repos": repos,
            "counts_by_owner": counts}


def _assess(info):
    if info.get("processes"):
        return "active (processes running)"
    if info.get("dirty_entries"):
        return "has uncommitted changes"
    if info.get("covered_by_remote") is False:
        return "has commits not on any remote"
    if info.get("covered_by_remote"):
        return "clean; content is on the remote"
    return "unknown"
