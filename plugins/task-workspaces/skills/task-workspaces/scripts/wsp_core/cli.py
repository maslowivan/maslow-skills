"""wsp command line. Every command supports --json with stable error codes."""

import argparse
import json
import os
import sys

from . import __version__, config as config_mod, deps, doctor, hooks, init_wizard, inventory, janitor, sparse, util, workspace
from .errors import WspError
from .registry import Registry


def _ctx(args, require=True):
    cfg = config_mod.Config.load(args.config, require=require)
    reg = Registry(cfg.registry_path)
    return workspace.Ctx(cfg, reg)


def _print(args, data, human=None):
    if args.json:
        json.dump({"ok": True, **data} if isinstance(data, dict) else {"ok": True, "result": data},
                  sys.stdout, indent=2, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    elif human:
        print(human(data))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


# --- human renderers --------------------------------------------------------------
def _render_ensure(data):
    task = data["task"]
    lines = [f"Task {task['id']} ({task['status']}), holder {data['holder']}"
             + ("" if data["session_confirmed"] else " [session not confirmed]")]
    for t in data["trees"]:
        fresh = "" if t.get("base_fresh", True) else "  BASE NOT FRESH"
        lines.append(f"  {t['action']:9} {t['path']}  [{t['branch']} @ {str(t.get('head'))[:12]}]"
                     f"  lease={t.get('lease')}{fresh}")
        if t.get("sparse"):
            lines.append(f"            sparse {','.join(t['sparse']['profiles']) or '-'}: "
                         f"{', '.join(t['sparse']['folders'])} (+ root files)")
    for d in data.get("dependencies") or []:
        if "error" in d:
            lines.append(f"  deps {d['repo']}:{d['package_dir']}: ERROR {d['error']['message']}")
        else:
            lines.append(f"  deps {d['repo']}:{d.get('lock_dir')}: {d['mode']} (cwd {d.get('package_cwd')})")
    dk = data["disk"]
    lines.append(f"Disk: {dk['free']} free ({dk['level']}); manager {dk['usage_human']['total']} "
                 f"(trees {dk['usage_human']['trees']}, cache {dk['usage_human']['dependency_cache']}, "
                 f"checkpoints {dk['usage_human']['checkpoints']}); {dk['tasks_with_trees']} tasks, "
                 f"{dk['trees_on_disk']} trees on disk")
    for w in data.get("warnings") or []:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)


def _render_list(data):
    lines = []
    for task in data["tasks"]:
        pin = " [pinned]" if task["pinned"] else ""
        lines.append(f"{task['id']}  {task['status']}{pin}  {task['title'] or ''}")
        for t in task["trees"]:
            lease = ",".join(t["leases"]) or "-"
            lines.append(f"    {t['repo']:24} {t['state']:10} {t['size']:>10}  lease {lease}  {t['path']}")
    dk = data["disk"]
    lines.append(f"Disk: {dk['free']} free ({dk['level']}); manager {dk['usage_human']['total']}; "
                 f"{dk['tasks_with_trees']} tasks / {dk['trees_on_disk']} trees on disk; measured {dk['measured_at']}")
    return "\n".join(lines) if lines else "no tasks"


def _render_inventory(data):
    lines = [f"Inventory ({data['coverage']}), {data['measured_at']}: {data['counts_by_owner']}"]
    for repo in data["repos"]:
        lines.append(f"{repo['repo']}  ({repo['source']})")
        for t in repo["trees"]:
            if t["main"]:
                continue
            ref = t["branch"] or ("detached " + (t["head"] or "")[:9])
            size = f" {t['size']}" if "size" in t else ""
            lines.append(f"    {t.get('owner', '?'):9} {t.get('assessment', ''):36} {ref:40}{size}  {t['path']}")
    return "\n".join(lines)


def _render_doctor(data):
    lines = []
    for c in data["checks"]:
        mark = "ok  " if c["ok"] else ("WARN" if c["level"] == "warning" else "FAIL")
        detail = "" if c["detail"] in (None, "", []) else f"  {c['detail']}"
        lines.append(f"[{mark}] {c['check']}{detail}")
    if data.get("hint"):
        lines.append(data["hint"])
    return "\n".join(lines)


# --- commands ------------------------------------------------------------------------------
def cmd_doctor(args):
    cfg = config_mod.Config.load(args.config)
    reg = Registry(cfg.registry_path) if cfg.exists else None
    result = doctor.run(cfg, reg)
    _print(args, result, _render_doctor)
    return 0 if result["ok"] else 1


