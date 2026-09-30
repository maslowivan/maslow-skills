"""First-run setup: detect the environment, ask only what is ambiguous, write config.

Two modes:
  * interactive terminal: `wsp init`
  * agent-driven: `wsp init --detect --json` prints questions with options and a
    recommended value; the agent asks the user and runs `wsp init --answers FILE`.
The wizard never deletes anything, never touches existing trees and never
changes Git settings. It only writes the wsp config (and, if asked,
Time Machine / Spotlight exclusions for the wsp roots).
"""

import copy
import difflib
import json
import os
import platform
import shutil
import subprocess

from . import config as config_mod, disk, gitutil, util
from .integrations import repo_map

SEARCH_DIRS = ("~/Projects", "~/projects", "~/src", "~/code", "~/dev", "~/work", "~/repos", "~/git")
WORKTREE_DIR_CANDIDATES = ("~/Projects/worktrees", "~/worktrees", "~/src/worktrees", "~/code/worktrees")
LOCKFILE_NAMES = ("yarn.lock", "pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json", "uv.lock",
                  "poetry.lock", "Pipfile.lock", "go.sum", "Cargo.lock", "Gemfile.lock")
ENV_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")


def _find_repos(search_dirs):
    found, seen = [], set()

    def add(path):
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)  # case-insensitive volumes list ~/Projects and ~/projects twice
        if key not in seen:
            seen.add(key)
            found.append(path)

    for base in search_dirs:
        base = util.expand(base)
        if not os.path.isdir(base):
            continue
        for depth1 in sorted(os.listdir(base)):
            p1 = os.path.join(base, depth1)
            if not os.path.isdir(p1) or depth1.startswith("."):
                continue
            if os.path.isdir(os.path.join(p1, ".git")):
                add(p1)
                continue
            try:
                children = sorted(os.listdir(p1))
            except OSError:
                continue
            for depth2 in children:
                p2 = os.path.join(p1, depth2)
                if not depth2.startswith(".") and os.path.isdir(os.path.join(p2, ".git")):
                    add(p2)
    return found


def describe_repo(path):
    listed = subprocess.run(["git", "-C", path, "ls-files", "-z"], capture_output=True, text=True).stdout.split("\0")
    lockfiles = sorted({f for f in listed if os.path.basename(f) in LOCKFILE_NAMES})
    env_examples = sorted(f for f in listed if any(
        os.path.basename(f).startswith(p) for p in (".env", ".dev.vars")) and f.endswith(ENV_EXAMPLE_SUFFIXES))
    managers = sorted({
        "yarn" if f.endswith("yarn.lock") else "pnpm" if f.endswith("pnpm-lock.yaml") else
        "npm" if f.endswith(("package-lock.json", "npm-shrinkwrap.json")) else
        "python" if f.endswith(("uv.lock", "poetry.lock", "Pipfile.lock")) else
        "go" if f.endswith("go.sum") else "rust" if f.endswith("Cargo.lock") else "ruby"
        for f in lockfiles
    })
    secrets = []
    for example in env_examples:
        target = example
        for suffix in ENV_EXAMPLE_SUFFIXES:
            if target.endswith(suffix):
                target = target[: -len(suffix)]
        secrets.append(target)
    return {
        "name": os.path.basename(path.rstrip("/")),
        "path": path,
        "origin": gitutil.origin_url(path),
        "default_branch": gitutil.default_branch(path),
        "package_managers": managers,
        "lockfiles": lockfiles[:30],
        "lockfile_count": len(lockfiles),
        "env_files": secrets,
    }


