"""Checkpoints: insurance for unfinished work, not an archive.

A checkpoint exists only when a tree has unique local state (uncommitted or
unpushed changes, or preserved ignored files). It is stored as Git objects:

  index commit : tree of the real index (git write-tree), parent HEAD
  work commit  : tree of all working files incl. untracked, built in a temporary
                 index (GIT_INDEX_FILE), parents HEAD + index commit
  ref          : refs/wsp/<task>/<repo>/<seq> -> work commit (protects from GC)
  bundle       : git bundle of that ref, excluding everything already on remotes
  ignored      : tar.gz of profile-allowed ignored files (never secrets)

HEAD, the index and the stash of the user are never modified.
"""

import fnmatch
import json
import os
import shutil
import tarfile
import tempfile

from . import gitutil, manifests, util
from .errors import WspError

DEFAULT_REPRODUCIBLE = [
    "node_modules/", ".pnpm-store/", ".yarn/cache/", ".yarn/install-state.gz", ".yarn/unplugged/",
    "dist/", "build/", "out/", "coverage/", ".cache/", ".turbo/", ".next/", ".nuxt/", ".astro/",
    ".svelte-kit/", ".vite/", ".vitest/", ".parcel-cache/", ".wrangler/", ".vercel/", ".netlify/",
    "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".venv/", "venv/",
    "*.tsbuildinfo", ".eslintcache", ".DS_Store", "*.log", "tmp/", ".tmp/", "playwright-report/",
    "test-results/", "target/",
]
DEFAULT_SECRETS = [".env", ".env.*", ".dev.vars", ".dev.vars.*", "*.pem", "*.key", ".npmrc.local"]


def _match(path, pattern):
    """Gitignore-like matching: 'x/' matches a dir named x at any depth, patterns
    with an inner '/' are anchored at the repo root, others match any basename."""
    is_dir = path.endswith("/")
    p = path.rstrip("/")
    pat = pattern.rstrip("/")
    dir_only = pattern.endswith("/")
    if "/" in pat:
        if fnmatch.fnmatchcase(p, pat) or p.startswith(pat + "/"):
            return True
        return False
    parts = p.split("/")
    if dir_only:
        # a directory component anywhere, or the entry itself when it is a dir
        comps = parts if is_dir else parts[:-1]
        return any(fnmatch.fnmatchcase(c, pat) for c in comps)
    return any(fnmatch.fnmatchcase(c, pat) for c in parts)


def _secret_entries(profile):
    entries = []
    for item in profile.get("secrets") or []:
        if isinstance(item, str):
            entries.append({"path": item, "source": "canonical"})
        else:
            entries.append(item)
    return entries


def classify_ignored(tree_path, profile):
    secrets_cfg = _secret_entries(profile)
    secret_patterns = [s["path"] for s in secrets_cfg] + DEFAULT_SECRETS
    preserve = profile.get("preserve_ignored") or []
    reproducible = (profile.get("reproducible_ignored") or []) + DEFAULT_REPRODUCIBLE
    result = {"preserve": [], "secret": [], "reproducible": [], "unclassified": []}
    for path in gitutil.ignored_paths(tree_path):
        if any(_match(path, p) for p in secret_patterns):
            result["secret"].append(path)
        elif any(_match(path, p) for p in preserve):
            result["preserve"].append(path)
        elif any(_match(path, p) for p in reproducible):
            result["reproducible"].append(path)
        else:
            result["unclassified"].append(path)
    return result


def unique_state(tree_path, profile, default_ref):
    """What exists only in this tree (and would be lost if the folder vanished)."""
    entries = gitutil.status_entries(tree_path)
    head = gitutil.rev_parse(tree_path, "HEAD")
    coverage = gitutil.covered_by_remote(tree_path, head, default_ref)
    ignored = classify_ignored(tree_path, profile)
    conflicted = [p for xy, p in entries if xy in ("DD", "AU", "UD", "UA", "DU", "AA", "UU")]
    dirty = [{"status": xy, "path": p} for xy, p in entries]
    unpushed = gitutil.unpushed_count(tree_path, head) if head else 0
    unique = bool(dirty) or not coverage["covered"] or bool(ignored["preserve"])
    return {
        "head": head,
        "unique": unique,
        "dirty": dirty,
        "conflicted": conflicted,
        "unpushed_commits": unpushed,
        "covered_by_remote": coverage["covered"],
        "coverage": coverage["how"],
        "ignored": ignored,
    }