def cmd_init(args):
    cfg = config_mod.Config.load(args.config)
    detected = init_wizard.detect(cfg, args.search_dir or None, repo_map_path=args.repo_map)
    if args.detect:
        _print(args, detected)
        return 0
    if args.answers:
        with (sys.stdin if args.answers == "-" else open(args.answers)) as fh:
            answers = json.load(fh)
        result = init_wizard.apply(cfg, answers, detected, write=not args.dry_run)
        _print(args, result, lambda d: (d["diff"] or "(no changes)") + ("\nwritten" if d["written"] else "\n(dry run)"))
        return 0
    if args.json or not sys.stdin.isatty():
        raise WspError("USAGE", "non-interactive: use `wsp init --detect --json`, then `wsp init --answers FILE`")
    result = init_wizard.interactive(cfg, detected)
    return 0 if result.get("written") else 1


def cmd_config(args):
    cfg = config_mod.Config.load(args.config, require=args.action != "show")
    if args.action == "show":
        _print(args, cfg.to_public())
        return 0
    if args.action == "set":
        value = args.value
        try:
            value = json.loads(value)
        except ValueError:
            pass
        cfg.set_path(args.key.split("."), value)
        cfg.save()
        _print(args, {"set": args.key, "value": value})
        return 0
    if args.action == "add-repo":
        path = util.expand(args.path)
        info = init_wizard.describe_repo(path)
        name = args.name or info["name"]
        util.validate_repo_name(name)
        cfg.set_path(["repos", name], {"path": path, "origin": info["origin"],
                                       "default_branch": info["default_branch"], "profile": {}})
        cfg.save()
        _print(args, {"added": name, "repo": info})
        return 0
    raise WspError("USAGE", f"unknown config action {args.action}")


def cmd_inventory(args):
    cfg = config_mod.Config.load(args.config)
    result = inventory.scan(cfg, args.repo_path, sizes=args.sizes, check_processes=not args.no_processes)
    _print(args, result, _render_inventory)
    return 0


def cmd_ensure(args):
    ctx = _ctx(args)
    result = workspace.ensure(ctx, args.task, args.repo, title=args.title, tracker_ref=args.tracker_ref,
                              holder=args.holder, base_ref=args.base, branch=args.branch,
                              from_remote_branch=args.from_remote_branch, allow_stale_base=args.allow_stale_base,
                              packages=args.deps, reclone_source=args.reclone_source,
                              profiles=args.profile, folders=args.folder)
    _print(args, result, _render_ensure)
    return 0


def cmd_attach(args):
    ctx = _ctx(args)
    _print(args, workspace.attach(ctx, args.task, holder=args.holder, app=args.app, session=args.session,
                                  role=args.role))
    return 0


def cmd_list(args):
    ctx = _ctx(args)
    _print(args, workspace.list_all(ctx), _render_list)
    return 0


def cmd_status(args):
    ctx = _ctx(args)
    _print(args, workspace.status(ctx, args.task))
    return 0


def cmd_run(args):
    ctx = _ctx(args)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise WspError("USAGE", "missing command after --")
    return workspace.run(ctx, args.task, args.repo, command, package_dir=args.package, holder=args.holder,
                         skip_deps_check=args.skip_deps_check, max_growth_gib=args.max_growth_gib)


def cmd_checkpoint(args):
    ctx = _ctx(args)
    _print(args, workspace.checkpoint_task(ctx, args.task, args.repo or None))
    return 0


def cmd_deps(args):
    ctx = _ctx(args)
    tree = ctx.reg.find_tree(args.task, args.repo)
    if not tree:
        raise WspError("TREE_UNKNOWN", "no such tree", task=args.task, repo=args.repo)
    tree = workspace.reconcile(ctx, tree)
    if args.check:
        _print(args, {"dependencies": deps.check(ctx.cfg, ctx.reg, tree)})
        return 0
    if tree["state"] != "ready":
        raise WspError("TREE_UNKNOWN", f"tree is {tree['state']}; run `wsp ensure` first")
    result = deps.prepare(ctx.cfg, ctx.reg, tree, args.package or ".", strategy=args.strategy,
                          force_install=args.force_install)
    _print(args, result)
    return 0


def cmd_release(args):
    ctx = _ctx(args)
    _print(args, workspace.release(ctx, args.task, holder=args.holder, repos=args.repo or None, pause=args.pause,
                                   force=args.force))
    return 0