def detect(cfg, search_dirs=None, repo_map_path=None):
    home = os.path.expanduser("~")
    existing_wt = [util.expand(p) for p in WORKTREE_DIR_CANDIDATES if os.path.isdir(util.expand(p))]
    root_default = os.path.join(existing_wt[0], "tasks") if existing_wt else util.expand("~/worktrees/tasks")
    if cfg.exists:
        root_default = cfg.worktrees_root
    state_default = cfg.state_dir if cfg.exists else util.expand("~/.local/share/wsp")

    map_path = repo_map_path or (cfg.data.get("integration_options") or {}).get("repo_map_path")
    map_path = util.expand(map_path) if map_path else None
    if map_path and not os.path.isfile(map_path):
        map_path = None
    wa_repos = repo_map.parse_repo_map(map_path) if map_path else []
    repo_paths = _find_repos(search_dirs or SEARCH_DIRS)
    known = set()
    for p in repo_paths:
        st = os.stat(p)
        known.add((st.st_dev, st.st_ino))
    for r in wa_repos:
        if os.path.isdir(r["path"]):
            st = os.stat(r["path"])
            if (st.st_dev, st.st_ino) not in known:
                known.add((st.st_dev, st.st_ino))
                repo_paths.append(r["path"])
    repos = []
    for path in repo_paths:
        try:
            repos.append(describe_repo(path))
        except Exception:
            continue
    configured = set(cfg.repo_names()) if cfg.exists else set()
    wa_names = {r["name"] for r in wa_repos}

    fs_root = disk.fs_type(root_default)
    clone = disk.clone_mode(root_default)
    free_gib = disk.free_bytes(root_default) / util.GIB
    reserve = int(max(20, min(50, round(free_gib * 0.2))))
    ancestors = []
    probe = os.path.dirname(root_default)
    while probe and probe != os.path.dirname(probe) and probe.startswith(home):
        for name in ("node_modules", "package.json"):
            if os.path.exists(os.path.join(probe, name)):
                ancestors.append(os.path.join(probe, name))
        probe = os.path.dirname(probe)

    agents = {"claude": bool(shutil.which("claude")), "codex": bool(shutil.which("codex"))}
    environment = {
        "platform": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
        "git": ".".join(map(str, gitutil.version())), "filesystem": fs_root, "copy_on_write": clone,
        "free_gib": round(free_gib, 1), "agents": agents, "repo_map": map_path,
        "existing_worktree_dirs": existing_wt, "config_path": cfg.path, "config_exists": cfg.exists,
    }
    questions = [
        {"id": "worktrees_root", "type": "directory", "question": "Where should task worktrees live?",
         "options": list(dict.fromkeys([root_default] + [os.path.join(p, "tasks") for p in existing_wt]
                                       + [os.path.join(util.expand(d), "worktrees", "tasks") for d in SEARCH_DIRS[:1]
                                          if os.path.isdir(util.expand(d))]
                                       + [util.expand("~/worktrees/tasks")])),
         "recommended": root_default,
         "notes": ["Must not have node_modules/package.json in a parent directory."] +
                  ([f"Problem with default: {a}" for a in ancestors])},
        {"id": "state_dir", "type": "directory", "question": "Where should the registry and checkpoints live?",
         "options": [state_default], "recommended": state_default,
         "notes": ["Not a temporary or cloud-synced folder; survives plugin reinstall."]},
        {"id": "repos", "type": "multi", "question": "Which repositories should wsp manage?",
         "options": [{"name": r["name"], "path": r["path"], "origin": r["origin"],
                      "package_managers": r["package_managers"], "env_files": r["env_files"]} for r in repos],
         "recommended": sorted(configured | (wa_names & {r["name"] for r in repos})) or [],
         "notes": ["Repositories can also be added later with `wsp config add-repo`."]},
        {"id": "dependency_strategy", "type": "choice", "question": "How should dependencies be prepared?",
         "options": ["auto", "clone", "install", "none"],
         "recommended": "auto",
         "notes": [f"copy-on-write on this volume: {clone or 'not available'} (auto = clone when possible, otherwise install)"]},
        {"id": "secrets_source", "type": "choice", "question": "Where should local env/secret files (e.g. .dev.vars) come from?",
         "options": ["canonical", "none"], "recommended": "canonical",
         "notes": ["canonical = copy from the main checkout; 1Password or other sources can be set per file as "
                   "'command:op read ...' in the repository profile. Secrets are never stored in checkpoints."]},
        {"id": "disk_reserve_gib", "type": "number", "question": "Minimum free space to keep (GiB)?",
         "options": [reserve, 20, 50], "recommended": reserve},
        {"id": "claude_hooks", "type": "boolean",
         "question": "Use wsp for Claude Code worktrees (WorktreeCreate/WorktreeRemove hooks from the plugin)?",
         "options": [True, False], "recommended": agents["claude"],
         "notes": ["The hooks ship with the plugin; nothing is written to ~/.claude/settings.json by init."]},
        {"id": "exclusions", "type": "boolean",
         "question": "Exclude worktrees and dependency cache from Time Machine and Spotlight?",
         "options": [True, False], "recommended": environment["platform"] == "Darwin"},
        {"id": "janitor", "type": "boolean", "question": "Enable background cleanup (janitor)?",
         "options": [False, True], "recommended": False,
         "notes": ["Recommended only after the first successful evict -> restore round-trip."]},
        {"id": "integration", "type": "choice", "question": "Optional integration",
         "options": ["none"] + (["repo-map"] if map_path else []),
         "recommended": "repo-map" if map_path else "none",
         "notes": ["repo-map imports repositories from a Markdown table (wsp init --repo-map FILE)."]},
    ]
    return {"environment": environment, "questions": questions, "repos_detected": repos}


