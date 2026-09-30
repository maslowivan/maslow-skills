# task-workspaces (`wsp`)

A powerful alternative to the built-in worktrees of **Codex** and **Claude Code**:
isolated Git worktrees per task, across one or more repositories, that are fast
to prepare, easy on your SSD, can contain just the subproject you work on, and
stay fully under your control.

## Why use it instead of built-in worktrees

1. **Faster to a working tree.** A fresh built-in worktree has no dependencies:
   Codex runs your setup script and Claude Code can at most symlink one shared,
   mutable `node_modules` into it. `wsp` keeps a verified install per lockfile
   and hands each task its own copy-on-write clone (APFS clonefile, btrfs/xfs
   reflink): a second task with the same lockfile skips the install entirely, and
   what one task writes into its `node_modules` never leaks into another. Only the
   packages the task needs are prepared, even in a monorepo with a lockfile per
   service.
2. **Only the code the task needs — sparse checkout per subproject.** A task in
   one service of a large monorepo can get a tree with just that service and the
   folders it imports, instead of the whole repository (see
   [Sparse checkout](#sparse-checkout)). Codex worktrees have no sparse option;
   Claude Code has a single static `worktree.sparsePaths` list for every worktree.
   `wsp` picks a profile per task from `.wsp/SC-PROFILES.md`, which it builds for you.
3. **Easy on your SSD.** Clones share blocks with the cached install, so ten
   trees with the same `node_modules` do not take ten times the space. Disk use is
   accounted and limited: a free-space reserve, quotas and a tree limit stop growth
   before the disk fills up. Trees that are no longer in use are removed
   automatically: while you use `wsp` (or its Claude Code hooks), a background run
   at most once an hour evicts paused and finished tasks — no system service
   needed. Clean trees also go when a Claude Code session ends, and any task can be
   removed with one command. Unpublished work is always checkpointed first, so a
   removed tree can be restored.
4. **Many repositories from one project and one chat.** Built-in worktrees belong
   to the repository the chat is opened in. With `wsp` a single orchestrating
   project (say, `my-example-workflow`) can give one task isolated trees in
   `my-example-frontend` and `my-example-backend` at the same time, without
   opening a new thread per repository. One chat can also run several tasks.
5. **Native on macOS, works on Linux.** Built for macOS (APFS clonefile, Time
   Machine and Spotlight exclusions, optional launchd job); Linux is supported and
   tested in CI (reflink on btrfs/xfs, otherwise a normal install per tree).
   Windows is not supported yet.
6. **Safe and transparent.** You decide where trees, the dependency cache and
   checkpoints live. Trees are ordinary folders at predictable paths
   (`<worktrees_root>/<task>/<repo>`): open them, edit a file, or remove a task
   with `wsp evict` / `wsp close`. `wsp` never touches other tools' worktrees,
   never deletes work that exists only locally without your confirmation, and
   keeps secrets out of its checkpoints.

## Sparse checkout

**What it is.** Git's sparse checkout puts only selected folders of a repository
into a working tree; the rest stays in Git's history, untouched and invisible.
`wsp` uses it per task: `wsp ensure --task hub-fix --repo my-monorepo --profile
observability-hub` creates a tree with that service, the folders it imports and
the repository root files — nothing else.

**Why it matters.**

- **Faster setup and tools.** Fewer files to write when a tree is created or
  restored, and fewer files for everything that walks the tree: `git status`,
  file watchers, type checkers, test runners, search.
- **Less SSD load.** A tree with one service instead of the whole monorepo
  writes and stores a fraction of the files; together with copy-on-write
  dependencies, parallel tasks stay small.
- **Fewer tokens and faster AI.** Coding agents list, search and read files to
  find their way. With only the relevant folders present, searches return
  fewer irrelevant hits, file listings are short, and the agent does not wander
  into unrelated services — less context spent on noise, quicker to the right
  code.
- **Focus without losing correctness.** Profiles are generous on purpose:
  referenced folders are included whole, and a task can widen its tree at any
  time (`wsp sparse add`). Before changing shared code, `wsp sparse consumers`
  tells which other subprojects use it.

**.wsp/SC-PROFILES.md.** The profiles live in the repository's `.wsp/` folder,
one section per subproject, most active subprojects first, readable and editable
by people:

```markdown
## observability-hub

- path: `apps/observability-hub`
- about: Internal ops dashboard: service health, releases, incidents
- folders: `apps/observability-hub`, `.claude`, `shared`, `libs/charts`
- tags: @acme/hub, hub.acme.dev, dashboards
```

Build it with **`/task-workspaces:setup-sparse-checkout [folder]`** in Codex or
Claude Code (in Codex `$setup-sparse-checkout` works as an alternative), or
`wsp sparse scan --write`.

> **Recommended: run it once in every repository.** Besides smaller task trees,
> `.wsp/SC-PROFILES.md` is a compact index of the project — every subproject with
> a few-word brief, the folders it depends on and the domains it serves — that
> any AI agent (or new teammate) can read to understand the structure quickly.
> The command does not touch your `AGENTS.md`/`CLAUDE.md`; if you want agents to
> find the index without the skill, add a line yourself, for example:
>
> ```markdown
> Project map: `.wsp/SC-PROFILES.md` lists every subproject with a short brief,
> its folders and domains — read it first to find where a change belongs.
> ```

The scan
finds every subproject (a folder with `package.json`, `pyproject.toml`,
`go.mod`, `wrangler*`, ...; hidden tooling folders like `.claude` are not
subprojects but are included in every profile), follows its `../` references
and imports — a reference to even one file in `../web` includes all of `web` —
and tags each profile with package and worker names and the domains it is
actually served on (routes and custom domains; zones, vars, dev/staging variants
and infrastructure hosts are left out). The setup skill then has the agent write
the few-word `about` for each subproject from its AGENTS.md or README and add
documented public domains. Profiles are ordered by commits in the last 4 weeks.

**Edit it freely.** Fix a brief, add tags a subproject is missing (team,
product, service or domain names the agent should match), add folders, or write
your own profiles. A later scan keeps your additions (a hidden `wsp:auto` comment
tracks what the scanner generated). Commit the file so teammates and task bases
use the same profiles. Details: [references/sparse.md](skills/task-workspaces/references/sparse.md).

## Features

- one task → one tree per repository, own branch, fresh base, no upstream to main
- optional sparse checkout per subproject from `.wsp/SC-PROFILES.md` (scan, match, widen, consumers)
- absolute paths from `wsp ensure`; resume always goes through `ensure`
- dependencies per package dir: copy-on-write clone of a verified install
  (APFS clonefile / reflink), never symlinks
- checkpoints only for unique local work (Git objects + bundle), secrets never stored
- evict/restore, unexpected deletion recovery, squash-merge aware close
- leases, process checks, disk reserve/quotas/limits, pin, read-only inventory
- Codex plugin (skill) and Claude Code plugin (skill, hooks for
  WorktreeCreate/Remove and SessionStart/End, `bin/wsp`, `userConfig`); one shared
  `SKILL.md` and one config
- first-run wizard `wsp init` (interactive or agent-driven); nothing machine- or
  company-specific in the code — all paths and repositories come from your config;
  optional import of repositories from a Markdown table (`--repo-map`)

Python 3.9+ (standard library only — the macOS system `/usr/bin/python3` from the
Command Line Tools is enough, nothing to install) and Git 2.38+. macOS (native),
Linux (reflink or a normal install per tree). Windows is not supported yet.

## Layout

```
.codex-plugin/plugin.json         Codex plugin manifest
.claude-plugin/plugin.json        Claude Code plugin manifest with userConfig
CHANGELOG.md                      release notes per version
hooks/hooks.json                  Claude Code hooks -> bin/wsp hook claude ...
bin/wsp                           launcher (on PATH when the plugin is enabled)
skills/setup-sparse-checkout/     skill that builds .wsp/SC-PROFILES.md (/task-workspaces:setup-sparse-checkout)
skills/task-workspaces/
  SKILL.md                        agent workflow
  references/                     lifecycle, dependencies, sparse, configuration, claude-code, codex
  agents/openai.yaml              Codex skill metadata
  scripts/wsp.py                  CLI entry point
  scripts/wsp_core/               core modules
tests/                            end-to-end tests on temporary Git repositories
```

## Try it without installing

```sh
python3 skills/task-workspaces/scripts/wsp.py init          # or: init --detect --json
python3 skills/task-workspaces/scripts/wsp.py doctor
python3 skills/task-workspaces/scripts/wsp.py inventory     # read-only
python3 skills/task-workspaces/scripts/wsp.py ensure --task my-task --repo my-repo
```

Use `WSP_CONFIG=/tmp/x/config.json` to experiment with a throwaway configuration.

## Install

From the [maslow-skills](../../README.md) marketplace.

**Codex:**

```sh
codex plugin marketplace add maslowivan/maslow-skills
codex plugin add task-workspaces@maslow-skills
```

**Claude Code** (2.1.271 or newer):

```sh
claude plugin marketplace add maslowivan/maslow-skills
claude plugin install task-workspaces@maslow-skills
```

To try it in Claude Code for one session without installing:
`claude --plugin-dir /path/to/maslow-skills/plugins/task-workspaces`.

## First run

Settings live in `~/.config/wsp/config.json` and are shared by Codex, Claude Code
and the CLI, so you configure once for both agents:

- **Codex** has no install-time settings dialog: on first use the skill runs
  `wsp init --detect --json`, asks you the few open questions and writes the config.
  You can also run `wsp init` yourself in a terminal.
- **Claude Code** asks for the main options (worktrees folder, state folder,
  dependency strategy, disk reserve, automatic cleanup) when the plugin is
  enabled; the rest comes from the same `wsp init`.

## Automatic cleanup

The cleanup policy (`wsp gc`) closes tasks whose work has been merged into the
default branch (merge, fast-forward or squash — no need to run `wsp close` after
a PR is merged), evicts paused tasks after 24 hours and removes finished tasks
whose work is on the remote. It never deletes unpublished work without your
confirmation, waits while a session still holds a task, and never touches other
tools' worktrees. How it runs
is set by `janitor.mode`:

| Mode | How it runs |
| --- | --- |
| `on-use` (default) | In the background whenever `wsp` or its Claude Code hooks are used, at most once per `interval_minutes` (60). No service, nothing to install. |
| `scheduled` | A launchd (macOS) / systemd user (Linux) job, also when `wsp` is idle: `wsp janitor install`. |
| `off` | Only when you run `wsp gc --apply`. |

`wsp gc` shows what the policy would do; `wsp janitor status` shows the mode and
the last run.

## Codex and Claude Code

| | Codex | Claude Code |
| --- | --- | --- |
| Skill (`ensure`, `run`, `evict`, `close`, ...) | ✓ | ✓ |
| Session identity | `CODEX_THREAD_ID` | `CLAUDE_CODE_SESSION_ID` |
| CLI | `python3 <skill>/scripts/wsp.py` | `wsp` on PATH (plugin `bin/`) |
| Sparse profiles, `setup-sparse-checkout` skill | ✓ `/task-workspaces:setup-sparse-checkout` (or `$setup-sparse-checkout`) | ✓ `/task-workspaces:setup-sparse-checkout` |
| Built-in `--worktree` routed through wsp | — | ✓ `WorktreeCreate`/`WorktreeRemove` hooks |
| Session start/end: context and lease release | — | ✓ `SessionStart`/`SessionEnd` hooks |
| Settings dialog when enabling | — (`wsp init`) | ✓ `userConfig` |

In Codex the chat keeps its own project and works in the task trees through
explicit absolute paths (`workdir`) or `wsp run`; Codex-managed worktrees are
listed by `wsp inventory` but never touched.

## Tests

```sh
cd plugins/task-workspaces && python3 -m unittest discover -s tests -v
```

Tests create their own origin/canonical repositories under a temporary
directory with an isolated Git config; they never touch real repositories.

## Not in this version

Tracker UI, adoption of existing native worktrees, multiple writers per tree,
Windows. Tested in CI on macOS and Linux; live runs
inside Codex and Claude Code sessions are still being piloted.
