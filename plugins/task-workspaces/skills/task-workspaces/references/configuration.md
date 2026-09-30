# Configuration

## Files

| What | Where | Notes |
| --- | --- | --- |
| Settings | `~/.config/wsp/config.json` (`WSP_CONFIG` overrides) | shared by CLI, Codex and Claude Code |
| Registry, manifests, checkpoints, logs | `state_dir` (default `~/.local/share/wsp`) | survives plugin reinstall; never `${CLAUDE_PLUGIN_DATA}` |
| Trees | `worktrees_root` (default `~/worktrees/tasks`) | no `node_modules`/`package.json` above it; not a symlink |
| Dependency cache | `dependency_cache_dir` (default next to the trees) | same volume as trees for copy-on-write |
| Shared repo profile | `<repo>/.wsp/profile.json` | committed by the team |
| Local profile override | `~/.config/wsp/profiles/<repo>.json` | wins over the shared profile |

## config.json

```json
{
  "version": 1,
  "worktrees_root": "~/worktrees/tasks",
  "state_dir": "~/.local/share/wsp",
  "branch_prefix": "wsp/",
  "dependency_strategy": "auto",
  "integration": "none",
  "limits": {
    "min_free_gib": 50, "warn_free_gib": 75, "emergency_free_gib": 20,
    "manager_quota_gib": 100, "max_tasks_with_trees": 8, "max_trees": 16,
    "dependency_cache_gib": 40, "checkpoints_gib": 20, "default_tree_estimate_gib": 2,
    "run_max_growth_gib": null
  },
  "policy": {"paused_evict_after_hours": 24, "stale_review_after_days": 7, "keep_checkpoints_per_tree": 2,
             "lease_stale_hours": 12},
  "exclusions": {"time_machine": true, "spotlight": true},
  "janitor": {"mode": "on-use", "interval_minutes": 60},
  "repos": {
    "my-app": {
      "path": "~/src/my-app",
      "origin": "git@github.com:org/my-app.git",
      "default_branch": "main",
      "estimate_gib": 3,
      "profile": {}
    }
  }
}
```

Change values with `wsp config set limits.max_trees 20`, add repositories with
`wsp config add-repo --path ~/src/other`.

- `checkpoints_gib` — when exceeded, growth is blocked; checkpoints are never deleted to make room.
- `dependency_cache_gib` — unused instances are dropped first; if still full, new installs are not cached.
- `run_max_growth_gib` — default disk budget of one `wsp run` command (override with `--max-growth-gib`).
- `lease_stale_hours` — after this without a heartbeat, `wsp gc` marks a lease `uncertain` (still protective).
- `janitor.mode` — `on-use` (default): the cleanup policy runs in a detached background process after normal
  wsp commands and Claude Code hooks, at most once per `interval_minutes`, no service; `scheduled`: a
  launchd/systemd user job installed with `wsp janitor install`; `off`: only `wsp gc --apply` by hand.

## Repository profile

```json
{
  "secrets": [
    {"path": ".dev.vars", "source": "canonical"},
    {"path": "web/.env.local", "source": "command:op read op://Dev/web/env"},
    {"path": ".env.test", "source": "none"}
  ],
  "preserve_ignored": ["local.settings.json"],
  "reproducible_ignored": ["generated/"],
  "packages": {
    "services/api": {"install_command": "yarn install --immutable", "verify_command": "yarn tsc --noEmit"}
  },
  "verify_command": null
}
```

- `secrets` — never stored in checkpoints; re-created from `source`
  (`canonical` = copy from the main checkout, `command:<cmd>` = stdout of a
  command, `none` = recreate by hand). Values never appear in logs.
- `preserve_ignored` — ignored files copied into checkpoints.
- `reproducible_ignored` — ignored files that may be dropped (common build
  outputs and `node_modules` are built in).
- Ignored files matching none of these block eviction until classified.
- Commands (`install_command`, `verify_command`, `command:` secret sources) from a
  profile committed inside the repository (`.wsp/profile.json`) are ignored unless
  the local config sets `repos.<name>.trust_shared_commands: true`; a cloned
  repository cannot make `wsp` run commands on its own. Local config and local
  overrides are always trusted. Lockfile-based installs (`npm ci`, `yarn install`)
  run as usual.

## Importing repositories from a table

`wsp init --repo-map FILE` (or `integration: "repo-map"` with
`integration_options.repo_map_path`) imports repositories from any Markdown
table whose rows contain an absolute local checkout path; a cell that looks like
a Git URL becomes the origin. The file is only read.

## Claude Code plugin options

When installed as a plugin, Claude Code asks for `worktrees_root`, `state_dir`,
`dependency_strategy`, `disk_reserve_gib`, `cleanup_mode` and `integration` and exports them as
`CLAUDE_PLUGIN_OPTION_*`; the hooks copy changed values into `config.json`, so
Codex and the CLI see the same settings.

## Platforms

macOS (APFS): full support including clonefile. Linux: reflink on
btrfs/xfs, otherwise install per tree; janitor via a systemd user timer.
Windows: not supported in v1.
