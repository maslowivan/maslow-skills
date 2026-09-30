# task-workspaces (`wsp`)

Isolated Git worktrees per task, across one or more repositories, for Codex,
Claude Code and humans at the command line.

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

Python 3.11+ standard library and Git 2.38+. macOS fully, Linux without
clonefile (reflink or install), Windows not supported.

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
  dependency strategy, disk reserve) when the plugin is enabled; the rest comes
  from the same `wsp init`.

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
multiple writers per tree. Not verified yet: live runs inside Codex and Claude
Code sessions, Linux. The background janitor is implemented but never installed
automatically (`wsp janitor install`).