def build_config(cfg, answers, detected):
    raw = copy.deepcopy(cfg.raw) if cfg.exists else {}
    raw["version"] = config_mod.CONFIG_VERSION
    recommended = {q["id"]: q["recommended"] for q in detected["questions"]}
    get = lambda key: answers.get(key, recommended.get(key))
    raw["worktrees_root"] = get("worktrees_root")
    raw["state_dir"] = get("state_dir")
    raw["dependency_strategy"] = get("dependency_strategy")
    raw.setdefault("limits", {})["min_free_gib"] = float(get("disk_reserve_gib"))
    raw.setdefault("agents", {})["claude_hooks"] = bool(get("claude_hooks"))
    raw["agents"]["codex"] = detected["environment"]["agents"]["codex"]
    excl = bool(get("exclusions"))
    raw["exclusions"] = {"time_machine": excl and detected["environment"]["platform"] == "Darwin", "spotlight": excl}
    raw.setdefault("janitor", {})["enabled"] = bool(get("janitor"))
    raw["integration"] = get("integration")
    if raw["integration"] == "repo-map" and detected["environment"]["repo_map"]:
        raw.setdefault("integration_options", {})["repo_map_path"] = detected["environment"]["repo_map"]
    by_name = {r["name"]: r for r in detected["repos_detected"]}
    repos = raw.setdefault("repos", {})
    secrets_source = get("secrets_source")
    for item in get("repos") or []:
        if isinstance(item, dict):
            name, path, origin = item["name"], item["path"], item.get("origin")
            info = by_name.get(name) or describe_repo(util.expand(path))
        else:
            info = by_name.get(item)
            if not info:
                continue
            name, path, origin = info["name"], info["path"], info["origin"]
        entry = repos.setdefault(name, {})
        entry["path"] = path
        entry["origin"] = origin
        entry.setdefault("default_branch", info.get("default_branch"))
        profile = entry.setdefault("profile", {})
        if info.get("env_files") and "secrets" not in profile:
            profile["secrets"] = [{"path": p, "source": secrets_source} for p in info["env_files"]]
    return raw


def apply(cfg, answers, detected, write=True):
    before = json.dumps(cfg.raw if cfg.exists else {}, indent=2, sort_keys=True).splitlines()
    raw = build_config(cfg, answers, detected)
    after = json.dumps(raw, indent=2, sort_keys=True).splitlines()
    diff = "\n".join(difflib.unified_diff(before, after, "config (before)", "config (after)", lineterm=""))
    actions = []
    if write:
        new = config_mod.Config(raw, cfg.path, True)
        new.raw = raw
        new.save()
        os.makedirs(new.worktrees_root, exist_ok=True)
        os.makedirs(new.state_dir, exist_ok=True)
        os.makedirs(new.dependency_cache_dir, exist_ok=True)
        actions.extend(apply_exclusions(new))
        cfg = new
    return {"config_path": cfg.path, "diff": diff, "written": write, "actions": actions}


def apply_exclusions(cfg):
    actions = []
    targets = [cfg.worktrees_root, cfg.dependency_cache_dir]
    if cfg.data["exclusions"].get("spotlight"):
        for target in targets:
            if os.path.isdir(target):
                marker = os.path.join(target, ".metadata_never_index")
                if not os.path.exists(marker):
                    open(marker, "a").close()
                    actions.append(f"spotlight: {marker}")
    if cfg.data["exclusions"].get("time_machine") and shutil.which("tmutil"):
        for target in targets:
            if os.path.isdir(target):
                proc = subprocess.run(["tmutil", "addexclusion", target], capture_output=True, text=True)
                actions.append(f"time machine exclusion {target}: {'ok' if proc.returncode == 0 else proc.stderr.strip()}")
    return actions


def interactive(cfg, detected, input_fn=input, print_fn=print):
    answers = {}
    env = detected["environment"]
    print_fn(f"wsp setup — {env['platform']} {env['machine']}, filesystem {env['filesystem']}, "
             f"copy-on-write: {env['copy_on_write'] or 'no'}, free {env['free_gib']} GiB")
    for q in detected["questions"]:
        print_fn("")
        print_fn(q["question"])
        for note in q.get("notes", []):
            print_fn(f"  note: {note}")
        if q["type"] == "multi":
            for i, opt in enumerate(q["options"], 1):
                mark = "*" if opt["name"] in q["recommended"] else " "
                print_fn(f"  [{mark}] {i}. {opt['name']}  {opt['path']}  {','.join(opt['package_managers'])}")
            raw = input_fn("  numbers separated by spaces (Enter = marked): ").strip()
            if raw:
                picks = []
                for token in raw.replace(",", " ").split():
                    if token.isdigit() and 1 <= int(token) <= len(q["options"]):
                        picks.append(q["options"][int(token) - 1]["name"])
                answers[q["id"]] = picks
            else:
                answers[q["id"]] = q["recommended"]
            continue
        for i, opt in enumerate(q["options"], 1):
            mark = " (recommended)" if opt == q["recommended"] else ""
            print_fn(f"  {i}. {opt}{mark}")
        raw = input_fn("  choice number or custom value (Enter = recommended): ").strip()
        if not raw:
            answers[q["id"]] = q["recommended"]
        elif raw.isdigit() and 1 <= int(raw) <= len(q["options"]):
            answers[q["id"]] = q["options"][int(raw) - 1]
        elif q["type"] == "boolean":
            answers[q["id"]] = raw.lower() in ("y", "yes", "true", "1", "да")
        elif q["type"] == "number":
            answers[q["id"]] = float(raw)
        else:
            answers[q["id"]] = raw
    preview = apply(cfg, answers, detected, write=False)
    print_fn("")
    print_fn(preview["diff"] or "(no changes)")
    confirm = input_fn("Write this configuration? [y/N] ").strip().lower()
    if confirm in ("y", "yes", "да"):
        return apply(cfg, answers, detected, write=True)
    return {"written": False, "diff": preview["diff"]}