def cmd_evict(args):
    ctx = _ctx(args)
    _print(args, workspace.evict(ctx, args.task, repos=args.repo or None, holder=args.holder,
                                 accept_unclassified=args.accept_unclassified))
    return 0


def cmd_restore(args):
    ctx = _ctx(args)
    _print(args, workspace.restore(ctx, args.task, repos=args.repo or None, holder=args.holder,
                                   reclone_source=args.reclone_source))
    return 0


def cmd_close(args):
    ctx = _ctx(args)
    _print(args, workspace.close(ctx, args.task, status=args.status, discard=args.discard, holder=args.holder,
                                 keep_branch=args.keep_branch))
    return 0


def cmd_task(args):
    ctx = _ctx(args)
    _print(args, workspace.set_task_status(ctx, args.task, args.status))
    return 0


def cmd_pin(args, pinned):
    ctx = _ctx(args)
    _print(args, workspace.set_pin(ctx, args.task, pinned))
    return 0


def cmd_gc(args):
    ctx = _ctx(args)
    result = workspace.gc(ctx, apply=args.apply)
    _print(args, result)
    return 0


def cmd_registry(args):
    ctx = _ctx(args)
    if args.action == "rebuild":
        _print(args, workspace.rebuild_registry(ctx))
    return 0


def _render_sparse(data):
    if "profiles" not in data:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    lines = []
    if data.get("file"):
        lines.append(f"{data['file']}{' (written)' if data.get('written') else ''}")
    report = data.get("report")
    if report:
        lines.append(f"added {len(report['added'])}, updated {len(report['updated'])}, "
                     f"kept manual {len(report['kept_manual'])}")
    for name, prof in data["profiles"].items():
        lines.append(f"  {name:28} {', '.join(prof['folders'])}")
        if prof["tags"]:
            lines.append(f"  {'':28} tags: {', '.join(prof['tags'])}")
    return "\n".join(lines)


def cmd_sparse(args):
    if args.action == "scan":
        target = util.expand(args.path or (args.query[0] if args.query else None) or os.getcwd())
        result = sparse.scan_and_write(target, write=args.write)
        if not args.details:
            result.pop("details", None)
        if args.json is False:
            result.pop("preview", None)
        _print(args, result, _render_sparse)
        return 0
    if args.action in ("list", "match", "consumers"):
        if args.repo:
            cfg = config_mod.Config.load(args.config, require=True)
            repo_path = cfg.repo(args.repo)["path"]
        else:
            repo_path = util.expand(args.path or os.getcwd())
        from . import gitutil
        root = gitutil.toplevel(repo_path)
        profiles = None
        if args.repo:  # the committed version on the default branch is what new tasks use
            default = cfg.repo(args.repo).get("default_branch") or gitutil.default_branch(root)
            profiles, _ = sparse.load_at(root, f"refs/remotes/origin/{default}")
        profiles = profiles if profiles is not None else sparse.load(root)
        if profiles is None:
            raise WspError("SPARSE_PROFILE_UNKNOWN", f"{sparse.FILE_NAME} not found in {root}; "
                           "run `wsp sparse scan --write` there")
        if args.action == "list":
            _print(args, {"file": sparse.profiles_path(root), "profiles": profiles}, _render_sparse)
        elif args.action == "match":
            names = sparse.match(profiles, " ".join(args.query or []))
            _print(args, {"query": " ".join(args.query or []), "matches": [{"name": n, **profiles[n]} for n in names]})
        else:
            _print(args, {"folder": args.folder_arg, "profiles": sparse.consumers(profiles, args.folder_arg)})
        return 0
    if args.action == "add":
        ctx = _ctx(args)
        _print(args, workspace.sparse_widen(ctx, args.task, args.repo, profiles=args.profile, folders=args.folder))
        return 0
    raise WspError("USAGE", f"unknown sparse action {args.action}")


def cmd_hook(args):
    return hooks.dispatch(args.app, args.name)


