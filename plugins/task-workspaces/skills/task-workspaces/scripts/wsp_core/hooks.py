"""Claude Code hook handlers (WorktreeCreate, WorktreeRemove, SessionStart, SessionEnd).

Hooks are thin: they read the event JSON from stdin and call the same core as the CLI.

Safety rules that come from Claude Code's hook contract:
  * WorktreeCreate has no fallback: a non-zero exit fails worktree creation, so
    repositories outside the wsp configuration get the standard git behaviour here.
  * WorktreeRemove: if the hook exits non-zero and the directory still exists,
    Claude Code runs `rm -rf` on it. This handler therefore ALWAYS exits 0 and
    never lets an error turn into deletion of unsaved work.
"""

import json
import os
import sys
import traceback

from . import checkpoint, config as config_mod, gitutil, janitor, procs, util, workspace
from .errors import WspError
from .registry import Registry


def _log(cfg, event, **data):
    try:
        path = os.path.join(cfg.state_dir, "logs", "hooks.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": util.now_iso(), "event": event, **data}, default=str) + "\n")
    except Exception:
        pass


def _load(sync_options=True):
    cfg = config_mod.Config.load()
    if sync_options:
        changed = cfg.apply_plugin_options()
        if changed:
            cfg.save()
    return cfg


def _read_event(stdin):
    raw = stdin.read()
    return json.loads(raw) if raw.strip() else {}


def worktree_create(event, stdout):
    parent = event.get("parent_worktree_path") or event.get("cwd") or os.getcwd()
    branch = event.get("branch") or ""
    commit = event.get("commit")
    session = event.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    cfg = _load()
    top = gitutil.toplevel(parent)
    repo = cfg.find_repo_by_path(top) if cfg.exists else None
    if repo:
        task_id = util.slugify(branch) or f"claude-{(session or 'session')[:8]}"
        reg = Registry(cfg.registry_path)
        try:
            ctx = workspace.Ctx(cfg, reg)
            result = workspace.ensure(ctx, task_id, [repo], holder=f"claude:{session}" if session else None,
                                      base_ref=commit, title=branch or None)
            path = result["trees"][0]["path"]
            _log(cfg, "WorktreeCreate", repo=repo, task=task_id, path=path)
        finally:
            reg.close()
    else:
        path = _standard_create(top, branch, commit)
        _log(cfg, "WorktreeCreate:standard", repo=top, path=path)
    stdout.write(path + "\n")
    return 0


def _standard_create(top, branch, commit):
    """Behaviour for repositories that wsp does not manage."""
    name = util.slugify(branch) or "worktree"
    path = os.path.join(top, ".claude", "worktrees", name)
    if os.path.exists(path):
        raise WspError("PATH_CONFLICT", "worktree path exists", path=path)
    if branch and gitutil.local_branch_exists(top, branch):
        gitutil.git(top, "worktree", "add", path, branch)
    elif branch:
        gitutil.git(top, "worktree", "add", "-b", branch, path, commit or "HEAD")
    else:
        gitutil.git(top, "worktree", "add", "--detach", path, commit or "HEAD")
    return path


