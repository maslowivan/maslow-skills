"""Disk accounting, filesystem detection and admission control."""

import os
import platform
import shutil
import subprocess

from . import util
from .errors import WspError


def existing_ancestor(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def free_bytes(path):
    return shutil.disk_usage(existing_ancestor(path)).free


def fs_type(path):
    """Filesystem type of the volume holding path: 'apfs', 'btrfs', 'xfs', 'ext4', ... or None."""
    target = os.path.realpath(existing_ancestor(path))
    system = platform.system()
    best, best_type = "", None
    if system == "Darwin":
        try:
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            # "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)"
            if " on " not in line or "(" not in line:
                continue
            mount_point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
            kind = line.rsplit("(", 1)[1].split(",")[0].strip(" )")
            if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > len(best):
                best, best_type = mount_point, kind
        return best_type
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, kind = parts[1], parts[2]
                if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > len(best):
                    best, best_type = mount_point, kind
    except OSError:
        return None
    return best_type


def same_volume(a, b):
    try:
        return os.stat(existing_ancestor(a)).st_dev == os.stat(existing_ancestor(b)).st_dev
    except OSError:
        return False


def clone_mode(path):
    """How cheap copies can be made on this volume: 'clonefile', 'reflink' or None."""
    kind = fs_type(path)
    if platform.system() == "Darwin" and kind == "apfs":
        return "clonefile"
    if kind in ("btrfs", "xfs", "bcachefs", "zfs"):
        return "reflink"
    return None


def clone_tree(src, dst, mode):
    """Copy-on-write copy of a directory tree. Symlinks are copied as symlinks."""
    if os.path.exists(dst):
        raise WspError("PATH_CONFLICT", "clone destination exists", path=dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if mode == "clonefile":
        cmd = ["cp", "-c", "-R", src, dst]
    elif mode == "reflink":
        cmd = ["cp", "-R", "--reflink=always", "--no-dereference", src, dst]
    else:
        raise WspError("UNSUPPORTED", "copy-on-write clones are not supported on this volume", path=src)
    util.run(cmd, code="DEPS_FAILED")


def dir_size(path, timeout=600):
    """Allocated size in bytes (du -sk; does not follow symlinks). None if unknown."""
    if not os.path.exists(path):
        return 0
    try:
        proc = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    try:
        return int(proc.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return None


def manager_usage(cfg, reg, measure=False):
    """Conservative accounted usage: trees (cached sizes), dependency cache, checkpoints, logs."""
    trees = 0
    for tree in reg.all_trees():
        if tree["state"] in ("ready", "restoring", "provisioning", "snapshotting", "evicting"):
            size = tree["size_bytes"]
            if measure or size is None:
                size = dir_size(tree["path"]) or 0
                reg.update_tree(tree["id"], size_bytes=size, size_measured_at=util.now_iso())
            trees += size or 0
    cache = sum((i["size_bytes"] or 0) for i in reg.dep_instances())
    checkpoints = dir_size(cfg.checkpoints_dir) or 0
    logs = dir_size(os.path.join(cfg.state_dir, "logs")) or 0
    return {"trees": trees, "dependency_cache": cache, "checkpoints": checkpoints, "logs": logs,
            "total": trees + cache + checkpoints + logs}


def summary(cfg, reg):
    free = free_bytes(cfg.worktrees_root)
    limits = cfg.limits
    usage = manager_usage(cfg, reg)
    tasks_with_trees = {t["task_id"] for t in reg.all_trees() if t["state"] not in ("evicted", "closed")}
    on_disk = [t for t in reg.all_trees() if t["state"] not in ("evicted", "closed", "missing")]
    level = "ok"
    if free < limits["emergency_free_gib"] * util.GIB:
        level = "emergency"
    elif free < limits["min_free_gib"] * util.GIB:
        level = "below_reserve"
    elif free < limits["warn_free_gib"] * util.GIB:
        level = "warning"
    return {
        "measured_at": util.now_iso(),
        "free_bytes": free,
        "free": util.human_bytes(free),
        "level": level,
        "usage": usage,
        "usage_human": {k: util.human_bytes(v) for k, v in usage.items()},
        "tasks_with_trees": len(tasks_with_trees),
        "trees_on_disk": len(on_disk),
        "reserved_bytes": sum(r["bytes"] for r in reg.active_reservations()),
    }


def admit(cfg, reg, estimate_bytes, new_trees=0, new_task=False, kind="create"):
    """Raise unless an operation of estimate_bytes may start. Checks fresh free space,
    concurrent reservations, the emergency floor, the manager quota and count limits."""
    limits = cfg.limits
    free = free_bytes(cfg.worktrees_root)
    reserved = sum(r["bytes"] for r in reg.active_reservations())
    available = free - reserved
    if free < limits["emergency_free_gib"] * util.GIB:
        raise WspError("DISK_LOW", "free space is below the emergency floor; growth is blocked", free=util.human_bytes(free),
                       emergency_floor_gib=limits["emergency_free_gib"])
    if available - estimate_bytes < limits["min_free_gib"] * util.GIB:
        raise WspError(
            "DISK_LOW",
            f"{kind} would leave less than the {limits['min_free_gib']} GiB reserve",
            free=util.human_bytes(free), reserved=util.human_bytes(reserved), estimate=util.human_bytes(estimate_bytes),
        )
    accounted = manager_usage(cfg, reg)
    usage = accounted["total"]
    if usage + estimate_bytes > limits["manager_quota_gib"] * util.GIB:
        raise WspError("QUOTA_EXCEEDED", "manager quota would be exceeded", usage=util.human_bytes(usage),
                       estimate=util.human_bytes(estimate_bytes), quota_gib=limits["manager_quota_gib"])
    if accounted["checkpoints"] > limits["checkpoints_gib"] * util.GIB:
        # unpublished work is never deleted to make room: growth stops until the user decides
        raise WspError("QUOTA_EXCEEDED", "checkpoint sub-quota is full; push or close tasks with saved work "
                       "(see `wsp list`), or raise limits.checkpoints_gib",
                       checkpoints=util.human_bytes(accounted["checkpoints"]), quota_gib=limits["checkpoints_gib"],
                       holders=checkpoint_holders(reg))
    on_disk = [t for t in reg.all_trees() if t["state"] not in ("evicted", "closed", "missing")]
    if new_trees and len(on_disk) + new_trees > limits["max_trees"]:
        raise WspError("LIMIT_REACHED", "tree limit reached; evict idle trees or raise limits.max_trees",
                       trees_on_disk=len(on_disk), limit=limits["max_trees"])
    if new_task:
        tasks = {t["task_id"] for t in on_disk}
        if len(tasks) + 1 > limits["max_tasks_with_trees"]:
            raise WspError("LIMIT_REACHED", "task limit reached; evict idle tasks or raise limits.max_tasks_with_trees",
                           tasks_with_trees=len(tasks), limit=limits["max_tasks_with_trees"])
    return {"free_bytes": free, "reserved_bytes": reserved}


def checkpoint_holders(reg):
    """Which tasks hold checkpoints, largest first (sizes from bundles and archives)."""
    out = {}
    for tree in reg.all_trees():
        for ck in reg.valid_checkpoints(tree["id"]):
            size = 0
            for path in (ck["bundle_path"], ck["ignored_archive"]):
                if path and os.path.exists(path):
                    size += os.path.getsize(path)
            out[tree["task_id"]] = out.get(tree["task_id"], 0) + size
    return [{"task": t, "size": util.human_bytes(b)} for t, b in sorted(out.items(), key=lambda kv: -kv[1])]


def checkpoint_usage_warning(cfg):
    used = dir_size(cfg.checkpoints_dir) or 0
    quota = cfg.limits["checkpoints_gib"] * util.GIB
    if used > quota:
        return f"checkpoints use {util.human_bytes(used)} (> {cfg.limits['checkpoints_gib']} GiB); new trees are blocked"
    return None
