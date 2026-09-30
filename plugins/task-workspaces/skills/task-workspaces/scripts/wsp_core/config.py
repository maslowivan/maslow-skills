"""User configuration: ~/.config/wsp/config.json (override with WSP_CONFIG).

Nothing in the core refers to a particular company, workspace or path; every
location and limit comes from here.
"""

import copy
import os

from . import util
from .errors import WspError

CONFIG_VERSION = 1

DEFAULTS = {
    "version": CONFIG_VERSION,
    "worktrees_root": "~/worktrees/tasks",
    "state_dir": "~/.local/share/wsp",
    "dependency_cache_dir": None,  # default: <dirname(worktrees_root)>/.dependency-cache
    "branch_prefix": "wsp/",
    "dependency_strategy": "auto",  # auto | clone | install | none
    "integration": "none",  # none | repo-map
    "integration_options": {},
    "limits": {
        "min_free_gib": 50,
        "warn_free_gib": 75,
        "emergency_free_gib": 20,
        "manager_quota_gib": 100,
        "max_tasks_with_trees": 8,
        "max_trees": 16,
        "dependency_cache_gib": 40,
        "checkpoints_gib": 20,
        "default_tree_estimate_gib": 2,
        "run_max_growth_gib": None,  # per-command disk budget for `wsp run`; None = only the emergency floor
    },
    "fetch": {"retries": 4, "backoff_seconds": 1.5, "timeout_seconds": 300},
    "policy": {
        "paused_evict_after_hours": 24,
        "stale_review_after_days": 7,
        "keep_checkpoints_per_tree": 2,
        "lease_stale_hours": 12,
        "close_merged": True,  # gc closes tasks whose work is already in the default branch
    },
    "agents": {"claude_hooks": False, "codex": False},
    "exclusions": {"time_machine": False, "spotlight": False},
    # Cleanup policy runner (`wsp gc --apply`):
    #   on-use    - runs in the background while wsp is used, at most once per interval (default; no service)
    #   scheduled - a launchd/systemd user job (`wsp janitor install`) runs it even when wsp is idle
    #   off       - only when you run `wsp gc --apply` yourself
    "janitor": {"mode": "on-use", "interval_minutes": 60},
    "repos": {},
}

REPO_DEFAULTS = {
    "path": None,
    "origin": None,
    "default_branch": None,
    "estimate_gib": None,
    # Allow <repo>/.wsp/profile.json to define commands (install/verify, command: secrets).
    "trust_shared_commands": False,
    "profile": {},
}

PROFILE_DEFAULTS = {
    # Ignored files copied into checkpoints (never secrets).
    "preserve_ignored": [],
    # Ignored files that are rebuilt from sources and may be dropped.
    "reproducible_ignored": [],
    # Local secret/env files: never stored in checkpoints, re-materialised from a source.
    # {"path": ".dev.vars", "source": "canonical" | "none" | "command:<shell cmd>"}
    "secrets": [],
    # Per package dir overrides: {"cloudflare/hub": {"install_command": "yarn install --immutable"}}
    "packages": {},
    "verify_command": None,
}

# Plugin userConfig keys (Claude Code exports CLAUDE_PLUGIN_OPTION_<KEY>).
PLUGIN_OPTION_MAP = {
    "WORKTREES_ROOT": ("worktrees_root",),
    "STATE_DIR": ("state_dir",),
    "DEPENDENCY_STRATEGY": ("dependency_strategy",),
    "DISK_RESERVE_GIB": ("limits", "min_free_gib"),
    "INTEGRATION": ("integration",),
    "CLEANUP_MODE": ("janitor", "mode"),
}

VALID_STRATEGIES = ("auto", "clone", "install", "none")
VALID_INTEGRATIONS = ("none", "repo-map")
VALID_JANITOR_MODES = ("on-use", "scheduled", "off")


def strip_commands(shared):
    """A profile committed inside a repository must not run commands on this machine
    unless the local config trusts it (repos.<name>.trust_shared_commands).
    Drops command secret sources and install/verify commands; keeps classifications."""
    shared = copy.deepcopy(shared)
    shared.pop("verify_command", None)
    for pkg in (shared.get("packages") or {}).values():
        if isinstance(pkg, dict):
            pkg.pop("install_command", None)
            pkg.pop("verify_command", None)
    secrets = []
    for item in shared.get("secrets") or []:
        if isinstance(item, dict) and str(item.get("source", "")).startswith("command:"):
            item = {**item, "source": "none"}
        secrets.append(item)
    if "secrets" in shared:
        shared["secrets"] = secrets
    return shared


