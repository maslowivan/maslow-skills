# Lifecycle, ownership and recovery

## Entities

- **Task** — stable id, title, tracker ref, status `open | in_progress | paused | completed | cancelled`, pin flag.
- **Session** — app (`claude`, `codex`, `cli`), real session id, confirmed or not, role owner/observer.
- **Tree** — one worktree of one repository for one task: path, branch, base SHA, generation, state.
- **Lease** — one write owner per tree (`held | released | uncertain`); other sessions are observers.
- **Operation** — durable journal of create/restore/evict with stage and disk reservation.

Task status and folder state are independent: a completed task may still have a
folder; an unfinished task may have no folder but a checkpoint.

## Tree states

```
provisioning -> ready -> snapshotting -> evicting -> evicted
                          evicted/missing -> restoring -> ready
also: missing (folder vanished unexpectedly), unknown (ownership unproven),
      recovery_failed, closed
```

`unknown` is never removed automatically. `missing` keeps its record and
checkpoint and is restored by `wsp ensure`.

## Checkpoints

Created only when a tree has **unique local state**: uncommitted or untracked
changes, commits not on any remote, or profile-allowed ignored files. Clean,
pushed trees need no checkpoint: restore recreates them from the branch.

A checkpoint is Git objects (index tree + full working tree built in a temporary
index) under `refs/wsp/<task>/<repo>/<n>`, a `git bundle` of that ref (everything
not already on a remote) and an optional archive of allowed ignored files.
Secrets are never included; they are re-created from their source on restore.
HEAD, index and stash of the user are never touched.

Checkpoints are deleted as soon as their content is on the remote or the task is
closed. There is no age-based retention. When checkpoints exceed
`limits.checkpoints_gib`, new trees/restores/installs are refused
(`QUOTA_EXCEEDED` with `holders`) — saved work is never deleted to make room.

A task with several repositories also gets a **set manifest**
(`<state_dir>/manifests/<task>/_task.json`): each tree's state, branch, base,
head, what it is recoverable from (checkpoint or branch), incomplete parts and
the verification order. Repositories are restored independently; commits and PRs
across repositories are not a transaction.

## Eviction order

lock → check leases, processes (cwd scan) and ownership marker → classify
ignored files (unknown kinds block) → checkpoint if unique → re-check that
nothing changed → unlock registration → `git worktree remove` → verify the folder
is gone → `evicted`. Any failure leaves the code in the folder or in a verified
checkpoint.

## Restore

`wsp ensure` (or `wsp restore`) on an evicted/missing tree: verifies the
checkpoint (ref, objects, bundle checksum, `git bundle verify`), recovers the ref
from the bundle if needed, removes only this tree's stale registration (proven by
the marker in its admin dir), checks out the branch at the checkpoint HEAD,
applies working state and index, compares tree hashes, re-creates secrets and
re-prepares dependencies. The base is not moved to a newer main. An occupied
path or a branch checked out elsewhere is a conflict (`RESTORE_CONFLICT`,
`BRANCH_BUSY`) — never overwritten or forced.

## Automatic cleanup

`wsp gc` is the cleanup policy: evict paused tasks older than
`policy.paused_evict_after_hours`, close finished tasks whose work is on the
remote, delete checkpoints whose content reached the remote, drop unused
dependency instances, mark stale leases `uncertain`, mark abandoned operations.
Pinned, `unknown`, leased or process-used trees are kept; finished tasks with
unpublished work are only reported (`needs_confirmation`).

With `janitor.mode: on-use` (default) the policy runs by itself in a detached
background process after normal `wsp` commands and Claude Code hooks, at most
once per `janitor.interval_minutes`; a lock prevents parallel runs. There is no
service. `scheduled` adds a launchd/systemd user job; `off` leaves it to
`wsp gc --apply`.

## Lost source checkout

If the canonical checkout itself is gone, `wsp ensure/restore --reclone-source`
clones `origin` into the configured path (only when that path is missing or an
empty directory — never over other files), fetches the checkpoint from its
bundle and restores the tree. Without the flag the result is `SOURCE_MISSING`.

## Leases and heartbeats

`ensure` and `wsp run` refresh the lease heartbeat. `wsp gc` marks a held lease
whose heartbeat is older than `policy.lease_stale_hours` (default 12) and has no
live registered process as `uncertain`. An uncertain lease still protects the
tree (nobody else can evict or close it); the holder re-acquires it with
`ensure`, or releases it explicitly with `wsp release --holder <holder>`. A
registered process whose PID (checked together with its start time) is gone is
cleared from the lease.

## Git rules used

- `git fetch origin <default>` updates remote-tracking refs only, with retries on
  lock contention; the local main and the source checkout are untouched.
- `git worktree add --no-track -b <prefix><task>` — no upstream `origin/main`.
- `git worktree lock --reason wsp:<task>` right after creation, so foreign
  `git worktree prune` keeps the registration of a temporarily missing tree.
- Ownership marker `wsp-owner.json` lives in the tree's git admin dir.
- Squash-merged work is recognised with `git merge-tree --write-tree`: merging
  the branch into the default branch changes nothing.

## Error codes (exit status)

| Code | Exit | Meaning / what to do |
| --- | --- | --- |
| CONFIG_MISSING | 10 | run `wsp init` |
| REPO_UNKNOWN | 12 | repository not configured: `wsp config add-repo --path P` |
| SOURCE_MISSING / SOURCE_INVALID | 53 / 13 | source checkout missing or not a repo |
| ORIGIN_MISMATCH | 14 | checkout origin differs from config |
| INVALID_ID | 17 | use the suggested slug |
| PATH_UNSAFE | 18 | root is a symlink, has node_modules/package.json above it, or path escapes |
| PATH_CONFLICT / RESTORE_CONFLICT | 19 / 52 | target occupied; inspect, never overwrite |
| FETCH_FAILED | 20 | network/auth; `--allow-stale-base` only with the user's consent (marked NOT fresh) |
| BRANCH_EXISTS / BRANCH_BUSY | 21 / 22 | pick `--branch` or `--from-remote-branch` |
| LOCKED | 25 | another wsp operation runs; retry later |
| DISK_LOW / QUOTA_EXCEEDED / LIMIT_REACHED | 30 / 31 / 32 | free space or evict idle tasks (`wsp gc`); `wsp run` also returns DISK_LOW when it stopped a command over budget |
| LEASE_HELD | 40 | another session owns the tree |
| PROCESS_RUNNING | 41 | stop listed processes first |
| PINNED | 42 | `wsp unpin` only on request |
| UNKNOWN_OWNERSHIP | 43 | leave it; report to the user |
| UNIQUE_STATE | 44 | local work would be lost; ask the user before `--discard` |
| UNCLASSIFIED_IGNORED | 45 | classify in the profile or ask before `--accept-unclassified` |
| CHECKPOINT_MISSING / CHECKPOINT_INVALID | 50 / 51 | do not remove anything; report |
| DEPS_INCOMPATIBLE / DEPS_FAILED | 60 / 61 | rerun `wsp deps`; show install output |
| INTERNAL | 1 | bug; include the trace in the report |
