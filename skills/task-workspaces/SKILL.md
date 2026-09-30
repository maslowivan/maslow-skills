---
name: task-workspaces
description: Give every coding task its own isolated worktrees in one or more repositories, with current absolute paths, dependency reuse, checkpoints before cleanup and restore of removed folders. Use when starting, resuming, pausing or finishing implementation work in a Git repository, when the user asks where a task's code lives, how much disk the task folders take, or to clean up or restore task folders. Not for read-only research.
---

# Task workspaces (`wsp`)

`wsp` gives each task isolated Git worktrees (one per repository, own branch),
tracks who uses them, saves unfinished work before a folder is removed and
restores it later. The CLI does the file, Git, lock and disk work; you follow
the order below and report paths and state to the user.

**Command:** `wsp` (on PATH when installed as a Claude Code plugin); otherwise
`python3 <this skill folder>/scripts/wsp.py`. Always add `--json` when you parse
the output. Errors come back as `{"ok": false, "error": {"code": ...}}` with a
stable code; see `references/lifecycle.md`.

## First use

If any command returns `CONFIG_MISSING`, run setup before anything else:

1. `wsp init --detect --json` — read `environment` and `questions`.
2. Ask the user the questions whose `recommended` value is not obviously right
   (at least: which repositories, where trees live). Offer the listed options
   and the recommended value. Never invent paths.
3. Write the answers to a JSON file and run `wsp init --answers FILE --json`;
   show the user the returned `diff`. Then `wsp doctor`.

## Start or resume a task

1. **Mode.** Research, review-only and question answering do not create
   worktrees. Only implementation work does.
2. **Task id.** Short ASCII slug `[a-z0-9-]`, ≤48 chars, stable for the task
   (e.g. from the tracker row). One chat can run several tasks.
3. **Repositories.** Decide every repository the task needs (`wsp config show`
   lists configured ones). Read their instructions (AGENTS.md / CLAUDE.md).
4. **Ensure.** `wsp ensure --task ID --repo NAME [--repo NAME2] [--deps NAME:PACKAGE_DIR] --json`
   - reuses, creates (from a fresh `origin/<default>`), or restores the trees;
   - to continue an existing remote branch: `--branch B --from-remote-branch`;
   - it never switches or modifies the source checkout.
5. **Report** to the user: each absolute path, branch, whether the base is
   fresh, lease (owner/observer), dependency state, and the disk line. Do not
   call a tree ready unless `ensure` said so.
6. **Work only through these paths.** Pass them as the explicit working
   directory of every command. A path from an old message is not a source of
   truth: call `ensure` again when resuming.

## While working

- Heavy commands (build, tests, installs): `wsp run --task ID --repo NAME [--package DIR] [--max-growth-gib N] -- <cmd>`.
  It holds the lease, checks dependency freshness and disk, and stops only its
  own command (then checkpoints) if disk falls below the emergency floor or the
  command exceeds its disk budget.
- Dependencies for one package: `wsp deps --task ID --repo NAME --package DIR --json`
  (clone from the verified cache when possible, otherwise install). Use the
  returned `package_cwd` for package commands.
- `wsp checkpoint --task ID` after significant steps while changes are not yet pushed.
- `DEPS_INCOMPATIBLE` means lockfile/manifests changed: run `wsp deps` again.

## Pause, finish, clean up

- Pause: `wsp release --task ID --pause` (checkpoints unique work, frees your lease).
- Free disk now: `wsp evict --task ID` (checkpoint if needed, remove folders; restorable
  with `wsp ensure`). Stop your own dev servers first; `PROCESS_RUNNING` lists them.
- Finished (PR merged/closed): `wsp close --task ID`. If it returns `UNIQUE_STATE`,
  show the user exactly what would be lost (`would_lose`) and rerun with
  `--discard` **only after the user explicitly confirms**.
- Overview: `wsp list`, `wsp status --task ID` (includes the task's set manifest),
  `wsp inventory` (all worktrees of configured repos, read-only), `wsp gc` (dry run
  of the cleanup policy).
- Source checkout gone (`SOURCE_MISSING`): tell the user; with their consent run
  `wsp ensure ... --reclone-source` (clones origin into the missing/empty path and
  restores from the checkpoint bundle).
- `QUOTA_EXCEEDED` for checkpoints: saved unpublished work is never deleted to make
  room; show `details.holders` and ask the user which tasks to push or close.

## Never

- Delete, prune or `--force` other people's or other tools' worktrees
  (`inventory` shows them as `codex`, `claude`, `unowned`); report them instead.
- Run `git worktree prune`, `git gc --prune=now` or `rm -rf` on task folders —
  use `evict`/`close`.
- Pass `--discard`, `--accept-unclassified`, `--force`, `--reclone-source` or
  `wsp gc --apply` without the user's explicit request in this conversation.
- Store secrets in checkpoints, logs or commits; `wsp` re-creates env files
  from their configured source.

References: `references/lifecycle.md` (states, error codes, restore),
`references/dependencies.md`, `references/configuration.md`,
`references/claude-code.md`, `references/codex.md`.
