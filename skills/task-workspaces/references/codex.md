# Codex integration

- **Identity:** if `CODEX_THREAD_ID` is present, `wsp` records `codex:<id>` as a
  confirmed session. It never invents an id; without one the session is
  `cli:<user>@<host>` and marked unconfirmed.
- **Working directory:** the chat stays in its project (for example a workflow
  folder); every command uses the absolute tree path as an explicit `workdir`,
  or goes through `wsp run`.
- **Sandbox:** if writes to `worktrees_root` or `state_dir` are denied, request
  access to exactly those directories; do not disable the sandbox.
- **Native tools:** Codex-managed worktrees are a different lifecycle. `wsp`
  does not adopt, archive or delete them; `wsp inventory` lists them as
  `codex`. Native Handoff and the review panel are not available for wsp trees;
  use Git and the pull request for diffs.
- **Chat status:** an archived chat is a signal to run `wsp status`/`wsp gc`,
  not proof that processes stopped or the task is done. An error or a missing
  chat in a recent list means `unknown`, never deletion.
- **Setup:** there is no install-time dialog in Codex; when a command returns
  `CONFIG_MISSING`, run the `wsp init --detect --json` → ask → `wsp init --answers`
  flow from SKILL.md.
