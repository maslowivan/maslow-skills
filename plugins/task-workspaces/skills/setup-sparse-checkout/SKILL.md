---
name: setup-sparse-checkout
description: Build or refresh SC-PROFILES.md, the sparse-checkout profiles of a repository's subprojects (folders plus tags such as package names and domains) that task-workspaces uses to create small task trees. Use when the user runs /task-workspaces:setup-sparse-checkout, or asks to set up or update sparse checkout profiles for a repository or monorepo.
---

# Set up sparse-checkout profiles

Target folder: `$ARGUMENTS` — if this is empty or still reads `$ARGUMENTS`, use the
folder the user named, otherwise the current working directory. Any folder inside
the repository works; the file is written to the repository root.

**Command:** `wsp` (on PATH with the Claude Code plugin), otherwise
`python3 <this skill folder>/../task-workspaces/scripts/wsp.py`. Add `--json` when
you parse the output.

## Steps

1. **Preview.** `wsp sparse scan <folder> --json`. It finds every subproject
   (a folder with a package manifest such as `package.json`, `pyproject.toml`,
   `go.mod`, `wrangler.jsonc`) and builds for each:
   - **folders** — the subproject plus every folder it references with `../`,
     always whole folders (`import "../web/x"` includes all of `web`), followed
     transitively through real imports;
   - **tags** — profile name, package and worker names, and domains the
     subproject is served on (routes / custom domains in `wrangler.*`,
     `netlify.toml`, `vercel.json`, `CNAME`, `astro.config` `site`, plus hosts
     of the repository's own domains mentioned in its README, AGENTS.md or
     CLAUDE.md).
   Nothing is written yet. An existing `SC-PROFILES.md` is merged: user-added
   folders, tags and whole profiles are kept.
2. **Review with the user.** Show a short table: profile, path, number of
   folders, tags. Point out profiles that include a large part of the
   repository; `wsp sparse scan <folder> --details --json` shows which reference
   pulled each folder in. Optionally read each subproject's README/AGENTS.md
   briefly and propose extra tags that are clearly right (product or team
   names, domains) — do not invent.
3. **Write.** `wsp sparse scan <folder> --write`. Add the agreed extra tags by
   editing only the `- tags:` lines of `SC-PROFILES.md`.
4. **Tell the user**, in their language:
   - where the file is and how many profiles it has;
   - that they can edit `SC-PROFILES.md` themselves: add tags a subproject is
     missing (team, product, domains, services), add folders it needs, or add
     their own profiles — a later scan keeps these edits;
   - that tasks use a profile with `wsp ensure --task <id> --repo <repo> --profile <name>`,
     and the task-workspaces skill picks profiles by these tags;
   - that the file should be committed so teammates and task bases use it
     (until then wsp falls back to the working copy).

Do not commit, push or open a pull request unless the user asks.