def _index_file(tree_path):
    return os.path.join(gitutil.absolute_git_dir(tree_path), "index")


def snapshot_trees(tree_path):
    """Return (index_tree, work_tree) without touching the real index."""
    index_tree = gitutil.out(tree_path, "write-tree")
    fd, tmp_index = tempfile.mkstemp(prefix="wsp-index-")
    os.close(fd)
    try:
        real = _index_file(tree_path)
        if os.path.exists(real):
            shutil.copyfile(real, tmp_index)
            env = {"GIT_INDEX_FILE": tmp_index}
        else:
            os.unlink(tmp_index)
            env = {"GIT_INDEX_FILE": tmp_index}
            gitutil.git(tree_path, "read-tree", "HEAD", env=env)
        gitutil.git(tree_path, "add", "-A", "--", ".", env=env)
        work_tree = gitutil.out(tree_path, "write-tree", env=env)
    finally:
        if os.path.exists(tmp_index):
            os.unlink(tmp_index)
    return index_tree, work_tree


def _commit_tree(tree_path, tree, parents, message):
    args = ["commit-tree", "--no-gpg-sign", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", message]
    return gitutil.out(tree_path, *args, env=gitutil.INTERNAL_IDENTITY)


def ref_name(task_id, repo, seq):
    return f"refs/wsp/{task_id}/{repo}/{seq}"


def _archive_ignored(tree_path, paths, dest):
    files = []
    for rel in paths:
        full = os.path.join(tree_path, rel.rstrip("/"))
        if os.path.isdir(full) and not os.path.islink(full):
            for base, dirs, names in os.walk(full):
                for name in names:
                    files.append(os.path.relpath(os.path.join(base, name), tree_path))
                for name in dirs:
                    if os.path.islink(os.path.join(base, name)):
                        files.append(os.path.relpath(os.path.join(base, name), tree_path))
        elif os.path.lexists(full):
            files.append(rel.rstrip("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    hashes = {}
    with tarfile.open(dest, "w:gz") as tar:
        for rel in sorted(files):
            full = os.path.join(tree_path, rel)
            tar.add(full, arcname=rel, recursive=False)
            if os.path.isfile(full) and not os.path.islink(full):
                hashes[rel] = util.sha256_file(full)
    os.chmod(dest, 0o600)
    return hashes


def create(cfg, reg, tree, profile, default_ref, reason, state=None):
    """Create a checkpoint if the tree has unique local state.

    Returns {"created": bool, ...}. When nothing is unique, older checkpoints of
    the tree are deleted (their content is on the remote)."""
    tree_path = tree["path"]
    state = state or unique_state(tree_path, profile, default_ref)
    if state["conflicted"]:
        raise WspError("CONFLICTED_INDEX", "tree has unresolved merge conflicts; resolve before checkpointing",
                       paths=state["conflicted"])
    if not state["unique"]:
        deleted = delete_all(cfg, reg, tree)
        reg.update_tree(tree["id"], head_sha=state["head"], remote_covered_sha=state["head"])
        manifests.write_manifest(cfg, reg, reg.get_tree(tree["id"]))
        return {"created": False, "reason": "covered_by_remote", "head": state["head"], "deleted_checkpoints": deleted,
                "state": state}

    head = state["head"]
    index_tree, work_tree = snapshot_trees(tree_path)
    index_commit = _commit_tree(tree_path, index_tree, [head], f"wsp index {tree['task_id']}/{tree['repo']}")
    seq = reg.next_checkpoint_seq(tree["id"])
    work_commit = _commit_tree(tree_path, work_tree, [head, index_commit],
                               f"wsp checkpoint {tree['task_id']}/{tree['repo']} #{seq} ({reason})")
    ref = ref_name(tree["task_id"], tree["repo"], seq)
    gitutil.git(tree_path, "update-ref", ref, work_commit)

    ck_dir = os.path.join(cfg.checkpoints_dir, tree["task_id"], tree["repo"])
    os.makedirs(ck_dir, exist_ok=True)
    bundle = os.path.join(ck_dir, f"{seq}.bundle")
    proc = gitutil.git(tree_path, "bundle", "create", bundle, ref, "--not", "--remotes", check=False)
    if proc.returncode != 0:
        gitutil.git(tree_path, "update-ref", "-d", ref)
        raise WspError("CHECKPOINT_INVALID", "git bundle create failed", stderr=proc.stderr.strip()[-1500:])
    os.chmod(bundle, 0o600)
    bundle_sha = util.sha256_file(bundle)

    archive, archive_sha, archive_hashes = None, None, {}
    if state["ignored"]["preserve"]:
        archive = os.path.join(ck_dir, f"{seq}-ignored.tar.gz")
        archive_hashes = _archive_ignored(tree_path, state["ignored"]["preserve"], archive)
        archive_sha = util.sha256_file(archive)

    secrets = []
    for rel in state["ignored"]["secret"]:
        full = os.path.join(tree_path, rel.rstrip("/"))
        secrets.append({"path": rel, "sha256": util.sha256_file(full) if os.path.isfile(full) else None})

    manifest = {
        "format_version": manifests.FORMAT_VERSION,
        "task_id": tree["task_id"], "repo": tree["repo"], "seq": seq, "reason": reason,
        "branch": tree["branch"], "head": head, "base_sha": tree["base_sha"],
        "index_tree": index_tree, "work_tree": work_tree, "work_commit": work_commit, "ref": ref,
        "bundle": os.path.basename(bundle), "bundle_sha256": bundle_sha,
        "ignored_archive": os.path.basename(archive) if archive else None, "ignored_sha256": archive_sha,
        "ignored_files": archive_hashes, "secrets_present": secrets,
        "dirty_entries": len(state["dirty"]), "unpushed_commits": state["unpushed_commits"],
        "created_at": util.now_iso(),
    }
    manifest_file = os.path.join(ck_dir, f"{seq}.json")
    util.write_json_atomic(manifest_file, manifest, mode=0o600)

    row = reg.insert_checkpoint(
        tree_id=tree["id"], seq=seq, ref=ref, branch=tree["branch"], head_sha=head,
        index_tree=index_tree, work_tree=work_tree, work_commit=work_commit,
        bundle_path=bundle, bundle_sha256=bundle_sha, ignored_archive=archive, ignored_sha256=archive_sha,
        manifest_path=manifest_file, secrets_json=json.dumps(secrets), reason=reason,
    )
    verify(tree_path, row)
    prune_old(cfg, reg, tree, keep=int(cfg.policy.get("keep_checkpoints_per_tree", 2)))
    reg.update_tree(tree["id"], head_sha=head)
    manifests.write_manifest(cfg, reg, reg.get_tree(tree["id"]))
    reg.event("checkpoint", tree["task_id"], tree["id"], seq=seq, reason=reason)
    return {"created": True, "checkpoint": row, "state": state}


def verify(repo_path, ck):
    """Check that a checkpoint can be restored: ref, objects, bundle and archive integrity."""
    problems = []
    target = gitutil.rev_parse(repo_path, ck["ref"])
    if target != ck["work_commit"]:
        problems.append("ref missing or moved")
    for obj in (ck["work_commit"], ck["index_tree"], ck["work_tree"]):
        if gitutil.git(repo_path, "cat-file", "-e", obj, check=False).returncode != 0:
            problems.append(f"object {obj} missing")
    if not ck["bundle_path"] or not os.path.exists(ck["bundle_path"]):
        problems.append("bundle missing")
    elif util.sha256_file(ck["bundle_path"]) != ck["bundle_sha256"]:
        problems.append("bundle checksum mismatch")
    else:
        proc = gitutil.git(repo_path, "bundle", "verify", "--quiet", ck["bundle_path"], check=False)
        if proc.returncode != 0:
            problems.append("bundle verify failed: " + proc.stderr.strip()[-300:])
    if ck["ignored_archive"]:
        if not os.path.exists(ck["ignored_archive"]):
            problems.append("ignored archive missing")
        elif util.sha256_file(ck["ignored_archive"]) != ck["ignored_sha256"]:
            problems.append("ignored archive checksum mismatch")
    if problems:
        raise WspError("CHECKPOINT_INVALID", "checkpoint failed verification", checkpoint=ck["id"], problems=problems)
    return True


def ensure_objects(repo_path, ck):
    """Bring checkpoint objects back from the bundle if the ref was lost."""
    if gitutil.rev_parse(repo_path, ck["ref"]) == ck["work_commit"]:
        return "present"
    if not ck["bundle_path"] or not os.path.exists(ck["bundle_path"]):
        raise WspError("CHECKPOINT_MISSING", "checkpoint ref and bundle are both missing", checkpoint=ck["id"])
    gitutil.git(repo_path, "fetch", "--no-tags", "--quiet", ck["bundle_path"], f"+{ck['ref']}:{ck['ref']}")
    return "fetched-from-bundle"


def restore_into(tree_path, ck):
    """Apply a checkpoint to a freshly checked-out tree at ck.head_sha."""
    head = gitutil.rev_parse(tree_path, "HEAD")
    if head != ck["head_sha"]:
        raise WspError("RESTORE_CONFLICT", "tree HEAD does not match checkpoint HEAD", head=head, expected=ck["head_sha"])
    # Working files and index := full working state (adds, deletions, modes, symlinks, untracked).
    gitutil.git(tree_path, "read-tree", "--reset", "-u", ck["work_tree"])
    # Index := original index; untracked files become untracked again, files stay on disk.
    gitutil.git(tree_path, "read-tree", ck["index_tree"])
    if ck["ignored_archive"]:
        with tarfile.open(ck["ignored_archive"], "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise WspError("CHECKPOINT_INVALID", "unsafe path in ignored archive", member=member.name)
            try:
                tar.extractall(tree_path, filter="tar")
            except TypeError:  # Python without extraction filters
                tar.extractall(tree_path)
    index_tree, work_tree = snapshot_trees(tree_path)
    if index_tree != ck["index_tree"] or work_tree != ck["work_tree"]:
        raise WspError("CHECKPOINT_INVALID", "restored state does not match the checkpoint",
                       index=(index_tree, ck["index_tree"]), work=(work_tree, ck["work_tree"]))
    return {"index_tree": index_tree, "work_tree": work_tree}


def _delete_one(cfg, reg, tree, ck, repo_path):
    if repo_path and gitutil.is_repo(repo_path):
        gitutil.git(repo_path, "update-ref", "-d", ck["ref"], check=False)
    for path in (ck["bundle_path"], ck["ignored_archive"], ck["manifest_path"]):
        if path and os.path.exists(path):
            os.unlink(path)
    reg.mark_checkpoint(ck["id"], "deleted")


def prune_old(cfg, reg, tree, keep):
    valid = reg.valid_checkpoints(tree["id"])
    repo_path = tree["source_path"]
    removed = 0
    for ck in valid[:-keep] if keep > 0 else valid:
        _delete_one(cfg, reg, tree, ck, repo_path)
        removed += 1
    return removed


def delete_all(cfg, reg, tree):
    removed = prune_old(cfg, reg, tree, keep=0)
    ck_dir = os.path.join(cfg.checkpoints_dir, tree["task_id"], tree["repo"])
    if os.path.isdir(ck_dir) and not os.listdir(ck_dir):
        os.rmdir(ck_dir)
    return removed