def worktree_remove(event):
    path = event.get("worktree_path")
    session = event.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    holder = f"claude:{session}" if session else None
    try:
        cfg = _load()
    except Exception as exc:
        sys.stderr.write(f"wsp: config error, keeping {path}: {exc}\n")
        return 0
    try:
        if not path or not os.path.isdir(path):
            return 0
        reg = Registry(cfg.registry_path) if cfg.exists else None
        tree = reg.find_tree_by_path(path) if reg else None
        if tree and os.path.realpath(tree["path"]) == os.path.realpath(path):
            ctx = workspace.Ctx(cfg, reg)
            for lease in reg.active_leases(tree["id"]):
                if lease["holder"] == holder:
                    reg.release_lease(lease["id"])
            repo_cfg = cfg.repo(tree["repo"])
            _, default_ref = workspace._default_ref(repo_cfg)
            profile = cfg.profile(tree["repo"], tree["path"])
            state = checkpoint.unique_state(tree["path"], profile, default_ref)
            running = procs.cwd_processes(tree["path"])
            if not state["unique"] and running == [] and not reg.active_leases(tree["id"]):
                workspace.evict(ctx, tree["task_id"], [tree["repo"]], holder=holder, reason="claude-exit",
                                accept_unclassified=False)
                _log(cfg, "WorktreeRemove:evicted", path=path)
            else:
                if state["unique"]:
                    workspace.checkpoint_task(ctx, tree["task_id"], [tree["repo"]], reason="claude-exit")
                _log(cfg, "WorktreeRemove:kept", path=path, unique=state["unique"], processes=running)
                sys.stderr.write(f"wsp: kept {path} (unsaved work or active use); checkpoint recorded\n")
            reg.close()
            return 0
        if reg:
            reg.close()
        # not ours: remove only a clean tree whose content is on the remote
        top = gitutil.toplevel(path)
        common = gitutil.common_dir(path)
        src = os.path.dirname(common) if os.path.basename(common) == ".git" else top
        entries = gitutil.status_entries(path)
        head = gitutil.rev_parse(path, "HEAD")
        default_ref = f"refs/remotes/origin/{gitutil.default_branch(src)}"
        if not entries and head and gitutil.covered_by_remote(src, head, default_ref)["covered"]:
            gitutil.git(src, "worktree", "remove", path, check=False)
            _log(cfg, "WorktreeRemove:standard-removed", path=path)
        else:
            _log(cfg, "WorktreeRemove:standard-kept", path=path)
            sys.stderr.write(f"wsp: kept {path}: it has changes that are not on a remote\n")
    except Exception as exc:
        _log(cfg, "WorktreeRemove:error", path=path, error=str(exc), trace=traceback.format_exc()[-2000:])
        sys.stderr.write(f"wsp: error while handling worktree removal, kept {path}: {exc}\n")
    return 0


def session_start(event, stdout):
    cfg = _load()
    if not cfg.exists:
        return 0
    cwd = event.get("cwd") or os.getcwd()
    session = event.get("session_id")
    reg = Registry(cfg.registry_path)
    try:
        tree = reg.find_tree_by_path(cwd)
        if not tree:
            return 0
        holder = f"claude:{session}"
        reg.attach_session(tree["task_id"], holder, app="claude", session_id=session, confirmed=True)
        lease, ok = reg.acquire_lease(tree["id"], holder)
        trees = reg.trees_for_task(tree["task_id"])
        lines = [f"wsp task `{tree['task_id']}`; use these absolute paths (never paths from old messages):"]
        for t in trees:
            lines.append(f"- {t['repo']}: {t['path']} [{t['state']}] branch {t['branch']}")
        if not ok:
            lines.append(f"Write lease is held by {lease['holder']}: act as an observer.")
        lines.append("Before resuming work run `wsp ensure --task <id> --repo <name> --json`.")
        stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                         "additionalContext": "\n".join(lines)[:9000]}}))
        return 0
    finally:
        reg.close()


def session_end(event):
    try:
        cfg = _load(sync_options=False)
        if not cfg.exists:
            return 0
        session = event.get("session_id")
        if not session:
            return 0
        reg = Registry(cfg.registry_path)
        try:
            for lease in reg.leases_for_holder(f"claude:{session}"):
                tree = reg.get_tree(lease["tree_id"])
                running = procs.cwd_processes(tree["path"]) if tree else []
                if running:
                    reg.release_lease(lease["id"], state="uncertain")
                else:
                    reg.release_lease(lease["id"])
        finally:
            reg.close()
    except Exception as exc:
        sys.stderr.write(f"wsp session-end: {exc}\n")
    return 0


def dispatch(app, name, stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if app != "claude":
        raise WspError("USAGE", f"unsupported hook app {app}")
    event = _read_event(stdin)
    if name in ("worktree-create", "session-start"):
        try:
            cfg = config_mod.Config.load()
            janitor.maybe_run_on_use(cfg)
        except Exception:
            pass
    if name == "worktree-create":
        return worktree_create(event, stdout)
    if name == "worktree-remove":
        return worktree_remove(event)
    if name == "session-start":
        return session_start(event, stdout)
    if name == "session-end":
        return session_end(event)
    raise WspError("USAGE", f"unknown hook {name}")
