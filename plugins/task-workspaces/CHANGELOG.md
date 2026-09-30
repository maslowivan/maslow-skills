# Changelog — task-workspaces

Versions follow [semantic versioning](https://semver.org). Every change to the
plugin bumps `version` in both `.claude-plugin/plugin.json` and
`.codex-plugin/plugin.json` and adds a section here; CI enforces it and tags the
release as `task-workspaces-v<version>`.

## 0.5.0 — 2026-09-30

- **Changed:** requires Python 3.9+ instead of 3.11+, so the macOS system
  `/usr/bin/python3` (Command Line Tools) runs `wsp` with nothing to install. The
  whole test suite runs on 3.9 and 3.13 in CI, including the macOS system Python.
- **Changed:** the CLI imports its modules on first use (faster start).

## 0.4.4 — 2026-09-30

- **Docs:** fix the README blockquote that swallowed the next paragraph on GitHub.

## 0.4.3 — 2026-09-30

- **Docs:** `/task-workspaces:setup-sparse-checkout` is the command in both Codex
  and Claude Code (plugin skills are namespaced, so it never clashes with other
  commands); `$setup-sparse-checkout` stays as a Codex alternative.

## 0.4.2 — 2026-09-30

- **Docs:** Codex lists plugin skills in its `/` menu — `/setup-sparse-checkout`
  (“Set Up Sparse Checkout”) as well as `$setup-sparse-checkout`.

## 0.4.1 — 2026-09-30

- **Docs:** recommend running `setup-sparse-checkout` once per repository — the
  profiles file is also a compact project index for agents; the skill offers a
  line to reference it from `AGENTS.md`/`CLAUDE.md` but does not edit them.

## 0.4.0 — 2026-09-30

- **Added:** the cleanup policy closes tasks whose work is already in the default
  branch (merge, fast-forward or squash — checked with a fresh fetch): trees,
  local branches and checkpoints are removed and the task becomes `completed`.
  Only when every tree has commits of its own, no uncommitted changes, no
  checkpoint of unpublished work, no running processes and no live lease (a
  lease without a heartbeat for `lease_stale_hours` counts as gone). Runs with
  the on-use janitor; `policy.close_merged: false` turns it off.

## 0.3.1 — 2026-09-30

- **Changed:** `wsp list` hides finished (completed/cancelled) tasks that no longer
  have a tree and says how many were hidden; `wsp list --all` shows them.

## 0.3.0 — 2026-09-30

Sparse profiles, reworked after the first real repositories.

- **Changed:** the profiles file lives in `.wsp/SC-PROFILES.md` (next to
  `.wsp/profile.json`). A root `SC-PROFILES.md` from 0.2 is still read and is
  moved on the next `wsp sparse scan --write`. `.wsp` is part of every sparse tree.
- **Added:** `- about:` — a few-word brief per subproject (from `package.json`
  `description`; the setup skill asks the agent to write it from AGENTS.md/README).
- **Added:** profiles are sorted by commit activity (last 28 days, `--activity-days`).
- **Added:** hidden `wsp:auto` marker records what the scanner generated, so a
  re-scan drops stale generated tags/folders but keeps everything a person added
  or re-spelled; `--rebuild` regenerates files written before markers existed.
- **Changed:** hidden folders (`.claude`, `.agents`, ...) are never subprojects;
  root agent folders (`.claude`, `.agents`, `.codex`, `.cursor`, ...) are
  included in every profile.
- **Changed:** domains come from served routes only: all `wrangler*.jsonc/json/toml`
  files, commented routes and `zone_name` ignored, `www.` merged, dev/staging/preview
  variants dropped when production hosts exist (a `.dev` TLD is not a variant),
  infrastructure hosts (cloudfront, amazonaws, workers.dev, ...) dropped, brand
  domains embedded in preview hosts extracted. Documentation hosts are used only
  for subprojects without routes, and only specific service hosts.
- **Changed:** tags are de-duplicated case- and punctuation-insensitively (the
  readable spelling wins) and never repeat the profile name.

## 0.2.1 — 2026-09-30

- **Fixed:** profile matching counts whole words (name and tags weigh more than paths).
- **Fixed:** file names such as `lib.rs` are no longer taken for hosts.
- **Fixed:** `wsp sparse list/match/consumers --repo` read the committed file on
  the default branch first.

## 0.2.0 — 2026-09-30

- **Added:** sparse-checkout profiles: `wsp sparse scan/list/match/consumers/add`,
  `wsp ensure --profile/--folder`, the `setup-sparse-checkout` skill.
- **Added:** registry schema v2 (sparse columns) with migration.

## 0.1.x — 2026-09-30

- Initial release: `wsp` CLI (ensure, run, deps, checkpoint, evict, restore,
  close, gc, inventory, doctor, init), Codex and Claude Code plugins, hooks,
  on-use cleanup (janitor), copy-on-write dependency cache, checkpoints.
