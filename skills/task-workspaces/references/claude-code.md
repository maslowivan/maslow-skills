# Claude Code integration

- **Identity:** `CLAUDE_CODE_SESSION_ID` is set for Bash commands; `wsp` uses it
  as `claude:<id>` automatically. It changes on `/clear`: `ensure` attaches the new
  session to the same task, it does not create a new task.
- **Plugin layout:** `skills/task-workspaces/`, `hooks/hooks.json`, `bin/wsp`
  (on PATH while the plugin is enabled), `userConfig` in `.claude-plugin/plugin.json`.

## Hooks

| Hook | wsp behaviour |
| --- | --- |
| `WorktreeCreate` | Repository in the wsp config → `wsp ensure` (task id = slug of the branch, base = the given commit) and print the manager-owned path. Other repositories → standard `git worktree add` under `<repo>/.claude/worktrees/<name>`. Non-zero exit fails creation (Claude has no fallback). |
| `WorktreeRemove` | wsp tree: release the session lease; clean and unused → `evict` (restorable); otherwise checkpoint and **keep**. Other trees: remove only if clean and on the remote. **Always exits 0**: on a non-zero exit Claude Code would `rm -rf` the folder. |
| `SessionStart` | cwd inside a wsp tree → attach the session, take the lease if free, add the task's absolute paths as context. |
| `SessionEnd` | release this session's leases (`uncertain` if processes still use the tree). |

Hook commands receive `CLAUDE_PLUGIN_OPTION_<KEY>`; `CLAUDE_PLUGIN_ROOT` and
plugin options are not available to Bash tool commands, so the skill calls
`wsp` by name (plugin `bin/`) or by absolute path.
