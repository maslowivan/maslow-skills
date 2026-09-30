"""Portable per-tree manifests and in-gitdir ownership markers.

Manifests (<state_dir>/manifests/<task>/<repo>.json) describe everything needed
to restore a tree and to rebuild the registry. The ownership marker lives in the
tree's private git dir (never in the working files), so it cannot be committed.
"""

import json
import os

from . import gitutil, util

FORMAT_VERSION = 1
MARKER_NAME = "wsp-owner.json"


def manifest_path(cfg, task_id, repo):
    return os.path.join(cfg.manifests_dir, task_id, f"{repo}.json")


def write_manifest(cfg, reg, tree):
    task = reg.get_task(tree["task_id"])
    checkpoints = reg.valid_checkpoints(tree["id"])
    data = {
        "format_version": FORMAT_VERSION,
        "written_at": util.now_iso(),
        "task": {k: task[k] for k in ("id", "title", "tracker_ref", "status", "pinned", "created_at")} if task else None,
        "tree": {k: tree[k] for k in tree},
        "checkpoints": checkpoints,
        "sessions": [
            {k: s[k] for k in ("app", "session_id", "holder", "confirmed", "role", "attached_at")}
            for s in reg.sessions_for_task(tree["task_id"])
        ],
    }
    util.write_json_atomic(manifest_path(cfg, tree["task_id"], tree["repo"]), data)
    write_task_manifest(cfg, reg, tree["task_id"])
    return data


def task_manifest_path(cfg, task_id):
    return os.path.join(cfg.manifests_dir, task_id, "_task.json")


def write_task_manifest(cfg, reg, task_id):
    """Set manifest for a multi-repository task: what each tree is recoverable to and in
    which order to verify it. This is a recoverable set, not a cross-repository transaction."""
    task = reg.get_task(task_id)
    trees = reg.trees_for_task(task_id, include_closed=True)
    members, incomplete = [], []
    for tree in sorted(trees, key=lambda t: t["repo"]):
        ck = reg.latest_checkpoint(tree["id"])
        member = {
            "repo": tree["repo"], "tree_id": tree["id"], "state": tree["state"], "path": tree["path"],
            "branch": tree["branch"], "base_sha": tree["base_sha"], "head_sha": tree["head_sha"],
            "generation": tree["generation"],
            "recoverable_from": ("checkpoint" if ck else "branch" if tree["state"] != "closed" else None),
            "checkpoint": ({"seq": ck["seq"], "ref": ck["ref"], "work_commit": ck["work_commit"],
                            "bundle": ck["bundle_path"], "created_at": ck["created_at"]} if ck else None),
        }
        members.append(member)
        if tree["state"] not in ("ready", "evicted", "closed"):
            incomplete.append({"repo": tree["repo"], "state": tree["state"]})
    data = {
        "format_version": FORMAT_VERSION,
        "written_at": util.now_iso(),
        "task_id": task_id,
        "status": task["status"] if task else None,
        "trees": members,
        "incomplete": incomplete,
        "verify_order": [m["repo"] for m in members if m["state"] != "closed"],
        "note": "restore each tree with `wsp ensure --task <id> --repo <repo>`; repositories are restored "
                "independently, commits and PRs across repositories are not transactional",
    }
    util.write_json_atomic(task_manifest_path(cfg, task_id), data)
    return data


def read_manifests(cfg):
    out = []
    root = cfg.manifests_dir
    if not os.path.isdir(root):
        return out
    for task_id in sorted(os.listdir(root)):
        task_dir = os.path.join(root, task_id)
        if not os.path.isdir(task_dir):
            continue
        for name in sorted(os.listdir(task_dir)):
            if name.endswith(".json") and not name.startswith("_"):
                data = util.read_json(os.path.join(task_dir, name))
                if data and data.get("format_version") == FORMAT_VERSION:
                    out.append(data)
    return out


def marker_path(tree_path):
    return os.path.join(gitutil.absolute_git_dir(tree_path), MARKER_NAME)


def write_marker(tree_path, tree, state_dir):
    data = {
        "format_version": FORMAT_VERSION,
        "manager": "wsp",
        "state_dir": state_dir,
        "task_id": tree["task_id"],
        "tree_id": tree["id"],
        "repo": tree["repo"],
        "generation": tree["generation"],
        "path": tree["path"],
        "written_at": util.now_iso(),
    }
    util.write_json_atomic(marker_path(tree_path), data)
    return data


def read_marker(tree_path):
    try:
        path = marker_path(tree_path)
    except Exception:
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_marker_from_admin_dir(admin_dir):
    try:
        with open(os.path.join(admin_dir, MARKER_NAME)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
