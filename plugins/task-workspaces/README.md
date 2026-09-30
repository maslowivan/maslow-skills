# task-workspaces (`wsp`)

A powerful alternative to the built-in worktrees of **Codex** and **Claude Code**:
isolated Git worktrees per task, across one or more repositories, that are fast
to prepare, easy on your SSD and fully under your control.

## Why use it instead of built-in worktrees

1. **Faster to a working tree.** A fresh built-in worktree has no dependencies,
   so every task starts with a full `npm`/`yarn`/`pnpm` install. `wsp` keeps a
   verified install per lockfile and hands it to the next task as a
   copy-on-write clone (APFS clonefile, btrfs/xfs reflink): a second task with the
   same lockfile skips the install entirely. Only the packages the task needs are
   prepared, even in a monorepo with a lockfile per service.
2. **Easy on your SSD.** Clones share blocks with the cached install, so ten
   trees with the same `node_modules` do not take ten times the space. Disk use is
   accounted and limited: a free-space reserve, quotas and a tree limit stop growth
   before the disk fills up. Trees that are no longer in use are removed
   automatically: while you use `wsp` (or its Claude Code hooks), a background run
   at most once an hour evicts paused and finished tasks — no system service
   needed. Clean trees also go when a Claude Code session ends, and any task can be
   removed with one command. Unpublished work is always checkpointed first, so a
   removed tree can be restored.
3. **Many repositories from one project and one chat.** Built-in worktrees belong
   to the repository the chat is opened in. With `wsp` a single orchestrating
   project (say, `my-example-workflow`) can give one task isolated trees in
   `my-example-frontend` and `my-example-backend` at the same time, without
   opening a new thread per repository. One chat can also run several tasks.
4. **Native on macOS, works on Linux.** Built for macOS (APFS clonefile, Time
   Machine and Spotlight exclusions, optional launchd job); Linux is supported and
   tested in CI (reflink on btrfs/xfs, otherwise a normal install per tree).
   Windows is not supported yet.
5. **Safe and transparent.** You decide where trees, the dependency cache and
   checkpoints live. Trees are ordinary folders at predictable paths
   (`<worktrees_root>/<task>/<repo>`): open them, edit a file, or remove a task
   with `wsp evict` / `wsp close`. `wsp` never touches other tools' worktrees,
   never deletes work that exists only locally without your confirmation, and
   keeps secrets out of its checkpoints.

## Features

- one task → one tree per repository, own branch, fresh base, no upstream to main
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

Python 3.11+ standard library and Git 2.38+. macOS (native), Linux (reflink or a
normal install per tree). Windows is not supported yet.

## Layout

```
.codex-plugin/plugin.json         Codex plugin manifest
.claude-plugin/plugin.json        Claude Code plugin manifest with userConfig
hooks/hooks.json                  Claude Code hooks -> bin/wsp hook claude ...
bin/wsp                           launcher (on PATH when the plugin is enabled)
skills/task-workspaces/
  SKILL.md                        agent workflow
  references/                     lifecycle, dependencies, configuration, claude-code, codex
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

The cleanup policy (`wsp gc`) evicts paused tasks after 24 hours and removes
finished tasks whose work is on the remote; it never deletes unpublished work
without your confirmation and never touches other tools' worktrees. How it runs
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

Sparse-checkout profiles, tracker UI, adoption of existing native worktrees,
multiple writers per tree, Windows. Tested in CI on macOS and Linux; live runs
inside Codex and Claude Code sessions are still being piloted.
