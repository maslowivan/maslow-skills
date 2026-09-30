# Sparse-checkout profiles (SC-PROFILES.md)

A task tree can contain only the folders a subproject needs instead of the whole
repository. Profiles live in `SC-PROFILES.md` in the repository root, one section
per subproject:

```markdown
## observability-hub

- path: `apps/observability-hub`
- folders: `apps/observability-hub`, `shared`, `libs/charts`
- tags: observability-hub, @acme/hub, hub.acme.dev, dashboards
```

- **folders** are always whole folders (Git cone mode). Files in the repository
  root and in every parent folder of an included folder are always present, so
  root configs and `AGENTS.md`/`CLAUDE.md` along the way are available.
- **tags** help choose a profile: profile, package and worker names, served
  domains, plus anything the user adds (team, product, service names).
- `- notes:` lines are free text and kept.

## Building it

`wsp sparse scan [FOLDER]` previews, `--write` writes; `/task-workspaces:setup-sparse-checkout`
walks the user through it. Heuristic, deliberately generous:

- a subproject is a folder with a manifest (`package.json`, `pyproject.toml`,
  `go.mod`, `Cargo.toml`, `wrangler.*`, ...), unless it sits inside another
  subproject that owns a lockfile (then it is part of that workspace);
- any `../` path in the subproject's code and config files includes the target
  folder whole — the folder directly under the common ancestor, one level deeper
  when that folder only groups other subprojects (`../../services/common/x`
  includes `services/common`, not all of `services`); a target inside another
  subproject includes that subproject;
- included folders are followed transitively, but only through real
  `import`/`require`/`@import` statements in non-test files, so shared code does
  not drag in every consumer its tests mention;
- workspace dependencies by package name (`"@acme/utils": "workspace:*"`) are
  included; references into `node_modules`, `dist`, fixtures are ignored;
- `.husky` and `.yarn` are added when present.

Domains come from where a service is served (`routes`, `pattern`,
`custom_domain`, `zone_name` in `wrangler.*`, `netlify.toml`, `vercel.json`,
`CNAME`, `site` in framework configs). Hosts in the subproject's README,
AGENTS.md or CLAUDE.md are added only when they belong to the repository's own
served domains. Library and documentation links are dropped.

A re-scan merges: user-added folders and tags stay, user-created profiles stay,
profiles whose path disappeared get a note instead of being deleted.

## Using it

| Command | Purpose |
| --- | --- |
| `wsp sparse match "<words>" --repo R --json` | profiles ranked by tags, name, path and folders |
| `wsp ensure --task T --repo R --profile NAME [--profile NAME2] [--folder F]` | create a sparse tree (with several repos: `--profile R:NAME`) |
| `wsp sparse add --task T --repo R --profile NAME / --folder F` | widen an existing sparse tree |
| `wsp sparse consumers --repo R --of FOLDER --json` | profiles that include a folder (who else a shared change affects) |
| `wsp sparse list --repo R` | show the parsed profiles |

Profiles are read from `SC-PROFILES.md` as committed at the task's base commit;
if it is not committed yet, the working copy of the source checkout is used.
A sparse tree records its profiles and folders; evict/restore recreates the same
cone. An existing full tree is never narrowed.

Git enables `extensions.worktreeConfig` in the repository config the first time
a sparse worktree is created (the sparse settings are per worktree; the main
checkout stays a full checkout). `wsp` reports this once as a warning.
