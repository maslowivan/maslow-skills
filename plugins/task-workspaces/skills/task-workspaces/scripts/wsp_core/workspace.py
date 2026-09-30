"""Task workspace lifecycle: ensure, status, release, checkpoint, evict, restore, close, run, gc."""

import getpass
import os
import shutil
import signal
import socket
import subprocess

import json

from . import checkpoint, deps, disk, gitutil, locks, manifests, procs, safety, sparse, util
from .errors import WspError

ACTIVE_STATES = ("ready",)


# --- identity -------------------------------------------------------------------
def detect_identity(holder=None, environ=None):
    """Return (app, session_id, holder, confirmed). Never invents a chat id."""
    environ = environ if environ is not None else os.environ
    if holder:
        app, _, session = holder.partition(":")
        return app or "cli", session or None, holder, False
    claude = environ.get("CLAUDE_CODE_SESSION_ID")
    if claude:
        return "claude", claude, f"claude:{claude}", True
    codex = environ.get("CODEX_THREAD_ID")
    if codex:
        return "codex", codex, f"codex:{codex}", True
    user = getpass.getuser()
    return "cli", None, f"cli:{user}@{socket.gethostname()}", False


class Ctx:
    def __init__(self, cfg, reg):
        self.cfg = cfg
        self.reg = reg

    @property
    def root(self):
        return safety.check_root(util.expand(self.cfg.data["worktrees_root"]))


# --- guards -----------------------------------------------------------------------
def root_ancestor_problems(root):
    """node_modules/package.json above the root would leak packages into trees without deps."""
    problems = []
    probe = os.path.dirname(os.path.realpath(root))
    home = os.path.realpath(os.path.expanduser("~"))
    while True:
        for name in ("node_modules", "package.json"):
            if os.path.exists(os.path.join(probe, name)):
                problems.append(os.path.join(probe, name))
        if probe in ("/", os.path.dirname(home)) or probe == os.path.dirname(probe):
            break
        probe = os.path.dirname(probe)
    return problems


def _guard_root(ctx):
    root = ctx.root
    problems = root_ancestor_problems(root)
    if problems:
        raise WspError("PATH_UNSAFE", "worktrees_root has node_modules/package.json in a parent directory; "
                       "trees without their own dependencies would resolve packages from there",
                       root=root, found=problems)
    os.makedirs(root, exist_ok=True)
    if ctx.cfg.data["exclusions"].get("spotlight"):
        marker = os.path.join(root, ".metadata_never_index")
        if not os.path.exists(marker):
            open(marker, "a").close()
    return root


def _tree_path(ctx, task_id, repo):
    return safety.inside(ctx.root, os.path.join(ctx.root, task_id, repo))


def _default_ref(repo_cfg):
    branch = repo_cfg.get("default_branch") or gitutil.default_branch(repo_cfg["path"])
    return branch, f"refs/remotes/origin/{branch}"


def _validate_source(repo_cfg):
    src = repo_cfg["path"]
    if not src or not os.path.isdir(src):
        raise WspError("SOURCE_MISSING", "source checkout not found", repo=repo_cfg["name"], path=src)
    if not gitutil.is_repo(src):
        raise WspError("SOURCE_INVALID", "source path is not a Git repository", path=src)
    origin = gitutil.origin_url(src)
    if repo_cfg.get("origin") and not gitutil.same_remote(origin, repo_cfg["origin"]):
        raise WspError("ORIGIN_MISMATCH", "source checkout origin differs from configuration",
                       repo=repo_cfg["name"], expected=repo_cfg["origin"], actual=origin)
    return src


def _estimate(ctx, repo):
    sizes = [t["size_bytes"] for t in ctx.reg.all_trees(include_closed=True)
             if t["repo"] == repo and t["size_bytes"]]
    repo_cfg = ctx.cfg.repo(repo)
    configured = repo_cfg.get("estimate_gib") or ctx.cfg.limits["default_tree_estimate_gib"]
    return max(sizes + [int(configured * util.GIB)])


def _op(ctx, kind, task_id, **detail):
    pid = os.getpid()
    return ctx.reg.begin_operation(kind, task_id, pid, procs.pid_start_time(pid), **detail)


