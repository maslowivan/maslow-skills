# maslow-skills

Plugins and skills for **Codex** and **Claude Code**. The repository is a plugin
marketplace for both: add it once in your agent, then install the plugins you
need. Every plugin ships the same skill for both agents.

## Plugins

| Plugin | Codex | Claude Code | What it does |
| --- | :-: | :-: | --- |
| [task-workspaces](plugins/task-workspaces) | ✓ | ✓ | A faster, SSD-friendly alternative to built-in worktrees, plus a per-repository project index for agents (`.wsp/SC-PROFILES.md`): isolated trees per task in several repositories from one project and chat, sparse checkout per subproject (fewer files, fewer tokens), reused dependency installs via copy-on-write clones, disk limits and automatic cleanup, checkpoints and restore. macOS native, Linux supported. [Why →](plugins/task-workspaces#why-use-it-instead-of-built-in-worktrees) |

## Install

### Codex

```sh
codex plugin marketplace add maslowivan/maslow-skills
codex plugin add task-workspaces@maslow-skills
```

Update later with `codex plugin marketplace upgrade maslow-skills`, then
`codex plugin add task-workspaces@maslow-skills` again (see [Versions and updates](#versions-and-updates)).

### Claude Code (2.1.271 or newer)

```sh
claude plugin marketplace add maslowivan/maslow-skills
claude plugin install task-workspaces@maslow-skills
```

Update later with `claude plugin marketplace update maslow-skills` and
`claude plugin update task-workspaces@maslow-skills`. Inside a session the same
is available through `/plugin`.

### Versions and updates

Every plugin has a version in its manifests and a `CHANGELOG.md`; each release is
tagged `<plugin>-v<version>` with [release notes](https://github.com/maslowivan/maslow-skills/releases).
To get the latest versions:

```sh
# Codex: refresh the catalog, then add the plugin again (installs the new version, removes the old one)
codex plugin marketplace upgrade maslow-skills
codex plugin add task-workspaces@maslow-skills

# Claude Code
claude plugin marketplace update maslow-skills
claude plugin update task-workspaces@maslow-skills
```

### Only the skill, without a plugin

Every skill is a plain folder at `plugins/<plugin>/skills/<skill>`; you can also
link or copy it into `~/.codex/skills/` or `~/.claude/skills/`.

## Layout

```
.agents/plugins/marketplace.json  Codex catalog of all plugins
.claude-plugin/marketplace.json   Claude Code catalog of all plugins
plugins/<plugin>/
  .codex-plugin/plugin.json       Codex manifest
  .claude-plugin/plugin.json      Claude Code manifest (options, hooks)
  skills/<skill>/SKILL.md         the skill, shared by both agents
  hooks/, bin/, tests/            optional (hooks and bin/ are Claude Code features)
scripts/validate.sh               checks both catalogs and all manifests
scripts/check_versions.py         changed plugins must bump version + changelog (CI)
```

## Adding a plugin

1. Create `plugins/<name>/` with `skills/<skill>/SKILL.md`,
   `.codex-plugin/plugin.json` (`"skills": "./skills/"`) and
   `.claude-plugin/plugin.json`, both with the same `name` and `version`.
2. Add an entry to both catalogs: `.agents/plugins/marketplace.json`
   (`"source": {"source": "local", "path": "./plugins/<name>"}`) and
   `.claude-plugin/marketplace.json` (`"source": "./plugins/<name>"`).
3. If the plugin has `tests/`, CI runs them automatically.
4. Run `scripts/validate.sh`.

**Releases.** Every change to a plugin must bump `version` in both manifests
(semantic versioning) and add a `## <version>` section to the plugin's
`CHANGELOG.md` — CI (`scripts/check_versions.py`) fails otherwise. When the change
lands on `main`, CI tags it `<plugin>-v<version>` and publishes a GitHub release
with the changelog section, so installed copies can be upgraded.

## License

[MIT](LICENSE)
