"""Dependency preparation per package directory.

Strategy (v1): never symlink node_modules. A verified instance with the same
fingerprint is materialised by copy-on-write clone (APFS clonefile / reflink),
which is isolated and nearly free. Otherwise the package manager installs into
the tree and, when the result is relocatable, a clone is stored in the cache.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess

from . import disk, locks, safety, util
from .errors import WspError

LOCKFILES = ("yarn.lock", "pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json")
FINGERPRINT_NAMES = {
    "package.json", "yarn.lock", "pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json",
    ".yarnrc.yml", ".yarnrc", ".npmrc", ".pnpmfile.cjs", "pnpm-workspace.yaml", ".nvmrc", ".node-version",
}
ANCESTOR_CONFIG = (".yarnrc.yml", ".yarnrc", ".npmrc", ".nvmrc", ".node-version")
MARKER = ".wsp-deps.json"


def find_lock_dir(tree_path, package_dir):
    """Nearest directory (package_dir or an ancestor inside the tree) holding a lockfile.
    Returns the path relative to the tree ('.' for the root) or None."""
    start = os.path.normpath(os.path.join(tree_path, package_dir or "."))
    safety_rel = os.path.relpath(start, tree_path)
    if safety_rel.startswith(".."):
        raise WspError("PATH_UNSAFE", "package dir is outside the tree", package_dir=package_dir)
    current = start
    while True:
        if any(os.path.isfile(os.path.join(current, name)) for name in LOCKFILES):
            return os.path.relpath(current, tree_path)
        if os.path.realpath(current) == os.path.realpath(tree_path):
            return None
        current = os.path.dirname(current)


def detect_recipe(lock_abs, override=None):
    override = override or {}
    if override.get("install_command"):
        return {"manager": override.get("manager", "custom"), "install_command": override["install_command"]}
    pkg = util.read_json(os.path.join(lock_abs, "package.json"), {}) or {}
    pm_field = str(pkg.get("packageManager") or "")
    if os.path.exists(os.path.join(lock_abs, "pnpm-lock.yaml")):
        return {"manager": "pnpm", "install_command": "pnpm install --frozen-lockfile"}
    if os.path.exists(os.path.join(lock_abs, "yarn.lock")):
        berry = os.path.exists(os.path.join(lock_abs, ".yarnrc.yml")) or (
            pm_field.startswith("yarn@") and not pm_field.startswith("yarn@1."))
        if berry:
            return {"manager": "yarn-berry", "install_command": "yarn install --immutable"}
        return {"manager": "yarn-classic", "install_command": "yarn install --frozen-lockfile"}
    if os.path.exists(os.path.join(lock_abs, "package-lock.json")) or os.path.exists(
            os.path.join(lock_abs, "npm-shrinkwrap.json")):
        return {"manager": "npm", "install_command": "npm ci"}
    raise WspError("DEPS_INCOMPATIBLE", "no supported lockfile found", dir=lock_abs)


def _node_version(cwd):
    try:
        return subprocess.run(["node", "-v"], cwd=cwd, capture_output=True, text=True, timeout=30).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def fingerprint(tree_path, lock_rel, recipe):
    lock_abs = os.path.normpath(os.path.join(tree_path, lock_rel))
    listed = subprocess.run(
        ["git", "-C", tree_path, "ls-files", "-z", "--", lock_rel], capture_output=True, text=True
    ).stdout.split("\0")
    files = []
    for rel in listed:
        if not rel:
            continue
        base = os.path.basename(rel)
        if base in FINGERPRINT_NAMES or "/patches/" in f"/{rel}" or rel.startswith(".yarn/patches/"):
            files.append(rel)
    # config files in ancestors between the lock dir and the repo root
    probe = lock_abs
    while os.path.realpath(probe) != os.path.realpath(tree_path):
        probe = os.path.dirname(probe)
        for name in ANCESTOR_CONFIG:
            candidate = os.path.join(probe, name)
            if os.path.isfile(candidate):
                files.append(os.path.relpath(candidate, tree_path))
    digest = hashlib.sha256()
    inputs = {
        "recipe": recipe,
        "node": _node_version(lock_abs),
        "platform": platform.system(),
        "machine": platform.machine(),
        "files": {},
    }
    for rel in sorted(set(files)):
        full = os.path.join(tree_path, rel)
        if os.path.isfile(full):
            inputs["files"][rel] = util.sha256_file(full)
    digest.update(json.dumps(inputs, sort_keys=True).encode())
    return digest.hexdigest()[:24], inputs


def node_modules_dirs(lock_abs):
    found = []
    for base, dirs, _files in os.walk(lock_abs):
        if ".git" in dirs:
            dirs.remove(".git")
        if "node_modules" in dirs:
            found.append(os.path.relpath(os.path.join(base, "node_modules"), lock_abs))
            dirs.remove("node_modules")
    return sorted(found)


def relocation_problems(tree_path, lock_abs, nm_rels):
    """Symlinks inside node_modules that would break or point at the wrong tree after cloning."""
    real_tree = os.path.realpath(tree_path)
    problems = []
    for nm_rel in nm_rels:
        nm = os.path.join(lock_abs, nm_rel)
        candidates = []
        for entry in os.scandir(nm):
            candidates.append(entry.path)
            if entry.name.startswith("@") and entry.is_dir(follow_symlinks=False):
                candidates.extend(e.path for e in os.scandir(entry.path))
            if entry.name == ".bin" and entry.is_dir(follow_symlinks=False):
                candidates.extend(e.path for e in os.scandir(entry.path))
        for path in candidates:
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            if os.path.isabs(target):
                if os.path.realpath(target).startswith(real_tree + os.sep):
                    problems.append({"link": os.path.relpath(path, tree_path), "target": target, "why": "absolute link into the tree"})
            else:
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not os.path.realpath(resolved).startswith(real_tree + os.sep):
                    problems.append({"link": os.path.relpath(path, tree_path), "target": target, "why": "relative link leaves the tree"})
    return problems


def _marker_file(lock_abs):
    return os.path.join(lock_abs, "node_modules", MARKER)


def read_marker(lock_abs):
    return util.read_json(_marker_file(lock_abs))


def _write_marker(lock_abs, data):
    if os.path.isdir(os.path.join(lock_abs, "node_modules")):
        util.write_json_atomic(_marker_file(lock_abs), data)


def _cache_dir(cfg, repo, lock_rel, fp):
    slug = util.slugify(lock_rel if lock_rel != "." else "root", max_len=80) or "root"
    return os.path.join(cfg.dependency_cache_dir, repo, slug, fp)


def _package_override(profile, package_dir, lock_rel):
    packages = profile.get("packages") or {}
    return packages.get(package_dir) or packages.get(lock_rel) or {}


def prepare(cfg, reg, tree, package_dir=".", profile=None, strategy=None, force_install=False):
    tree_path = tree["path"]
    profile = profile or cfg.profile(tree["repo"], tree_path)
    strategy = strategy or cfg.dependency_strategy
    lock_rel = find_lock_dir(tree_path, package_dir)
    if lock_rel is None:
        return {"package_dir": package_dir, "mode": "none", "reason": "no lockfile"}
    lock_abs = os.path.normpath(os.path.join(tree_path, lock_rel))
    override = _package_override(profile, package_dir, lock_rel)
    recipe = detect_recipe(lock_abs, override)
    fp, inputs = fingerprint(tree_path, lock_rel, recipe)
    result = {"package_dir": package_dir, "lock_dir": lock_rel, "manager": recipe["manager"], "fingerprint": fp,
              "package_cwd": lock_abs}

    marker = read_marker(lock_abs)
    if marker and marker.get("fingerprint") == fp and not force_install:
        result["mode"] = "already-prepared"
        return result
    if strategy == "none":
        result["mode"] = "skipped"
        result["reason"] = "dependency_strategy=none"
        return result

    cmode = disk.clone_mode(cfg.dependency_cache_dir)
    can_clone = (cmode is not None and strategy in ("auto", "clone")
                 and disk.same_volume(cfg.dependency_cache_dir, tree_path))
    cache_lock = f"cache:{tree['repo']}:{util.slugify(lock_rel, 60) or 'root'}:{fp}"
    with locks.locked(cfg.state_dir, cache_lock):
        inst = reg.get_dep_instance(tree["repo"], lock_rel, fp)
        payload = os.path.join(inst["path"], "payload") if inst else None
        if (inst and inst["state"] == "ready" and can_clone and not force_install
                and os.path.isdir(payload)):
            manifest = json.loads(inst["manifest_json"] or "{}")
            for nm_rel in manifest.get("node_modules", []):
                dst = os.path.join(lock_abs, nm_rel)
                if os.path.lexists(dst):
                    safety.remove_tree(tree_path, dst)
                disk.clone_tree(os.path.join(payload, nm_rel), dst, cmode)
            _write_marker(lock_abs, {"fingerprint": fp, "mode": "clone", "instance": inst["id"], "at": util.now_iso()})
            reg.set_dep_use(tree["id"], lock_rel, inst["id"], fp, "clone")
            reg.touch_dep_instance(inst["id"])
            _remeasure(reg, tree)
            result.update(mode="clone", instance=inst["id"], clone=cmode)
            return result

        estimate = (inst["size_bytes"] if inst and inst["size_bytes"] else util.GIB)
        disk.admit(cfg, reg, estimate, kind="dependency install")
        for nm_rel in node_modules_dirs(lock_abs):
            safety.remove_tree(tree_path, os.path.join(lock_abs, nm_rel))
        proc = subprocess.run(recipe["install_command"], shell=True, cwd=lock_abs, capture_output=True, text=True,
                              timeout=3600, env={**os.environ, "CI": os.environ.get("CI", "1")})
        if proc.returncode != 0:
            raise WspError("DEPS_FAILED", f"`{recipe['install_command']}` failed in {lock_rel}",
                           stdout=proc.stdout[-1500:], stderr=proc.stderr[-2500:])
        verify_cmd = override.get("verify_command") or profile.get("verify_command")
        if verify_cmd:
            vproc = subprocess.run(verify_cmd, shell=True, cwd=lock_abs, capture_output=True, text=True, timeout=3600)
            if vproc.returncode != 0:
                raise WspError("DEPS_FAILED", f"verify command failed: {verify_cmd}", stderr=vproc.stderr[-2500:])
        nm_rels = node_modules_dirs(lock_abs)
        problems = relocation_problems(tree_path, lock_abs, nm_rels)
        result.update(mode="install", node_modules=nm_rels)
        instance = None
        quota_block = None
        if can_clone and nm_rels and not problems:
            quota_block = _cache_room(cfg, reg, disk.dir_size(lock_abs) or 0)
        if can_clone and nm_rels and not problems and not quota_block:
            target = _cache_dir(cfg, tree["repo"], lock_rel, fp)
            staging = target + ".staging"
            if os.path.exists(staging):
                shutil.rmtree(staging)
            if os.path.exists(target):
                shutil.rmtree(target)
            for nm_rel in nm_rels:
                disk.clone_tree(os.path.join(lock_abs, nm_rel), os.path.join(staging, "payload", nm_rel), cmode)
            util.write_json_atomic(os.path.join(staging, "manifest.json"),
                                   {"fingerprint": fp, "inputs": inputs, "node_modules": nm_rels,
                                    "repo": tree["repo"], "lock_dir": lock_rel, "created_at": util.now_iso()})
            os.replace(staging, target)
            size = disk.dir_size(target)
            instance = reg.upsert_dep_instance(tree["repo"], lock_rel, fp, target, "ready",
                                               {"node_modules": nm_rels, "inputs": inputs}, size)
            result["cached"] = True
        else:
            reason = (quota_block or ("relocation problems" if problems else "no copy-on-write clone on this volume"
                      if not can_clone else "no node_modules produced"))
            instance = reg.upsert_dep_instance(tree["repo"], lock_rel, fp, _cache_dir(cfg, tree["repo"], lock_rel, fp),
                                               "unclonable", {"problems": problems, "reason": reason}, 0)
            result.update(cached=False, cache_skip_reason=reason, relocation_problems=problems[:20])
        _write_marker(lock_abs, {"fingerprint": fp, "mode": "install", "instance": instance["id"], "at": util.now_iso()})
        reg.set_dep_use(tree["id"], lock_rel, instance["id"], fp, "install")
        _remeasure(reg, tree)
        return result


def _remeasure(reg, tree):
    """Tree size is accounted conservatively (logical size, clones counted in full)."""
    size = disk.dir_size(tree["path"])
    if size is not None:
        reg.update_tree(tree["id"], size_bytes=size, size_measured_at=util.now_iso())


def _cache_room(cfg, reg, needed):
    """Keep the dependency cache within its sub-quota: drop unused instances first;
    if still over, do not cache (the tree keeps its own install)."""
    quota = cfg.limits["dependency_cache_gib"] * util.GIB
    total = sum(i["size_bytes"] or 0 for i in reg.dep_instances())
    if total + needed <= quota:
        return None
    gc_cache(cfg, reg, apply=True, target_free=total + needed - quota)
    total = sum(i["size_bytes"] or 0 for i in reg.dep_instances())
    if total + needed <= quota:
        return None
    return f"dependency cache sub-quota ({cfg.limits['dependency_cache_gib']} GiB) is full"


def check(cfg, reg, tree):
    """Compare recorded dependency fingerprints with the current tree."""
    out = []
    for use in reg.dep_uses_for_tree(tree["id"]):
        lock_rel = use["package_dir"]
        lock_abs = os.path.normpath(os.path.join(tree["path"], lock_rel))
        if not os.path.isdir(lock_abs):
            out.append({"lock_dir": lock_rel, "state": "missing"})
            continue
        try:
            profile = cfg.profile(tree["repo"], tree["path"])
            recipe = detect_recipe(lock_abs, _package_override(profile, lock_rel, lock_rel))
            fp, _ = fingerprint(tree["path"], lock_rel, recipe)
        except WspError as exc:
            out.append({"lock_dir": lock_rel, "state": "error", "error": exc.code})
            continue
        marker = read_marker(lock_abs)
        if not os.path.isdir(os.path.join(lock_abs, "node_modules")):
            state = "absent"
        elif fp != use["fingerprint"] or not marker or marker.get("fingerprint") != fp:
            state = "stale"
        else:
            state = "fresh"
        out.append({"lock_dir": lock_rel, "state": state, "recorded": use["fingerprint"], "current": fp, "mode": use["mode"]})
    return out


def gc_cache(cfg, reg, apply=False, target_free=0):
    """Unused instances may go, except the newest instance per package (to avoid reinstalls)
    unless the cache sub-quota is exceeded or `target_free` bytes must be released."""
    instances = reg.dep_instances()
    newest = {}
    for inst in instances:
        key = (inst["repo"], inst["package_dir"])
        if key not in newest or (inst["last_used_at"] or "") > (newest[key]["last_used_at"] or ""):
            newest[key] = inst
    total = sum(i["size_bytes"] or 0 for i in instances)
    quota = cfg.limits["dependency_cache_gib"] * util.GIB
    candidates = []
    for inst in sorted(instances, key=lambda i: i["last_used_at"] or ""):
        if reg.dep_use_count(inst["id"]) > 0:
            continue
        is_newest = newest[(inst["repo"], inst["package_dir"])]["id"] == inst["id"]
        if is_newest and total <= quota and inst["state"] == "ready" and target_free <= 0:
            continue
        candidates.append(inst)
        total -= inst["size_bytes"] or 0
        target_free -= inst["size_bytes"] or 0
    removed = []
    if apply:
        for inst in candidates:
            if os.path.isdir(inst["path"]):
                safety.remove_tree(cfg.dependency_cache_dir, inst["path"])
            reg.delete_dep_instance(inst["id"])
            removed.append(inst["id"])
    return {"candidates": [{"id": i["id"], "repo": i["repo"], "lock_dir": i["package_dir"], "size": util.human_bytes(i["size_bytes"])}
                           for i in candidates], "removed": removed}
