# task-workspaces (`wsp`)

Isolated Git worktrees per task, across one or more repositories, for coding
agents (Claude Code, Codex) and humans.

- one task → one tree per repository, own branch, fresh base, no upstream to main
- absolute paths from `wsp ensure`; resume always goes through `ensure`
- dependencies per package dir: copy-on-write clone of a verified install
  (APFS clonefile / reflink), never symlinks
- checkpoints only for unique local work (Git objects + bundle), secrets never stored
- evict/restore, unexpected deletion recovery, squash-merge aware close
- leases, process checks, disk reserve/quotas/limits, pin, read-only inventory
- Claude Code plugin: skill, hooks (WorktreeCreate/Remove, SessionStart/End),
  `bin/wsp`, `userConfig`; Codex skill: same `SKILL.md`, CLI by path
- first-run wizard `wsp init` (interactive or agent-driven); nothing machine- or
  company-specific in the code — all paths and repositories come from your config;
  optional import of repositories from a Markdown table (`--repo-map`)

Python 3.11+ standard library and Git 2.38+. macOS fully, Linux without
clonefile (reflink or install), Windows not supported.

## Layout

```
.claude-plugin/plugin.json        plugin manifest with userConfig
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

**Claude Code** (2.1.271 or newer), from the [maslow-skills](../../README.md) marketplace:

```sh
claude plugin marketplace add maslowivan/maslow-skills
claude plugin install task-workspaces@maslow-skills
```

Claude Code asks for the plugin options (worktrees folder, state folder,
dependency strategy, disk reserve) when the plugin is enabled. To try it for one
session without installing: `claude --plugin-dir /path/to/maslow-skills/plugins/task-workspaces`.

**Codex:** link or copy `skills/task-workspaces` into your Codex skills directory,
for example from the repository root:

```sh
ln -s "$PWD/plugins/task-workspaces/skills/task-workspaces" ~/.codex/skills/task-workspaces
```

Then run `wsp init` once (or let the agent run `wsp init --detect --json` and
ask you the questions). Settings live in `~/.config/wsp/config.json` and are
shared by the CLI, Codex and Claude Code.

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
