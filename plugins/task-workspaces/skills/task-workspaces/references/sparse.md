# Sparse-checkout profiles (.wsp/SC-PROFILES.md)

A task tree can contain only the folders a subproject needs instead of the whole
repository. Profiles live in `.wsp/SC-PROFILES.md` (next to `.wsp/profile.json`),
one section per subproject, sorted by recent commit activity:

```markdown
## observability-hub

- path: `apps/observability-hub`
- about: Internal ops dashboard: service health, releases, incidents
- folders: `apps/observability-hub`, `.claude`, `shared`, `libs/charts`
- tags: @acme/hub, hub.acme.dev, dashboards
<!-- wsp:auto {"folders": [...], "tags": ["@acme/hub", "hub.acme.dev"], "about": null} -->
```

- **folders** are always whole folders (Git cone mode). Files in the repository
  root and in every parent folder of an included folder are always present, and
  so is `.wsp/`.
- **about** is a few-word brief for agents choosing a profile.
- **tags** help choose a profile: package and worker names, served domains, plus
  anything people add (team, product, service names). The profile name itself is
  never repeated in tags.
- **`wsp:auto`** (hidden when rendered) records what the scanner generated. A
  re-scan keeps everything a person added or re-spelled and drops generated
  items the scanner no longer finds. Leave it in place.
- `- notes:` lines are free text and kept.

## Building it

`wsp sparse scan [FOLDER]` previews, `--write` writes, `--rebuild` regenerates a
file written before `wsp:auto` markers existed, `--activity-days N` changes the
ordering window (default 28). `/task-workspaces:setup-sparse-checkout` walks the
user through it and has the agent write the briefs.

- **Subprojects:** folders with a manifest (`package.json`, `pyproject.toml`,
  `go.mod`, `Cargo.toml`, `wrangler*`, ...), unless inside another subproject
  that owns a lockfile (part of that workspace) or inside a hidden folder
  (`.claude`, `.agents`, `.github` hold tooling, not subprojects).
- **Folders:** any `../` path in the subproject's code and config files includes
  the target folder whole — the folder directly under the common ancestor, one
  level deeper when that folder only groups other subprojects
  (`../../services/common/x` includes `services/common`, not all of `services`);
  a target inside another subproject includes that subproject. Included folders
  are followed transitively, but only through real `import`/`require`/`@import`
  statements in non-test files. Workspace dependencies by package name are
  included; references into `node_modules`, `dist` and fixtures are ignored.
  `.husky`, `.yarn` and root agent folders (`.claude`, `.agents`, `.codex`,
  `.cursor`, `.gemini`, `.windsurf`) are added when present.
- **Domains:** where a service is served — `routes`, `pattern`, `custom_domain`
  in any `wrangler*.jsonc/json/toml`, `netlify.toml`, `vercel.json`, `CNAME`,
  `site` in framework configs. Ignored: commented routes, `zone_name`, vars,
  image `hostname` settings, infrastructure hosts (cloudfront, amazonaws,
  workers.dev, ...). `www.x` counts as `x`; dev/staging/preview variants are kept
  only when no production host is known (a `.dev` TLD is not a variant); brand
  domains embedded in preview hosts (`www.shop.com.pre-release.example.org`)
  count. README/AGENTS.md/CLAUDE.md hosts are used only for subprojects without
  routes, and only specific service hosts (never a bare domain or `www.`).
- **Order:** commits touching the subproject in the last 28 days, then path.

## Using it

| Command | Purpose |
| --- | --- |
| `wsp sparse match "<words>" --repo R --json` | profiles ranked by whole-word matches in name, tags and paths |
| `wsp ensure --task T --repo R --profile NAME [--profile NAME2] [--folder F]` | create a sparse tree (with several repos: `--profile R:NAME`) |
| `wsp sparse add --task T --repo R --profile NAME / --folder F` | widen an existing sparse tree |
| `wsp sparse consumers --repo R --of FOLDER --json` | profiles that include a folder (who else a shared change affects) |
| `wsp sparse list --repo R` | show the parsed profiles |

Profiles are read from the file as committed at the task's base commit (then the
default branch, then the working copy; a root `SC-PROFILES.md` from 0.2 is still
read). A sparse tree records its profiles and folders; evict/restore recreates
the same cone. An existing full tree is never narrowed.

Git enables `extensions.worktreeConfig` in the repository config the first time
a sparse worktree is created (the sparse settings are per worktree; the main
checkout stays a full checkout). `wsp` reports this once as a warning.
