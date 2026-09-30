# maslow-skills

Plugins for [Claude Code](https://code.claude.com) and skills for Codex. This
repository is a Claude Code plugin marketplace: add it once, then install the
plugins you need.

## Plugins

| Plugin | What it does |
| --- | --- |
| [task-workspaces](plugins/task-workspaces) | Isolated Git worktrees per task across one or more repositories: absolute paths, dependency reuse via copy-on-write clones, checkpoints before cleanup, restore of removed folders. Works in Claude Code and Codex. |

## Install

**Claude Code** (2.1.271 or newer):

```sh
claude plugin marketplace add maslowivan/maslow-skills
claude plugin install task-workspaces@maslow-skills
```

Update later with `claude plugin marketplace update maslow-skills` and
`claude plugin update task-workspaces@maslow-skills`. Inside a session the same
is available through `/plugin`.

**Codex:** each plugin keeps its skill in `plugins/<plugin>/skills/<skill>`; link
or copy that folder into your Codex skills directory. See each plugin's README.

## Layout

```
.claude-plugin/marketplace.json   catalog of all plugins in this repository
plugins/<plugin>/                 one self-contained plugin per folder
  .claude-plugin/plugin.json      manifest (name, version, options)
  skills/<skill>/SKILL.md         skill instructions (also usable by Codex)
  hooks/, bin/, tests/            optional
scripts/validate.sh               checks every plugin and the catalog
```

## Adding a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json` and at least one
   `skills/<skill>/SKILL.md`.
2. Add an entry to `.claude-plugin/marketplace.json` with
   `"source": "./plugins/<name>"`.
3. If the plugin has `tests/`, CI runs them automatically.
4. Run `scripts/validate.sh`.

Bump `version` in the plugin's `plugin.json` for every release: users on a
pinned version only receive updates when it changes.

## License

[MIT](LICENSE)