def config_path():
    return util.expand(os.environ.get("WSP_CONFIG") or "~/.config/wsp/config.json")


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    def __init__(self, data, path, exists):
        self.raw = data
        self.path = path
        self.exists = exists
        self.data = _merge(DEFAULTS, data)
        self._validate()

    # --- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path=None, require=False):
        path = util.expand(path) if path else config_path()
        data = util.read_json(path)
        exists = data is not None
        if not exists:
            if require:
                raise WspError(
                    "CONFIG_MISSING",
                    "wsp is not configured yet; run `wsp init` (or `wsp init --detect --json` from an agent)",
                    config=path,
                )
            data = {}
        return cls(data, path, exists)

    def _validate(self):
        d = self.data
        if d.get("version") != CONFIG_VERSION:
            raise WspError("CONFIG_INVALID", "unsupported config version", version=d.get("version"))
        if d["dependency_strategy"] not in VALID_STRATEGIES:
            raise WspError("CONFIG_INVALID", "invalid dependency_strategy", value=d["dependency_strategy"])
        if d["integration"] not in VALID_INTEGRATIONS:
            raise WspError("CONFIG_INVALID", "invalid integration", value=d["integration"])
        janitor = d["janitor"]
        if "mode" not in (self.raw.get("janitor") or {}) and "enabled" in (self.raw.get("janitor") or {}):
            janitor["mode"] = "scheduled" if janitor.get("enabled") else "off"  # config written by v0.1
        if janitor.get("mode") not in VALID_JANITOR_MODES:
            raise WspError("CONFIG_INVALID", "invalid janitor.mode", value=janitor.get("mode"))
        for name in d["repos"]:
            util.validate_repo_name(name)

    def save(self):
        self.raw["version"] = CONFIG_VERSION
        util.write_json_atomic(self.path, self.raw)
        self.exists = True
        self.data = _merge(DEFAULTS, self.raw)
        self._validate()

    def set_path(self, keys, value):
        node = self.raw
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        self.data = _merge(DEFAULTS, self.raw)
        self._validate()

    def apply_plugin_options(self, environ=None):
        """Copy Claude Code plugin options into the shared config file.

        Returns the list of changed keys; the caller decides whether to save.
        """
        environ = environ if environ is not None else os.environ
        changed = []
        for suffix, keys in PLUGIN_OPTION_MAP.items():
            value = environ.get(f"CLAUDE_PLUGIN_OPTION_{suffix}")
            if value in (None, ""):
                continue
            if keys == ("limits", "min_free_gib"):
                try:
                    value = float(value)
                except ValueError:
                    continue
            if keys == ("dependency_strategy",) and value not in VALID_STRATEGIES:
                continue
            if keys == ("integration",) and value not in VALID_INTEGRATIONS:
                continue
            if keys == ("janitor", "mode") and value not in VALID_JANITOR_MODES:
                continue
            current = self.data
            for key in keys:
                current = current.get(key) if isinstance(current, dict) else None
            if current != value:
                self.set_path(list(keys), value)
                changed.append(".".join(keys))
        return changed

    # --- resolved values ---------------------------------------------------
    @property
    def worktrees_root(self):
        return os.path.realpath(util.expand(self.data["worktrees_root"]))

    @property
    def state_dir(self):
        return os.path.realpath(util.expand(self.data["state_dir"]))

    @property
    def dependency_cache_dir(self):
        explicit = self.data.get("dependency_cache_dir")
        if explicit:
            return os.path.realpath(util.expand(explicit))
        return os.path.join(os.path.dirname(self.worktrees_root), ".dependency-cache")

    @property
    def checkpoints_dir(self):
        return os.path.join(self.state_dir, "checkpoints")

    @property
    def manifests_dir(self):
        return os.path.join(self.state_dir, "manifests")

    @property
    def registry_path(self):
        return os.path.join(self.state_dir, "registry.sqlite")

    @property
    def limits(self):
        return self.data["limits"]

    @property
    def policy(self):
        return self.data["policy"]

    @property
    def branch_prefix(self):
        return self.data["branch_prefix"] or ""

    @property
    def dependency_strategy(self):
        return self.data["dependency_strategy"]

    def repo_names(self):
        return sorted(self.data["repos"])

    def repo(self, name):
        repos = self.data["repos"]
        if name not in repos:
            raise WspError("REPO_UNKNOWN", f"repository '{name}' is not configured", repo=name, known=sorted(repos))
        repo = _merge(REPO_DEFAULTS, repos[name])
        repo["profile"] = _merge(PROFILE_DEFAULTS, repo.get("profile") or {})
        repo["name"] = name
        repo["path"] = os.path.realpath(util.expand(repo["path"])) if repo["path"] else None
        return repo

    def profile(self, name, tree_path=None):
        """Effective repository profile.

        Precedence (last wins): built-in defaults, config.json repos.<name>.profile,
        <repo>/.wsp/profile.json (shared by the team), ~/.config/wsp/profiles/<name>.json
        (local override).
        """
        repo = self.repo(name)
        profile = repo["profile"]
        repo_file = os.path.join(tree_path or repo["path"] or "", ".wsp", "profile.json")
        shared = util.read_json(repo_file) if (tree_path or repo["path"]) else None
        if shared:
            if not repo.get("trust_shared_commands"):
                shared = strip_commands(shared)
            profile = _merge(profile, shared)
        local_file = os.path.join(os.path.dirname(self.path), "profiles", f"{name}.json")
        local = util.read_json(local_file)
        if local:
            profile = _merge(profile, local)
        return profile

    def find_repo_by_path(self, path):
        path = os.path.realpath(path)
        for name in self.repo_names():
            repo_path = self.repo(name)["path"]
            if repo_path and os.path.realpath(repo_path) == path:
                return name
        return None

    def to_public(self):
        return {
            "config_path": self.path,
            "configured": self.exists,
            "worktrees_root": self.worktrees_root,
            "state_dir": self.state_dir,
            "dependency_cache_dir": self.dependency_cache_dir,
            "dependency_strategy": self.dependency_strategy,
            "integration": self.data["integration"],
            "limits": self.limits,
            "repos": {n: {"path": self.repo(n)["path"], "origin": self.repo(n)["origin"]} for n in self.repo_names()},
        }
