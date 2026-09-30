---
name: setup-sparse-checkout
description: Build or refresh .wsp/SC-PROFILES.md, the sparse-checkout profiles of a repository's subprojects (folders, a few-word brief and tags such as package names and domains) that task-workspaces uses to create small task trees. Use when the user runs /task-workspaces:setup-sparse-checkout or $setup-sparse-checkout, or asks to set up or update sparse checkout profiles or a project index for a repository or monorepo.
---

# Set up sparse-checkout profiles

Target folder: `$ARGUMENTS` — if this is empty or still reads `$ARGUMENTS`, use the
folder the user named, otherwise the current working directory. Any folder inside
the repository works; the file is written to `.wsp/SC-PROFILES.md` in the
repository root.

**Command:** `wsp` (on PATH with the Claude Code plugin), otherwise
`python3 <this skill folder>/../task-workspaces/scripts/wsp.py`. Add `--json` when
you parse the output.

## Steps

1. **Preview.** `wsp sparse scan <folder> --json`. It finds every subproject
   (a folder with `package.json`, `pyproject.toml`, `go.mod`, `wrangler*.jsonc`,
   ...; hidden folders such as `.claude` are tooling, not subprojects) and builds
   for each, sorted by commit activity of the last 28 days:
   - **folders** — the subproject plus every folder it references with `../`,
     always whole folders (`import "../web/x"` includes all of `web`), followed
     transitively through real imports; root agent folders (`.claude`,
     `.agents`, ...) are added to every profile;
   - **about** — from `package.json` `description` when there is one;
   - **tags** — package and worker names and the hosts the subproject is served
     on (routes / custom domains in `wrangler*`, `netlify.toml`, `vercel.json`,
     `CNAME`, framework `site`); for subprojects without routes, specific
     service hosts from their README/AGENTS.md/CLAUDE.md.
   Nothing is written yet. An existing file is merged: what a person added or
   re-spelled is kept; what the scanner added earlier and no longer finds is
   dropped (tracked by the hidden `wsp:auto` comment).
2. **Write.** `wsp sparse scan <folder> --write`.
3. **Brief and public domains (your part).** For every profile whose `about` is
   missing or vague, read the subproject's AGENTS.md / CLAUDE.md / README.md
   (the first screen is enough; delegate to a subagent for many subprojects) and
   write a brief of 4–12 words saying what it is or does, e.g.
   `Internal ops dashboard: service health, releases, incidents`. Add the public
   production domains it serves when the docs state them clearly (bare hosts, no
   dev/staging/preview) and obviously right names (product, team, service). Do not
   add domains a subproject merely mentions or calls, and never guess. Edit only
   the `- about:` and `- tags:` lines; keep the `wsp:auto` comments.
4. **Check.** `wsp sparse scan <folder> --json` must report `changed: false`.
   Show the user a short table: profile, brief, number of folders, tags; point out
   profiles that include a large part of the repository
   (`--details` shows which reference pulled each folder in).
5. **Tell the user**, in their language:
   - where the file is and how many profiles it has;
   - that they can edit `.wsp/SC-PROFILES.md` themselves — fix a brief, add tags
     (team, product, domains, services), folders or their own profiles — and a
     later scan keeps these edits;
   - that tasks use a profile with `wsp ensure --task <id> --repo <repo> --profile <name>`,
     and the task-workspaces skill picks profiles by name, brief and tags;
   - that the file should be committed so teammates and task bases use it
     (until then wsp falls back to the working copy);
   - that the file doubles as a compact project index any agent can read to
     understand the structure quickly, and that they may reference it from the
     repository's `AGENTS.md` / `CLAUDE.md` (e.g. "Project map: `.wsp/SC-PROFILES.md`
     lists every subproject with a short brief, its folders and domains"). Offer
     the line, but do not edit those files unless the user asks.

A file written by version 0.2 in the repository root (`SC-PROFILES.md`) is moved to
`.wsp/` by `--write`; add `--rebuild` once to regenerate its generated parts.

Do not commit, push or open a pull request unless the user asks.