# --- secrets -----------------------------------------------------------------------
def materialize_secrets(ctx, tree, profile):
    """Create local env/secret files from their configured source. Values are never logged."""
    done = []
    for item in checkpoint._secret_entries(profile):
        rel = item["path"]
        if any(ch in rel for ch in "*?["):
            continue  # patterns classify files; only concrete paths are materialised
        dst = os.path.join(tree["path"], rel)
        if os.path.lexists(dst):
            continue
        if tree.get("sparse_folders"):
            cone = json.loads(tree["sparse_folders"])
            parent = os.path.dirname(rel)
            if parent and not any(parent == f or parent.startswith(f + "/") or f.startswith(parent + "/")
                                  for f in cone):
                continue  # its folder is not checked out in this sparse tree
        source = item.get("source", "canonical")
        if source == "canonical":
            src = os.path.join(tree["source_path"], rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
                done.append({"path": rel, "source": "canonical"})
        elif source.startswith("command:"):
            proc = subprocess.run(source[len("command:"):], shell=True, cwd=tree["path"], capture_output=True,
                                  text=True, timeout=120)
            if proc.returncode == 0:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(proc.stdout)
                done.append({"path": rel, "source": "command"})
            else:
                done.append({"path": rel, "source": "command", "error": "command failed"})
    return done


# --- reconciliation ---------------------------------------------------------------
def reconcile(ctx, tree):
    """Compare registry, directory, Git registration and ownership marker. Never deletes."""
    if tree["state"] in ("evicted", "closed"):
        return tree
    path = tree["path"]
    src = tree["source_path"]
    registered = None
    if src and gitutil.is_repo(src):
        for wt in gitutil.worktree_list(src):
            if os.path.realpath(wt["path"]) == os.path.realpath(path):
                registered = wt
    exists = os.path.isdir(path)
    new_state = tree["state"]
    if not exists:
        new_state = "missing"
    else:
        marker = manifests.read_marker(path) if registered else None
        if registered and marker and marker.get("tree_id") == tree["id"]:
            if tree["state"] in ("missing", "unknown", "provisioning", "restoring", "snapshotting", "evicting"):
                new_state = "ready"
        else:
            new_state = "unknown"
    if new_state != tree["state"]:
        tree = ctx.reg.update_tree(tree["id"], state=new_state)
        ctx.reg.event("reconcile", tree["task_id"], tree["id"], state=new_state)
        manifests.write_manifest(ctx.cfg, ctx.reg, tree)
    return tree


# --- ensure -------------------------------------------------------------------------
def _sparse_requests(repos, profiles, folders):
    """Map --profile/--folder specs (NAME or REPO:NAME) to {repo: {"profiles": [...], "folders": [...]}}."""
    out = {}
    for kind, specs in (("profiles", profiles or []), ("folders", folders or [])):
        for spec in specs:
            repo, sep, value = spec.partition(":")
            if not sep or repo not in repos:
                if len(repos) != 1:
                    raise WspError("USAGE", f"with several repositories write --{kind[:-1]} REPO:{spec}", spec=spec)
                repo, value = repos[0], spec
            out.setdefault(repo, {"profiles": [], "folders": []})[kind].append(value)
    return out


def ensure(ctx, task_id, repos, title=None, tracker_ref=None, holder=None, base_ref=None, branch=None,
           from_remote_branch=False, allow_stale_base=False, packages=None, reclone_source=False,
           profiles=None, folders=None):
    util.validate_task_id(task_id)
    if not repos:
        raise WspError("USAGE", "at least one --repo is required")
    for repo in repos:
        ctx.cfg.repo(repo)
    sparse_req = _sparse_requests(repos, profiles, folders)
    app, session_id, holder, confirmed = detect_identity(holder)
    _guard_root(ctx)
    task = ctx.reg.get_task(task_id)
    new_task = task is None or not [t for t in ctx.reg.trees_for_task(task_id) if t["state"] != "evicted"]
    status = None if task and task["status"] in ("in_progress", "open") else "in_progress"
    ctx.reg.upsert_task(task_id, title=title, tracker_ref=tracker_ref, status=status)
    ctx.reg.attach_session(task_id, holder, app=app, session_id=session_id, confirmed=confirmed)

    results, created, warnings = [], [], []
    op = _op(ctx, "ensure", task_id, repos=repos)
    try:
        with locks.locked(ctx.cfg.state_dir, f"task:{task_id}"):
            for repo in repos:
                with locks.locked(ctx.cfg.state_dir, f"repo:{repo}"):
                    existing = ctx.reg.find_tree(task_id, repo)
                    if existing and existing["state"] != "closed":
                        existing = reconcile(ctx, existing)
                    if existing and existing["state"] == "ready":
                        info = _reuse(ctx, existing, holder, sparse_req.get(repo))
                    elif existing and existing["state"] in ("evicted", "missing", "recovery_failed"):
                        info = restore_tree(ctx, existing, holder, op, reclone_source=reclone_source)
                    elif existing and existing["state"] == "unknown":
                        raise WspError("UNKNOWN_OWNERSHIP", "tree path exists but ownership cannot be proven; "
                                       "inspect it manually (wsp status) before continuing", path=existing["path"])
                    else:
                        info = _create(ctx, task_id, repo, existing, holder, op, base_ref=base_ref, branch=branch,
                                       from_remote_branch=from_remote_branch, allow_stale_base=allow_stale_base,
                                       new_task=new_task and not created, sparse_req=sparse_req.get(repo))
                        created.append(info["tree_id"])
                    warnings.extend(info.pop("warnings", []))
                    results.append(info)
        dep_results = []
        for spec in packages or []:
            repo, _, pkg = spec.partition(":")
            tree = ctx.reg.find_tree(task_id, repo)
            if not tree:
                raise WspError("REPO_UNKNOWN", f"--deps refers to repo not in this task: {repo}")
            try:
                dep_results.append({"repo": repo, **deps.prepare(ctx.cfg, ctx.reg, tree, pkg or ".")})
            except WspError as exc:
                dep_results.append({"repo": repo, "package_dir": pkg or ".", "error": exc.to_dict()})
        ctx.reg.finish_operation(op, "done")
    except BaseException as exc:
        rolled_back = _rollback_created(ctx, created)
        ctx.reg.finish_operation(op, "failed", error=str(exc), rolled_back=rolled_back)
        if isinstance(exc, WspError):
            exc.details.setdefault("rolled_back", rolled_back)
            exc.details.setdefault("completed", [r["repo"] for r in results])
        raise
    summary = disk.summary(ctx.cfg, ctx.reg)
    if summary["level"] != "ok":
        warnings.append(f"disk level {summary['level']}: {summary['free']} free")
    return {"task": ctx.reg.get_task(task_id), "holder": holder, "session_confirmed": confirmed,
            "trees": results, "dependencies": dep_results, "disk": summary, "warnings": warnings}


def _reuse(ctx, tree, holder, sparse_req=None):
    widened = None
    if sparse_req:
        if tree["sparse_folders"]:
            widened = _widen(ctx, tree, sparse_req.get("profiles"), sparse_req.get("folders"))
            tree = ctx.reg.get_tree(tree["id"])
    lease, ok = ctx.reg.acquire_lease(tree["id"], holder)
    head = gitutil.rev_parse(tree["path"], "HEAD")
    branch = gitutil.out(tree["path"], "rev-parse", "--abbrev-ref", "HEAD")
    dirty = len(gitutil.status_entries(tree["path"]))
    warnings = []
    if branch != tree["branch"]:
        warnings.append(f"{tree['repo']}: HEAD is on '{branch}', registry expects '{tree['branch']}'")
    if not ok:
        warnings.append(f"{tree['repo']}: write lease is held by {lease['holder']}; you are an observer")
    if sparse_req and not tree["sparse_folders"]:
        warnings.append(f"{tree['repo']}: tree is a full checkout; sparse profile ignored")
    ctx.reg.update_tree(tree["id"], head_sha=head)
    return {"repo": tree["repo"], "tree_id": tree["id"], "path": tree["path"], "branch": branch, "head": head,
            "base_sha": tree["base_sha"], "base_fresh": bool(tree["base_fresh"]), "state": "ready", "action": "reused",
            "dirty_entries": dirty, "lease": "owner" if ok else "observer", **_sparse_info(tree),
            **({"sparse_added": widened["added"]} if widened else {}), "warnings": warnings}


def _sparse_info(tree):
    if not tree.get("sparse_folders"):
        return {"sparse": None}
    return {"sparse": {"profiles": json.loads(tree["sparse_profiles"] or "[]"),
                       "folders": json.loads(tree["sparse_folders"])}}


def _sparse_folders_for(src, rev, sparse_req):
    """Resolve requested profiles (from .wsp/SC-PROFILES.md at the base revision) and extra folders."""
    if not sparse_req:
        return None, None, None
    names = sparse_req.get("profiles") or []
    folders = []
    source = None
    if names:
        profiles, source = sparse.load_at(src, rev)
        folders = sparse.resolve(profiles, names, source)
    for extra in sparse_req.get("folders") or []:
        norm = sparse._norm_folder(extra)
        if not norm:
            raise WspError("PATH_UNSAFE", "invalid folder", folder=extra)
        folders.append(norm)
    return names, sparse._dedupe(folders), source


def _checkout_sparse(src, path, folders, warnings):
    """Populate a --no-checkout worktree with only `folders` (cone mode)."""
    had_worktree_config = sparse.worktree_config_enabled(src)
    sparse.apply(path, folders)
    if not had_worktree_config and sparse.worktree_config_enabled(src):
        warnings.append("git enabled extensions.worktreeConfig in the repository config (required for "
                        "per-worktree sparse checkout; the main checkout stays a full checkout)")


def _widen(ctx, tree, profile_names=None, folders=None):
    names, new_folders, _ = _sparse_folders_for(tree["source_path"], None, {"profiles": profile_names or [],
                                                                             "folders": folders or []})
    current = json.loads(tree["sparse_folders"] or "[]")
    added = [f for f in new_folders or [] if f not in current]
    if added:
        disk.admit(ctx.cfg, ctx.reg, 0, kind="sparse widen")
        sparse.add(tree["path"], added)
    all_names = sparse._dedupe(json.loads(tree["sparse_profiles"] or "[]") + (names or []))
    updated = ctx.reg.update_tree(tree["id"], sparse_folders=json.dumps(current + added),
                                  sparse_profiles=json.dumps(all_names),
                                  size_bytes=disk.dir_size(tree["path"]), size_measured_at=util.now_iso())
    manifests.write_manifest(ctx.cfg, ctx.reg, updated)
    return {"added": added, "folders": current + added, "profiles": all_names}


def _create(ctx, task_id, repo, existing, holder, op, base_ref=None, branch=None, from_remote_branch=False,
            allow_stale_base=False, new_task=False, sparse_req=None):
    repo_cfg = ctx.cfg.repo(repo)
    src = _validate_source(repo_cfg)
    default_branch, default_ref = _default_ref(repo_cfg)
    path = _tree_path(ctx, task_id, repo)
    if os.path.lexists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise WspError("PATH_CONFLICT", "target path already exists and is not empty", path=path)
    branch = branch or f"{ctx.cfg.branch_prefix}{task_id}"
    warnings = []
    fetch_cfg = ctx.cfg.data["fetch"]
    base_fresh = True
    if from_remote_branch:
        gitutil.fetch_with_retry(src, "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                                 fetch_cfg["retries"], fetch_cfg["backoff_seconds"], fetch_cfg["timeout_seconds"])
        start = gitutil.rev_parse(src, f"refs/remotes/origin/{branch}")
    else:
        if gitutil.local_branch_exists(src, branch):
            where = gitutil.branch_checked_out_at(src, branch)
            if where:
                raise WspError("BRANCH_BUSY", "branch is checked out in another worktree", branch=branch, path=where)
            raise WspError("BRANCH_EXISTS", "local branch already exists; pass --branch or --from-remote-branch",
                           branch=branch)
        if gitutil.remote_branch_exists(src, branch):
            raise WspError("BRANCH_EXISTS", "branch exists on origin; continue it with --from-remote-branch",
                           branch=branch)
        try:
            gitutil.fetch_with_retry(src, "origin", f"+refs/heads/{default_branch}:{default_ref}",
                                     fetch_cfg["retries"], fetch_cfg["backoff_seconds"], fetch_cfg["timeout_seconds"])
        except WspError as exc:
            if not allow_stale_base:
                raise
            base_fresh = False
            warnings.append(f"{repo}: fetch failed, using the last known {default_ref} (NOT fresh): {exc.message}")
        start = gitutil.rev_parse(src, base_ref) if base_ref else gitutil.rev_parse(src, default_ref)
        if base_ref:
            base_fresh = False if base_ref != default_ref else base_fresh
    if not start:
        raise WspError("GIT_FAILED", "base revision not found", base=base_ref or default_ref)
    profile_names, sparse_folders, sparse_source = _sparse_folders_for(src, start, sparse_req)

    estimate = _estimate(ctx, repo)
    disk.admit(ctx.cfg, ctx.reg, estimate, new_trees=1, new_task=new_task)
    ctx.reg.reserve(op, path, estimate)

    fields = dict(task_id=task_id, repo=repo, source_path=src, origin=gitutil.origin_url(src), path=path,
                  branch=branch, base_sha=start, base_fresh=1 if base_fresh else 0, head_sha=start,
                  state="provisioning", deps_state=None, size_bytes=None, remote_covered_sha=None,
                  sparse_profiles=json.dumps(profile_names) if sparse_folders else None,
                  sparse_folders=json.dumps(sparse_folders) if sparse_folders else None)
    if existing:
        tree = ctx.reg.update_tree(existing["id"], generation=existing["generation"] + 1, **fields)
    else:
        tree = ctx.reg.insert_tree(**fields)
    ctx.reg.operation_stage(op, f"create:{repo}", tree_id=tree["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if sparse_folders:
            gitutil.git(src, "worktree", "add", "--no-track", "--no-checkout", "-b", branch, path, start)
            _checkout_sparse(src, path, sparse_folders, warnings)
        else:
            gitutil.git(src, "worktree", "add", "--no-track", "-b", branch, path, start)
    except WspError:
        ctx.reg.update_tree(tree["id"], state="closed")
        raise
    gitutil.git(src, "worktree", "lock", "--reason", f"wsp:{task_id}", path)
    admin_id = os.path.basename(gitutil.absolute_git_dir(path))
    tree = ctx.reg.update_tree(tree["id"], admin_id=admin_id)
    manifests.write_marker(path, tree, ctx.cfg.state_dir)
    profile = ctx.cfg.profile(repo, path)
    secrets = materialize_secrets(ctx, tree, profile)
    size = disk.dir_size(path)
    tree = ctx.reg.update_tree(tree["id"], state="ready", size_bytes=size, size_measured_at=util.now_iso())
    manifests.write_manifest(ctx.cfg, ctx.reg, tree)
    lease, _ = ctx.reg.acquire_lease(tree["id"], holder)
    upstream = gitutil.git(path, "rev-parse", "--abbrev-ref", "@{upstream}", check=False)
    if upstream.returncode == 0:
        warnings.append(f"{repo}: unexpected upstream {upstream.stdout.strip()}")
    ctx.reg.event("create", task_id, tree["id"], branch=branch, base=start)
    return {"repo": repo, "tree_id": tree["id"], "path": path, "branch": branch, "head": start, "base_sha": start,
            "base_fresh": base_fresh, "state": "ready", "action": "created", "lease": "owner",
            "secrets_materialized": [s["path"] for s in secrets], "size": util.human_bytes(size),
            **_sparse_info(tree), **({"sparse_source": sparse_source} if sparse_folders else {}), "warnings": warnings}


def _rollback_created(ctx, tree_ids):
    """Undo trees created by a failed ensure, only while they are still pristine."""
    rolled = []
    for tree_id in tree_ids:
        tree = ctx.reg.get_tree(tree_id)
        if not tree or not os.path.isdir(tree["path"]):
            continue
        try:
            pristine = (not gitutil.status_entries(tree["path"])
                        and gitutil.rev_parse(tree["path"], "HEAD") == tree["base_sha"])
            if not pristine:
                continue
            src = tree["source_path"]
            gitutil.git(src, "worktree", "unlock", tree["path"], check=False)
            gitutil.git(src, "worktree", "remove", "--force", tree["path"])
            gitutil.git(src, "branch", "-D", tree["branch"], check=False)
            safety.remove_empty_dir(ctx.root, os.path.dirname(tree["path"]))
            ctx.reg.update_tree(tree_id, state="closed")
            rolled.append(tree["repo"])
        except WspError:
            continue
    return rolled


# --- restore ----------------------------------------------------------------------
def _drop_stale_registration(ctx, tree):
    """Remove only this tree's stale Git registration, proven by the marker in its admin dir."""
    src = tree["source_path"]
    for wt in gitutil.worktree_list(src):
        if os.path.realpath(wt["path"]) != os.path.realpath(tree["path"]):
            continue
        if os.path.exists(wt["path"]):
            raise WspError("RESTORE_CONFLICT", "a registered worktree already exists at the path", path=wt["path"])
        admin_dir = os.path.join(gitutil.common_dir(src), "worktrees", tree["admin_id"] or "")
        marker = manifests.read_marker_from_admin_dir(admin_dir) if tree["admin_id"] else None
        if not marker or marker.get("tree_id") != tree["id"]:
            raise WspError("UNKNOWN_OWNERSHIP", "stale registration at this path is not provably ours",
                           path=tree["path"], admin_dir=admin_dir)
        gitdir_file = os.path.join(admin_dir, "gitdir")
        with open(gitdir_file) as fh:
            recorded = fh.read().strip()
        if os.path.realpath(os.path.dirname(recorded)) != os.path.realpath(tree["path"]):
            raise WspError("UNKNOWN_OWNERSHIP", "admin dir points elsewhere", admin_dir=admin_dir, gitdir=recorded)
        shutil.rmtree(admin_dir)
        return admin_dir
    return None


def restore_tree(ctx, tree, holder, op=None, reclone_source=False):
    own_op = op is None
    if own_op:
        op = _op(ctx, "restore", tree["task_id"], repo=tree["repo"])
    try:
        result = _restore(ctx, tree, holder, op, reclone_source=reclone_source)
        if own_op:
            ctx.reg.finish_operation(op, "done")
        return result
    except BaseException as exc:
        if own_op:
            ctx.reg.finish_operation(op, "failed", error=str(exc))
        current = ctx.reg.get_tree(tree["id"])
        if current and current["state"] == "restoring":
            ctx.reg.update_tree(tree["id"], state="recovery_failed")
        raise


def _reclone_source(ctx, tree, repo_cfg):
    """Recreate a lost source checkout from origin. Only into a missing or empty directory."""
    target = repo_cfg["path"] or tree["source_path"]
    origin = repo_cfg.get("origin") or tree["origin"]
    if not target or not origin:
        raise WspError("SOURCE_MISSING", "cannot re-clone: repository path or origin unknown", repo=tree["repo"])
    if os.path.lexists(target) and (not os.path.isdir(target) or os.listdir(target)):
        raise WspError("SOURCE_INVALID", "source path exists but is not a Git repository; not overwriting it",
                       path=target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fetch_cfg = ctx.cfg.data["fetch"]
    util.run(["git", "clone", "--quiet", origin, target], code="FETCH_FAILED",
             timeout=max(fetch_cfg["timeout_seconds"], 3600))
    ctx.reg.event("reclone_source", tree["task_id"], tree["id"], path=target, origin=origin)
    return target


def _restore(ctx, tree, holder, op, reclone_source=False):
    repo_cfg = ctx.cfg.repo(tree["repo"])
    src = tree["source_path"] or repo_cfg["path"]
    warnings = []
    if not src or not gitutil.is_repo(src):
        if not reclone_source:
            raise WspError("SOURCE_MISSING", "source checkout is unavailable; rerun with --reclone-source to clone "
                           "it from origin and restore from the checkpoint bundle", repo=tree["repo"], path=src)
        src = _reclone_source(ctx, tree, repo_cfg)
        warnings.append(f"{tree['repo']}: source checkout re-cloned from origin into {src}")
    path = safety.inside(ctx.root, tree["path"])
    if os.path.lexists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise WspError("RESTORE_CONFLICT", "restore path is occupied by another directory", path=path)
    _guard_root(ctx)
    ck = ctx.reg.latest_checkpoint(tree["id"])
    if ck:
        how = checkpoint.ensure_objects(src, ck)
        if how != "present":
            warnings.append(f"{tree['repo']}: checkpoint objects recovered from bundle")
        checkpoint.verify(src, ck)
        head = ck["head_sha"]
    else:
        head = tree["remote_covered_sha"] or tree["head_sha"]
        if not gitutil.rev_parse(src, head or ""):
            gitutil.fetch_with_retry(src, "origin", f"+refs/heads/{tree['branch']}:refs/remotes/origin/{tree['branch']}")
            head = gitutil.rev_parse(src, f"refs/remotes/origin/{tree['branch']}")
        if not head:
            raise WspError("CHECKPOINT_MISSING", "no checkpoint and the recorded commit is unavailable",
                           tree=tree["id"])
    estimate = _estimate(ctx, tree["repo"])
    disk.admit(ctx.cfg, ctx.reg, estimate, new_trees=1, kind="restore")
    ctx.reg.reserve(op, path, estimate)
    ctx.reg.update_tree(tree["id"], state="restoring")
    dropped = _drop_stale_registration(ctx, tree)
    if dropped:
        warnings.append(f"{tree['repo']}: removed stale registration of the missing tree")

    branch = tree["branch"]
    sparse_folders = json.loads(tree["sparse_folders"]) if tree["sparse_folders"] else None
    if gitutil.local_branch_exists(src, branch):
        where = gitutil.branch_checked_out_at(src, branch)
        if where:
            raise WspError("BRANCH_BUSY", "branch is checked out elsewhere; not forcing", branch=branch, path=where)
        tip = gitutil.rev_parse(src, f"refs/heads/{branch}")
        if tip != head:
            if ck:
                raise WspError("RESTORE_CONFLICT", "branch moved since the checkpoint; restore would lose "
                               "either the branch or the checkpoint", branch=branch, tip=tip, checkpoint_head=head)
            head = tip
        add_args = ["worktree", "add"] + (["--no-checkout"] if sparse_folders else []) + [path, branch]
    else:
        add_args = (["worktree", "add", "--no-track"] + (["--no-checkout"] if sparse_folders else [])
                    + ["-b", branch, path, head])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    gitutil.git(src, *add_args)
    if sparse_folders:
        _checkout_sparse(src, path, sparse_folders, warnings)
    gitutil.git(src, "worktree", "lock", "--reason", f"wsp:{tree['task_id']}", path)
    admin_id = os.path.basename(gitutil.absolute_git_dir(path))
    tree = ctx.reg.update_tree(tree["id"], admin_id=admin_id, generation=tree["generation"] + 1, source_path=src)
    manifests.write_marker(path, tree, ctx.cfg.state_dir)
    restored = None
    if ck:
        restored = checkpoint.restore_into(path, ck)
    profile = ctx.cfg.profile(tree["repo"], path)
    secrets = materialize_secrets(ctx, tree, profile)
    dep_results = []
    for use in ctx.reg.dep_uses_for_tree(tree["id"]):
        try:
            dep_results.append(deps.prepare(ctx.cfg, ctx.reg, tree, use["package_dir"], profile=profile))
        except WspError as exc:
            warnings.append(f"{tree['repo']}: dependencies for {use['package_dir']} not prepared: {exc.message}")
    size = disk.dir_size(path)
    tree = ctx.reg.update_tree(tree["id"], state="ready", head_sha=head, size_bytes=size,
                               size_measured_at=util.now_iso())
    manifests.write_manifest(ctx.cfg, ctx.reg, tree)
    ctx.reg.acquire_lease(tree["id"], holder)
    ctx.reg.event("restore", tree["task_id"], tree["id"], checkpoint=ck["seq"] if ck else None, head=head)
    return {"repo": tree["repo"], "tree_id": tree["id"], "path": path, "branch": branch, "head": head,
            "base_sha": tree["base_sha"], "base_fresh": bool(tree["base_fresh"]), "state": "ready",
            "action": "restored", "from_checkpoint": ck["seq"] if ck else None,
            "checkpoint_time": ck["created_at"] if ck else None, "verified": bool(restored) or not ck,
            "secrets_materialized": [s["path"] for s in secrets], "dependencies": dep_results,
            "lease": "owner", **_sparse_info(tree), "warnings": warnings}


def restore(ctx, task_id, repos=None, holder=None, reclone_source=False):
    trees = _select_trees(ctx, task_id, repos)
    _, _, holder, _ = detect_identity(holder)
    out = []
    with locks.locked(ctx.cfg.state_dir, f"task:{task_id}"):
        for tree in trees:
            tree = reconcile(ctx, tree)
            if tree["state"] == "ready":
                out.append(_reuse(ctx, tree, holder))
            elif tree["state"] in ("evicted", "missing", "recovery_failed"):
                out.append(restore_tree(ctx, tree, holder, reclone_source=reclone_source))
            else:
                raise WspError("RESTORE_CONFLICT", f"tree state {tree['state']} cannot be restored", repo=tree["repo"])
    return {"task": task_id, "trees": out}


# --- selection & status -------------------------------------------------------------
def _select_trees(ctx, task_id, repos=None, include_closed=False):
    if not ctx.reg.get_task(task_id):
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    trees = ctx.reg.trees_for_task(task_id, include_closed=include_closed)
    if repos:
        trees = [t for t in trees if t["repo"] in repos]
        missing = set(repos) - {t["repo"] for t in trees}
        if missing:
            raise WspError("TREE_UNKNOWN", "task has no tree for these repos", repos=sorted(missing))
    return trees


def tree_status(ctx, tree, detailed=True):
    tree = reconcile(ctx, tree)
    info = {"repo": tree["repo"], "tree_id": tree["id"], "path": tree["path"], "state": tree["state"],
            "branch": tree["branch"], "base_sha": tree["base_sha"], "base_fresh": bool(tree["base_fresh"]),
            "generation": tree["generation"], "leases": ctx.reg.active_leases(tree["id"]), **_sparse_info(tree)}
    ck = ctx.reg.latest_checkpoint(tree["id"])
    info["last_checkpoint"] = {"seq": ck["seq"], "at": ck["created_at"], "head": ck["head_sha"]} if ck else None
    if tree["state"] == "ready" and detailed:
        repo_cfg = ctx.cfg.repo(tree["repo"])
        _, default_ref = _default_ref(repo_cfg)
        state = checkpoint.unique_state(tree["path"], ctx.cfg.profile(tree["repo"], tree["path"]), default_ref)
        info.update(head=state["head"], dirty_entries=len(state["dirty"]), unpushed_commits=state["unpushed_commits"],
                    covered_by_remote=state["covered_by_remote"], unique_local_state=state["unique"],
                    unclassified_ignored=state["ignored"]["unclassified"][:50],
                    processes=procs.cwd_processes(tree["path"]),
                    dependencies=deps.check(ctx.cfg, ctx.reg, tree))
        if ck and state["unique"]:
            info["unsaved_since_checkpoint"] = True
    elif tree["state"] == "missing":
        info["recoverable_to"] = ("checkpoint #%s at %s" % (ck["seq"], ck["created_at"])) if ck else (
            "remote/branch commit %s" % (tree["remote_covered_sha"] or tree["head_sha"]))
    return info


def status(ctx, task_id):
    task = ctx.reg.get_task(task_id)
    if not task:
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    trees = [tree_status(ctx, t) for t in ctx.reg.trees_for_task(task_id)]
    set_manifest = manifests.task_manifest_path(ctx.cfg, task_id)
    return {"task": task, "sessions": ctx.reg.sessions_for_task(task_id), "trees": trees,
            "set_manifest": set_manifest if os.path.exists(set_manifest) else None}


def list_all(ctx, show_all=False):
    """Tasks with their trees. Finished tasks (completed/cancelled) that no longer have a tree
    are history and hidden unless show_all."""
    tasks = []
    hidden = 0
    for task in ctx.reg.list_tasks():
        trees = ctx.reg.trees_for_task(task["id"])
        trees = [reconcile(ctx, t) for t in trees]
        if not show_all and task["status"] in ("completed", "cancelled") and not trees:
            hidden += 1
            continue
        tasks.append({
            "id": task["id"], "title": task["title"], "status": task["status"], "pinned": bool(task["pinned"]),
            "trees": [{"repo": t["repo"], "state": t["state"], "path": t["path"], "branch": t["branch"],
                       "size": util.human_bytes(t["size_bytes"]),
                       "leases": [l["holder"] for l in ctx.reg.active_leases(t["id"])]} for t in trees],
            "sessions": [s["holder"] for s in ctx.reg.sessions_for_task(task["id"])],
        })
    return {"tasks": tasks, "hidden_finished_tasks": hidden, "disk": disk.summary(ctx.cfg, ctx.reg),
            "running_operations": ctx.reg.running_operations()}


def attach(ctx, task_id, holder=None, app=None, session=None, role="owner"):
    if not ctx.reg.get_task(task_id):
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    if app and session:
        holder, confirmed = f"{app}:{session}", False
        env_app, env_session, _, env_confirmed = detect_identity(None)
        if env_app == app and env_session == session:
            confirmed = env_confirmed
    else:
        app, session, holder, confirmed = detect_identity(holder)
    ctx.reg.attach_session(task_id, holder, app=app, session_id=session, confirmed=confirmed, role=role)
    return {"task": task_id, "holder": holder, "confirmed": confirmed, "role": role}


# --- checkpoint / release -------------------------------------------------------------
def checkpoint_task(ctx, task_id, repos=None, reason="manual"):
    out = []
    for tree in _select_trees(ctx, task_id, repos):
        tree = reconcile(ctx, tree)
        if tree["state"] != "ready":
            out.append({"repo": tree["repo"], "skipped": tree["state"]})
            continue
        with locks.locked(ctx.cfg.state_dir, f"tree:{tree['id']}"):
            repo_cfg = ctx.cfg.repo(tree["repo"])
            _, default_ref = _default_ref(repo_cfg)
            res = checkpoint.create(ctx.cfg, ctx.reg, tree, ctx.cfg.profile(tree["repo"], tree["path"]),
                                    default_ref, reason)
            entry = {"repo": tree["repo"], "created": res["created"]}
            if res["created"]:
                ck = res["checkpoint"]
                entry.update(seq=ck["seq"], ref=ck["ref"], at=ck["created_at"],
                             dirty_entries=len(res["state"]["dirty"]),
                             unpushed_commits=res["state"]["unpushed_commits"])
            else:
                entry.update(reason=res["reason"], head=res["head"], deleted_checkpoints=res["deleted_checkpoints"])
            entry["unclassified_ignored"] = res["state"]["ignored"]["unclassified"][:50]
            out.append(entry)
    warning = disk.checkpoint_usage_warning(ctx.cfg)
    return {"task": task_id, "trees": out, "set_manifest": manifests.task_manifest_path(ctx.cfg, task_id),
            "warnings": [warning] if warning else []}


def release(ctx, task_id, holder=None, repos=None, pause=False, force=False):
    _, _, holder, _ = detect_identity(holder)
    out = []
    for tree in _select_trees(ctx, task_id, repos):
        tree = reconcile(ctx, tree)
        mine = [l for l in ctx.reg.active_leases(tree["id"]) if l["holder"] == holder]
        if tree["state"] == "ready":
            running = procs.cwd_processes(tree["path"])
            if running and not force:
                raise WspError("PROCESS_RUNNING", "processes still use the tree; stop them first",
                               repo=tree["repo"], processes=running)
        entry = {"repo": tree["repo"], "released": len(mine)}
        for lease in mine:
            ctx.reg.release_lease(lease["id"])
        out.append(entry)
    ck = None
    if pause:
        ck = checkpoint_task(ctx, task_id, repos, reason="pause")
        ctx.reg.set_task_status(task_id, "paused")
    return {"task": task_id, "holder": holder, "trees": out, "paused": pause, "checkpoint": ck}


# --- evict -----------------------------------------------------------------------------
def _assert_evictable(ctx, tree, holder, accept_unclassified):
    others = [l for l in ctx.reg.active_leases(tree["id"]) if l["holder"] != holder]
    if others:
        raise WspError("LEASE_HELD", "another owner holds this tree", repo=tree["repo"],
                       holders=[l["holder"] for l in others])
    running = procs.cwd_processes(tree["path"])
    if running is None:
        raise WspError("PROCESS_RUNNING", "cannot verify processes (lsof unavailable); refusing to remove")
    if running:
        raise WspError("PROCESS_RUNNING", "processes still use the tree", repo=tree["repo"], processes=running)
    marker = manifests.read_marker(tree["path"])
    if not marker or marker.get("tree_id") != tree["id"]:
        raise WspError("UNKNOWN_OWNERSHIP", "ownership marker does not match; refusing to remove", path=tree["path"])


def _remove_worktree(ctx, tree):
    src = tree["source_path"]
    path = safety.inside(ctx.root, tree["path"])
    inode = os.stat(path).st_ino
    gitutil.git(src, "worktree", "unlock", path, check=False)
    if os.stat(path).st_ino != inode:
        raise WspError("PATH_UNSAFE", "tree changed identity during removal", path=path)
    proc = gitutil.git(src, "worktree", "remove", "--force", "--force", path, check=False)
    if proc.returncode != 0:
        gitutil.git(src, "worktree", "lock", "--reason", f"wsp:{tree['task_id']}", path, check=False)
        raise WspError("GIT_FAILED", "git worktree remove failed; tree kept", stderr=proc.stderr.strip()[-800:])
    if os.path.exists(path):
        raise WspError("GIT_FAILED", "tree directory still exists after removal", path=path)
    safety.remove_empty_dir(ctx.root, os.path.dirname(path))


def evict(ctx, task_id, repos=None, holder=None, accept_unclassified=False, reason="evict"):
    task = ctx.reg.get_task(task_id)
    if not task:
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    if task["pinned"]:
        raise WspError("PINNED", "task is pinned; unpin it first", task_id=task_id)
    _, _, holder, _ = detect_identity(holder)
    out = []
    op = _op(ctx, "evict", task_id)
    try:
        with locks.locked(ctx.cfg.state_dir, f"task:{task_id}"):
            for tree in _select_trees(ctx, task_id, repos):
                tree = reconcile(ctx, tree)
                if tree["state"] != "ready":
                    out.append({"repo": tree["repo"], "skipped": tree["state"]})
                    continue
                with locks.locked(ctx.cfg.state_dir, f"repo:{tree['repo']}", f"tree:{tree['id']}"):
                    out.append(_evict_tree(ctx, tree, holder, accept_unclassified, reason, op))
        ctx.reg.finish_operation(op, "done")
    except BaseException as exc:
        ctx.reg.finish_operation(op, "failed", error=str(exc))
        raise
    return {"task": task_id, "trees": out, "disk": disk.summary(ctx.cfg, ctx.reg)}


def _evict_tree(ctx, tree, holder, accept_unclassified, reason, op):
    _assert_evictable(ctx, tree, holder, accept_unclassified)
    repo_cfg = ctx.cfg.repo(tree["repo"])
    _, default_ref = _default_ref(repo_cfg)
    profile = ctx.cfg.profile(tree["repo"], tree["path"])
    ctx.reg.update_tree(tree["id"], state="snapshotting")
    ctx.reg.operation_stage(op, f"snapshot:{tree['repo']}")
    try:
        state = checkpoint.unique_state(tree["path"], profile, default_ref)
        if state["ignored"]["unclassified"] and not accept_unclassified:
            raise WspError("UNCLASSIFIED_IGNORED", "ignored files of unknown kind would be lost; classify them in "
                           "the profile (preserve_ignored / reproducible_ignored) or pass --accept-unclassified",
                           repo=tree["repo"], files=state["ignored"]["unclassified"][:100])
        free_before = disk.free_bytes(ctx.root)
        res = checkpoint.create(ctx.cfg, ctx.reg, tree, profile, default_ref, reason, state=state)
        # re-check that nothing changed after the snapshot
        again = checkpoint.unique_state(tree["path"], profile, default_ref)
        if res["created"]:
            index_tree, work_tree = checkpoint.snapshot_trees(tree["path"])
            ck = res["checkpoint"]
            if (index_tree, work_tree) != (ck["index_tree"], ck["work_tree"]) or again["head"] != ck["head_sha"]:
                raise WspError("UNIQUE_STATE", "tree changed during eviction; kept", repo=tree["repo"])
        elif again["unique"]:
            raise WspError("UNIQUE_STATE", "tree changed during eviction; kept", repo=tree["repo"])
    except BaseException:
        ctx.reg.update_tree(tree["id"], state="ready")
        raise
    ctx.reg.update_tree(tree["id"], state="evicting")
    ctx.reg.operation_stage(op, f"remove:{tree['repo']}")
    try:
        _remove_worktree(ctx, tree)
    except BaseException:
        ctx.reg.update_tree(tree["id"], state="ready")
        raise
    for lease in ctx.reg.active_leases(tree["id"]):
        ctx.reg.release_lease(lease["id"])
    ctx.reg.mark_dep_uses_evicted(tree["id"])
    tree = ctx.reg.update_tree(tree["id"], state="evicted", size_bytes=0, head_sha=state["head"])
    manifests.write_manifest(ctx.cfg, ctx.reg, tree)
    ctx.reg.event("evict", tree["task_id"], tree["id"], checkpoint=res["created"])
    return {"repo": tree["repo"], "state": "evicted", "checkpoint": res["checkpoint"]["seq"] if res["created"] else None,
            "restorable_from": "checkpoint" if res["created"] else f"branch {tree['branch']} @ {state['head'][:12]}",
            "free_before": util.human_bytes(free_before), "free_after": util.human_bytes(disk.free_bytes(ctx.root))}


# --- close -------------------------------------------------------------------------------
def close(ctx, task_id, status="completed", discard=False, holder=None, keep_branch=False):
    if status not in ("completed", "cancelled"):
        raise WspError("USAGE", "status must be completed or cancelled")
    task = ctx.reg.get_task(task_id)
    if not task:
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    _, _, holder, _ = detect_identity(holder)
    trees = [reconcile(ctx, t) for t in ctx.reg.trees_for_task(task_id)]
    would_lose, blockers = [], []
    states = {}
    for tree in trees:
        repo_cfg = ctx.cfg.repo(tree["repo"])
        _, default_ref = _default_ref(repo_cfg)
        if tree["state"] == "ready":
            try:
                _assert_evictable(ctx, tree, holder, True)
            except WspError as exc:
                blockers.append({"repo": tree["repo"], **exc.to_dict()})
                continue
            st = checkpoint.unique_state(tree["path"], ctx.cfg.profile(tree["repo"], tree["path"]), default_ref)
            states[tree["id"]] = st
            if st["unique"]:
                would_lose.append({"repo": tree["repo"], "dirty": st["dirty"][:50],
                                   "unpushed_commits": st["unpushed_commits"],
                                   "preserved_ignored": st["ignored"]["preserve"][:50]})
        elif tree["state"] in ("evicted", "missing"):
            cks = ctx.reg.valid_checkpoints(tree["id"])
            if cks:
                would_lose.append({"repo": tree["repo"], "checkpoints": [c["seq"] for c in cks]})
        elif tree["state"] == "unknown":
            blockers.append({"repo": tree["repo"], "code": "UNKNOWN_OWNERSHIP", "message": "ownership unproven"})
    if blockers:
        raise WspError("LEASE_HELD" if any(b.get("code") == "LEASE_HELD" for b in blockers) else "PROCESS_RUNNING"
                       if any(b.get("code") == "PROCESS_RUNNING" for b in blockers) else "UNKNOWN_OWNERSHIP",
                       "some trees cannot be removed now", blockers=blockers)
    if would_lose and not discard:
        raise WspError("UNIQUE_STATE", "closing would discard local work that is not on the remote; "
                       "show this to the user and rerun with --discard only after explicit confirmation",
                       would_lose=would_lose)
    out = []
    with locks.locked(ctx.cfg.state_dir, f"task:{task_id}"):
        for tree in trees:
            entry = {"repo": tree["repo"], "previous_state": tree["state"]}
            if tree["state"] == "ready":
                _remove_worktree(ctx, tree)
                entry["removed_tree"] = True
            src = tree["source_path"]
            if src and gitutil.is_repo(src) and not keep_branch and tree["branch"]:
                if gitutil.local_branch_exists(src, tree["branch"]) and not gitutil.branch_checked_out_at(src, tree["branch"]):
                    gitutil.git(src, "branch", "-D", tree["branch"], check=False)
                    entry["deleted_branch"] = tree["branch"]
            entry["deleted_checkpoints"] = checkpoint.delete_all(ctx.cfg, ctx.reg, tree)
            for lease in ctx.reg.active_leases(tree["id"]):
                ctx.reg.release_lease(lease["id"])
            ctx.reg.clear_dep_uses(tree["id"])
            updated = ctx.reg.update_tree(tree["id"], state="closed", size_bytes=0)
            manifests.write_manifest(ctx.cfg, ctx.reg, updated)
            out.append(entry)
        safety.remove_empty_dir(ctx.root, os.path.join(ctx.root, task_id))
        ctx.reg.set_task_status(task_id, status)
    ctx.reg.event("close", task_id, None, status=status, discarded=bool(would_lose))
    return {"task": task_id, "status": status, "trees": out, "discarded": would_lose}


def set_pin(ctx, task_id, pinned):
    if not ctx.reg.get_task(task_id):
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    ctx.reg.set_pinned(task_id, pinned)
    return {"task": task_id, "pinned": pinned}


def set_task_status(ctx, task_id, status):
    if status not in ("open", "in_progress", "paused"):
        raise WspError("USAGE", "use `wsp close` for completed/cancelled")
    if not ctx.reg.get_task(task_id):
        raise WspError("TASK_UNKNOWN", f"unknown task {task_id}", task_id=task_id)
    ctx.reg.set_task_status(task_id, status)
    return {"task": task_id, "status": status}


# --- run -----------------------------------------------------------------------------------
def run(ctx, task_id, repo, command, package_dir=None, holder=None, skip_deps_check=False, poll_seconds=15,
        max_growth_gib=None):
    """Run a command in the tree under the write lease.

    Stops only this command (its process group) when free space falls below the
    emergency floor or the command consumed more than its disk budget
    (max_growth_gib, default limits.run_max_growth_gib), then checkpoints."""
    tree = ctx.reg.find_tree(task_id, repo)
    if not tree:
        raise WspError("TREE_UNKNOWN", "no such tree; run `wsp ensure` first", task=task_id, repo=repo)
    tree = reconcile(ctx, tree)
    if tree["state"] != "ready":
        raise WspError("TREE_UNKNOWN", f"tree is {tree['state']}; run `wsp ensure` to restore it", repo=repo)
    _, _, holder, _ = detect_identity(holder)
    lease, ok = ctx.reg.acquire_lease(tree["id"], holder)
    if not ok:
        raise WspError("LEASE_HELD", "another owner holds the write lease", holder=lease["holder"])
    cwd = os.path.normpath(os.path.join(tree["path"], package_dir or "."))
    if os.path.relpath(cwd, tree["path"]).startswith(".."):
        raise WspError("PATH_UNSAFE", "package dir escapes the tree", package_dir=package_dir)
    if not skip_deps_check:
        stale = [d for d in deps.check(ctx.cfg, ctx.reg, tree) if d["state"] == "stale"]
        lock_rel = deps.find_lock_dir(tree["path"], package_dir or ".")
        stale = [d for d in stale if d["lock_dir"] == lock_rel]
        if stale:
            raise WspError("DEPS_INCOMPATIBLE", "dependency inputs changed since preparation; run `wsp deps` again",
                           stale=stale)
    limits = ctx.cfg.limits
    poll_seconds = float(os.environ.get("WSP_RUN_POLL_SECONDS", poll_seconds))
    budget_gib = max_growth_gib if max_growth_gib is not None else limits.get("run_max_growth_gib")
    free_start = disk.free_bytes(ctx.root)
    if free_start < limits["emergency_free_gib"] * util.GIB:
        raise WspError("DISK_LOW", "free space is below the emergency floor")
    proc = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    ctx.reg.set_lease_process(lease["id"], proc.pid, procs.pid_start_time(proc.pid), proc.pid)
    stop_reason = None
    try:
        while True:
            try:
                code = proc.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                ctx.reg.heartbeat(lease["id"])
                free_now = disk.free_bytes(ctx.root)
                if free_now < limits["emergency_free_gib"] * util.GIB:
                    stop_reason = "free space fell below the emergency floor"
                elif budget_gib is not None and free_start - free_now > budget_gib * util.GIB:
                    stop_reason = (f"the command used {util.human_bytes(free_start - free_now)}, more than its "
                                   f"{budget_gib} GiB disk budget")
                if stop_reason:
                    _kill_group(proc)
    except KeyboardInterrupt:
        _kill_group(proc, sig=signal.SIGINT)
        code = proc.wait()
    finally:
        ctx.reg.set_lease_process(lease["id"], None, None, None)
    if stop_reason:
        try:
            checkpoint_task(ctx, task_id, [repo], reason="disk-stop")
        finally:
            raise WspError("DISK_LOW", f"command stopped: {stop_reason}; tree state checkpointed",
                           exit_code=code, free_before=util.human_bytes(free_start),
                           free_after=util.human_bytes(disk.free_bytes(ctx.root)))
    return code


def _kill_group(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# --- gc ------------------------------------------------------------------------------------
def gc(ctx, apply=False):
    policy = ctx.cfg.policy
    plan = []
    # leases: expired heartbeat is a signal to reconcile, never permission to delete
    stale_hours = float(policy.get("lease_stale_hours", 12))
    for tree in ctx.reg.all_trees():
        for lease in ctx.reg.active_leases(tree["id"]):
            pid_dead = lease["pid"] and not procs.pid_alive(lease["pid"], lease["pid_start"])
            if pid_dead:
                plan.append({"action": "clear_dead_process", "task": tree["task_id"], "repo": tree["repo"],
                             "holder": lease["holder"], "pid": lease["pid"]})
                if apply:
                    ctx.reg.set_lease_process(lease["id"], None, None, None)
            age = util.age_hours(lease["heartbeat_at"]) or 0
            alive_run = lease["pid"] and not pid_dead
            if lease["state"] == "held" and age >= stale_hours and not alive_run:
                plan.append({"action": "mark_lease_uncertain", "task": tree["task_id"], "repo": tree["repo"],
                             "holder": lease["holder"], "idle_hours": round(age, 1),
                             "why": "no heartbeat; the tree stays protected until the holder releases it"})
                if apply:
                    ctx.reg.release_lease(lease["id"], state="uncertain")
    # abandoned operations
    for op in ctx.reg.running_operations():
        if not procs.pid_alive(op["pid"], op["pid_start"]):
            plan.append({"action": "mark_operation_abandoned", "operation": op["id"], "kind": op["kind"]})
            if apply:
                ctx.reg.finish_operation(op["id"], "abandoned")
    for task in ctx.reg.list_tasks():
        trees = [reconcile(ctx, t) for t in ctx.reg.trees_for_task(task["id"])]
        if not trees:
            continue
        if task["pinned"]:
            plan.append({"action": "keep", "task": task["id"], "why": "pinned"})
            continue
        if any(t["state"] == "unknown" for t in trees):
            plan.append({"action": "keep", "task": task["id"], "why": "unknown ownership"})
            continue
        leases = [l for t in trees for l in ctx.reg.active_leases(t["id"])]
        if task["status"] in ("completed", "cancelled"):
            if leases:
                plan.append({"action": "keep", "task": task["id"], "why": "held lease",
                             "holders": [l["holder"] for l in leases]})
                continue
            try:
                preview = _close_preview(ctx, task["id"], trees)
            except WspError as exc:
                plan.append({"action": "keep", "task": task["id"], "why": exc.code})
                continue
            if preview:
                plan.append({"action": "needs_confirmation", "task": task["id"], "would_lose": preview})
                continue
            plan.append({"action": "close_cleanup", "task": task["id"]})
            if apply:
                close(ctx, task["id"], status=task["status"], holder="gc:janitor")
            continue
        if task["status"] == "paused" and not leases:
            ready = [t for t in trees if t["state"] == "ready"]
            age = util.age_hours(task["updated_at"]) or 0
            if ready and age >= policy["paused_evict_after_hours"]:
                plan.append({"action": "evict", "task": task["id"], "paused_hours": round(age, 1)})
                if apply:
                    try:
                        evict(ctx, task["id"], holder="gc:janitor", reason="gc-paused")
                    except WspError as exc:
                        plan[-1]["result"] = exc.to_dict()
            continue
        last = max((t["updated_at"] or "") for t in trees)
        heartbeat = max([(l["heartbeat_at"] or "") for l in leases] + [last])
        age_days = (util.age_hours(heartbeat) or 0) / 24
        if age_days >= policy["stale_review_after_days"]:
            plan.append({"action": "review", "task": task["id"], "idle_days": round(age_days, 1),
                         "why": "old activity alone never allows removal"})
    # checkpoints whose content reached the remote
    for tree in ctx.reg.all_trees():
        if tree["state"] not in ("evicted", "missing"):
            continue
        for ck in ctx.reg.valid_checkpoints(tree["id"]):
            manifest = util.read_json(ck["manifest_path"] or "", {}) or {}
            clean = manifest.get("dirty_entries", 1) == 0 and not ck["ignored_archive"]
            src = tree["source_path"]
            if clean and src and gitutil.is_repo(src):
                repo_cfg = ctx.cfg.repo(tree["repo"])
                _, default_ref = _default_ref(repo_cfg)
                if gitutil.covered_by_remote(src, ck["head_sha"], default_ref)["covered"]:
                    plan.append({"action": "delete_checkpoint", "task": tree["task_id"], "repo": tree["repo"],
                                 "seq": ck["seq"], "why": "content is on the remote"})
                    if apply:
                        checkpoint._delete_one(ctx.cfg, ctx.reg, tree, ck, src)
                        if not ctx.reg.valid_checkpoints(tree["id"]):
                            ctx.reg.update_tree(tree["id"], remote_covered_sha=ck["head_sha"])
    cache = deps.gc_cache(ctx.cfg, ctx.reg, apply=apply)
    for cand in cache["candidates"]:
        plan.append({"action": "delete_dependency_instance", **cand})
    return {"applied": apply, "plan": plan, "disk": disk.summary(ctx.cfg, ctx.reg)}


def _close_preview(ctx, task_id, trees):
    lose = []
    for tree in trees:
        repo_cfg = ctx.cfg.repo(tree["repo"])
        _, default_ref = _default_ref(repo_cfg)
        if tree["state"] == "ready":
            running = procs.cwd_processes(tree["path"])
            if running is None or running:
                raise WspError("PROCESS_RUNNING", "processes use the tree")
            st = checkpoint.unique_state(tree["path"], ctx.cfg.profile(tree["repo"], tree["path"]), default_ref)
            if st["unique"]:
                lose.append({"repo": tree["repo"], "dirty": len(st["dirty"]), "unpushed": st["unpushed_commits"]})
        elif ctx.reg.valid_checkpoints(tree["id"]):
            lose.append({"repo": tree["repo"], "checkpoints": len(ctx.reg.valid_checkpoints(tree["id"]))})
    return lose


# --- registry rebuild --------------------------------------------------------------------
def rebuild_registry(ctx):
    """Recreate missing registry rows from manifests. Existing rows win; nothing is guessed."""
    restored_tasks, restored_trees, restored_cks = [], [], 0
    for data in manifests.read_manifests(ctx.cfg):
        task = data.get("task")
        tree = data.get("tree")
        if not task or not tree:
            continue
        if not ctx.reg.get_task(task["id"]):
            ctx.reg.upsert_task(task["id"], title=task.get("title"), tracker_ref=task.get("tracker_ref"),
                                status=task.get("status"))
            if task.get("pinned"):
                ctx.reg.set_pinned(task["id"], True)
            restored_tasks.append(task["id"])
        if not ctx.reg.get_tree(tree["id"]):
            fields = {k: v for k, v in tree.items() if k not in ("updated_at",)}
            fields["state"] = "unknown" if tree["state"] not in ("evicted", "closed") else tree["state"]
            ctx.reg.insert_tree(**fields)
            restored_trees.append(f"{tree['task_id']}/{tree['repo']}")
            for ck in data.get("checkpoints") or []:
                ck = {k: v for k, v in ck.items() if k != "id"}
                ctx.reg.insert_checkpoint(**ck)
                restored_cks += 1
            restored = ctx.reg.get_tree(tree["id"])
            reconcile(ctx, restored)
        for s in data.get("sessions") or []:
            ctx.reg.attach_session(tree["task_id"], s["holder"], app=s.get("app"), session_id=s.get("session_id"),
                                   confirmed=bool(s.get("confirmed")), role=s.get("role") or "owner")
    return {"tasks": restored_tasks, "trees": restored_trees, "checkpoints": restored_cks}



def sparse_widen(ctx, task_id, repo, profiles=None, folders=None):
    tree = ctx.reg.find_tree(task_id, repo)
    if not tree:
        raise WspError("TREE_UNKNOWN", "no such tree", task=task_id, repo=repo)
    tree = reconcile(ctx, tree)
    if tree["state"] != "ready":
        raise WspError("TREE_UNKNOWN", f"tree is {tree['state']}; run `wsp ensure` first", repo=repo)
    if not tree["sparse_folders"]:
        raise WspError("USAGE", "this tree is a full checkout; nothing to add", repo=repo)
    with locks.locked(ctx.cfg.state_dir, f"tree:{tree['id']}"):
        result = _widen(ctx, tree, profiles, folders)
    return {"task": task_id, "repo": repo, **result}