def cmd_janitor(args):
    cfg = config_mod.Config.load(args.config, require=True)
    if args.action == "status":
        _print(args, janitor.status(cfg))
    elif args.action == "plist":
        print(janitor.plist(cfg))
    elif args.action == "install":
        _print(args, janitor.install(cfg))
    elif args.action == "uninstall":
        _print(args, janitor.uninstall(cfg))
    elif args.action == "run":
        reg = Registry(cfg.registry_path)
        ctx = workspace.Ctx(cfg, reg)
        result = janitor.run(cfg, reg, lambda apply: workspace.gc(ctx, apply=apply))
        _print(args, result)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="wsp", description="Task workspaces: isolated multi-repo worktrees per task")
    p.add_argument("--version", action="version", version=f"wsp {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--config", help="config file (default: $WSP_CONFIG or ~/.config/wsp/config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        sp = sub.add_parser(name, parents=[common], help=help_text)
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", cmd_doctor, "check environment, roots, repositories and registry")
    sp = add("init", cmd_init, "first-run setup wizard")
    sp.add_argument("--detect", action="store_true", help="print detected environment and questions")
    sp.add_argument("--answers", help="JSON answers file ('-' for stdin)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--search-dir", action="append", help="directories to scan for repositories")
    sp.add_argument("--repo-map", help="Markdown table listing local repository paths (and origins) to import")
    sp = add("config", cmd_config, "show or change configuration")
    sp.add_argument("action", choices=["show", "set", "add-repo"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.add_argument("--path")
    sp.add_argument("--name")
    sp = add("inventory", cmd_inventory, "read-only list of all worktrees of configured repos")
    sp.add_argument("--repo-path", action="append", help="extra repository to include")
    sp.add_argument("--sizes", action="store_true", help="measure sizes (slow)")
    sp.add_argument("--no-processes", action="store_true")

    def task_arg(sp, repo=False, repo_required=False, holder=True):
        sp.add_argument("--task", required=True)
        if repo:
            sp.add_argument("--repo", action="append" if not repo_required else None, required=repo_required)
        if holder:
            sp.add_argument("--holder", help="owner identity app:session (default: detected)")

    sp = add("ensure", cmd_ensure, "reuse, create or restore the task's trees; prints absolute paths")
    task_arg(sp)
    sp.add_argument("--repo", action="append", required=True)
    sp.add_argument("--title")
    sp.add_argument("--tracker-ref")
    sp.add_argument("--base", help="base revision (default: fresh origin/<default branch>)")
    sp.add_argument("--branch", help="branch name (default: <branch_prefix><task>)")
    sp.add_argument("--from-remote-branch", action="store_true", help="continue an existing origin branch")
    sp.add_argument("--allow-stale-base", action="store_true", help="continue if fetch fails (marked NOT fresh)")
    sp.add_argument("--deps", action="append", metavar="REPO:PACKAGE_DIR", help="also prepare dependencies")
    sp.add_argument("--reclone-source", action="store_true",
                    help="if the source checkout is gone, clone it from origin (only into a missing/empty path)")
    sp.add_argument("--profile", action="append", metavar="[REPO:]NAME",
                    help="sparse checkout: only the folders of this SC-PROFILES.md profile (repeatable)")
    sp.add_argument("--folder", action="append", metavar="[REPO:]FOLDER",
                    help="sparse checkout: add a folder to the profile's folders (repeatable)")
    sp = add("attach", cmd_attach, "record a verified task <-> chat/session link")
    task_arg(sp)
    sp.add_argument("--app")
    sp.add_argument("--session")
    sp.add_argument("--role", default="owner", choices=["owner", "observer"])
    add("list", cmd_list, "tasks, trees, owners, sizes, disk")
    sp = add("status", cmd_status, "paths, leases, git state, checkpoints of a task")
    sp.add_argument("--task", required=True)
    sp = add("run", cmd_run, "run a command in a task tree under a lease")
    task_arg(sp)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--package", help="package dir inside the repo (cwd)")
    sp.add_argument("--skip-deps-check", action="store_true")
    sp.add_argument("--max-growth-gib", type=float, help="stop the command if it consumes more disk than this")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp = add("checkpoint", cmd_checkpoint, "save unique local state outside the tree")
    task_arg(sp, repo=True, holder=False)
    sp = add("deps", cmd_deps, "prepare or check dependencies of one package")
    task_arg(sp, holder=False)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--package", default=".")
    sp.add_argument("--strategy", choices=["auto", "clone", "install", "none"])
    sp.add_argument("--force-install", action="store_true")
    sp.add_argument("--check", action="store_true")
    sp = add("release", cmd_release, "release your lease (optionally pause the task)")
    task_arg(sp, repo=True)
    sp.add_argument("--pause", action="store_true", help="checkpoint and mark the task paused")
    sp.add_argument("--force", action="store_true", help="release even if processes still run")
    sp = add("evict", cmd_evict, "checkpoint if needed, then remove the trees (restorable)")
    task_arg(sp, repo=True)
    sp.add_argument("--accept-unclassified", action="store_true",
                    help="drop ignored files that no profile rule classifies")
    sp = add("restore", cmd_restore, "recreate evicted or missing trees")
    task_arg(sp, repo=True)
    sp.add_argument("--reclone-source", action="store_true",
                    help="if the source checkout is gone, clone it from origin (only into a missing/empty path)")
    sp = add("close", cmd_close, "finish a task: remove trees, branch and checkpoints")
    task_arg(sp)
    sp.add_argument("--status", default="completed", choices=["completed", "cancelled"])
    sp.add_argument("--discard", action="store_true", help="discard unpushed local work (needs user confirmation)")
    sp.add_argument("--keep-branch", action="store_true")
    sp = add("task", cmd_task, "set task status (open, in_progress, paused)")
    sp.add_argument("--task", required=True)
    sp.add_argument("--status", required=True, choices=["open", "in_progress", "paused"])
    sp = add("pin", lambda a: cmd_pin(a, True), "protect a task from automatic cleanup")
    sp.add_argument("--task", required=True)
    sp = add("unpin", lambda a: cmd_pin(a, False), "allow automatic cleanup again")
    sp.add_argument("--task", required=True)
    sp = add("gc", cmd_gc, "show (default) or apply the cleanup policy")
    group = sp.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--apply", action="store_true")
    sp = add("registry", cmd_registry, "registry maintenance")
    sp.add_argument("action", choices=["rebuild"])
    sp = add("sparse", cmd_sparse, "sparse-checkout profiles (SC-PROFILES.md): scan, list, match, consumers, add")
    sp.add_argument("action", choices=["scan", "list", "match", "consumers", "add"])
    sp.add_argument("query", nargs="*", help="scan: repository folder (default: current); match: words to look for")
    sp.add_argument("--write", action="store_true", help="scan: write SC-PROFILES.md (default: preview only)")
    sp.add_argument("--details", action="store_true", help="scan: include why each folder was added")
    sp.add_argument("--repo", help="configured repository (list/match/consumers/add)")
    sp.add_argument("--path", help="repository folder instead of --repo")
    sp.add_argument("--task", help="add: task id")
    sp.add_argument("--profile", action="append", help="add: profile to add to the tree")
    sp.add_argument("--folder", action="append", help="add: folder to add to the tree")
    sp.add_argument("--of", dest="folder_arg", help="consumers: folder whose consumers to list")
    sp = add("hook", cmd_hook, "agent hook entry point (reads event JSON on stdin)")
    sp.add_argument("app", choices=["claude"])
    sp.add_argument("name", choices=["worktree-create", "worktree-remove", "session-start", "session-end"])
    sp = add("janitor", cmd_janitor, "cleanup policy runner: status, run, and the optional scheduled job "
             "(plist, install, uninstall)")
    sp.add_argument("action", choices=["status", "plist", "install", "uninstall", "run"])
    return p


# commands after which the on-use janitor may start a background cleanup run
_JANITOR_TRIGGERS = {"ensure", "list", "status", "checkpoint", "release", "evict", "restore", "close", "run", "deps"}


def _after_command(args):
    if args.cmd not in _JANITOR_TRIGGERS:
        return
    try:
        cfg = config_mod.Config.load(args.config)
        janitor.maybe_run_on_use(cfg)
    except Exception:
        pass  # cleanup is best effort; never fail the user's command because of it


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = args.fn(args) or 0
        _after_command(args)
        return code
    except WspError as exc:
        if getattr(args, "json", False):
            json.dump({"ok": False, "error": exc.to_dict()}, sys.stdout, indent=2, ensure_ascii=False, default=str)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(f"wsp: {exc.code}: {exc.message}\n")
            if exc.details:
                sys.stderr.write(json.dumps(exc.details, indent=2, ensure_ascii=False, default=str) + "\n")
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # never leave an agent without a machine-readable answer
        import traceback
        err = WspError("INTERNAL", f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-3000:])
        if getattr(args, "json", False):
            json.dump({"ok": False, "error": err.to_dict()}, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            sys.stderr.write(err.details["trace"])
        return err.exit_code


if __name__ == "__main__":
    sys.exit(main())
