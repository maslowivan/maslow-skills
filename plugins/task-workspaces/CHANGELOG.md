# Changelog — task-workspaces

Versions follow [semantic versioning](https://semver.org). Every change to the
plugin bumps `version` in both `.claude-plugin/plugin.json` and
`.codex-plugin/plugin.json` and adds a section here; CI enforces it and tags the
release as `task-workspaces-v<version>`.

## 0.6.1 — 2026-11-25

- **Fixed:** an ignored directory with nothing tracked inside was judged as one
  entry, so a folder that held only a secret `wsp` had put there itself
  (`web/.env.local` in a sparse tree) or only `.yarn/install-state.gz` blocked
  eviction with `UNCLASSIFIED_IGNORED` on every cleanup run. A directory that
  matches no pattern itself is now opened and its contents are classified one by
  one; the error lists the files that are really unknown. A directory matched by
  `preserve_ignored` or `reproducible_ignored` is still taken as a whole.
- **Fixed:** Yarn's `.yarn/install-state.gz`, `.yarn/cache/` and
  `.yarn/unplugged/` were known as reproducible only at the repository root; in
  a package folder (`web/.yarn/install-state.gz`) they blocked eviction too.
- **Fixed:** when `ensure` hit the disk reserve (`DISK_LOW`), the cleanup it ran
  kept the newest unused dependency instance of every package, so gigabytes of
  cache nobody used stayed while the agent asked the user to lower the reserve.
  Under `DISK_LOW` unused instances are now removed, oldest first, until the new
  trees fit; instances in use are never touched.
- **Changed:** the `DISK_LOW` message and the skill tell the agent not to ask for
  a lower `limits.min_free_gib`.

## 0.6.0 — 2026-11-25

Forgotten tasks no longer pile up until the limits are hit.

- **Fixed:** a task whose pull request was merged stayed forever when the default
  branch later changed the same lines: from then on merging the branch conflicts,
  so the content check could never prove the merge again. `wsp gc` now also asks
  GitHub through `gh` (`policy.pr_lookup`, on by default) and closes the task when
  a pull request of the branch was merged into the default branch and the tree's
  HEAD is that pull request's head or an ancestor of it. Commits made after the
  merged head, uncommitted changes, checkpoints of unpublished work, running
  processes and a live lease still keep the task. Without `gh`, or for other
  hosts, nothing changes. `repos.<name>.github: "owner/name"` names the repository
  when the origin URL does not (SSH host alias).
- **Added:** `policy.idle_evict_after_days` (default 7, `0` turns it off): `wsp gc`
  evicts the trees of a task nobody used for that long — unpublished work goes
  into a checkpoint first and `wsp ensure` restores the tree. Before, only tasks
  explicitly paused with `wsp release --pause` were evicted, so trees of sessions
  that simply ended counted against `max_tasks_with_trees` for good. "Used" means
  session and lease activity and the tree's own files (Git index, HEAD, reflog,
  uncommitted files), so an agent that works in a tree without calling `wsp` is
  not interrupted.
- **Added:** `wsp ensure` runs the cleanup policy once before it would fail with
  `LIMIT_REACHED` or `DISK_LOW`, and says so in `warnings`. If that frees nothing,
  the error lists `idle_tasks` that can be evicted, and its message no longer
  suggests raising the limit.
- **Added:** `wsp gc` reports tasks whose pull request was closed without merge
  (`review`, with the `wsp close --status cancelled` command); they are never
  removed automatically.
- **Changed:** the idle age in `wsp gc` no longer uses the tree's registry
  timestamp, which every size measurement refreshed.

## 0.5.4 — 2026-10-09

- **Fixed:** flaky `test_queries_by_repo_fetch_the_default_branch_first` on macOS
  CI (`unable to read sha1 file`, `fatal: unable to checkout working tree`). Since
  Git 2.54 the default `geometric` maintenance strategy repacks after a push as
  soon as two loose objects sit in `objects/17` (it estimates the count from that
  one shard), so a detached `git repack` of the bare test origin packed and pruned
  loose objects while a local `git clone` of it was still copying them. The test
  sandbox now disables automatic maintenance (`maintenance.auto=false`,
  `gc.auto=0`), and a regression test pushes two objects into `objects/17` and
  checks that no maintenance starts and they stay loose. `wsp` itself is not
  affected: it only clones from the configured remote origin.

## 0.5.3 — 2026-10-01

- **Changed:** `wsp sparse list|match|consumers --repo` fetch the default branch
  first (remote-tracking ref only; `--no-fetch` skips it) and report the `base`
  they read, so profiles merged after the canonical checkout was last updated are
  found.
- **Added:** `wsp ensure` without `--profile` in a repository that has
  `.wsp/SC-PROFILES.md` at the base warns that the tree is a full checkout and
  returns `sparse_available` with suggested profiles.
- **Docs:** the skill always runs `wsp sparse match --repo` before `ensure`
  instead of looking for the file in the canonical checkout.

## 0.5.2 — 2026-10-01

- **Fixed:** the disk budget of `wsp run --max-growth-gib` also counts the growth
  of the task tree itself, not only the drop of free space on the volume (which
  other processes move); the budget test was flaky on a busy CI runner.

## 0.5.1 — 2026-10-01

- **Fixed:** `wsp --version` reports the plugin version from its manifest (it
  still said 0.1.0).

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
