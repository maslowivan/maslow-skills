# Dependencies

Dependencies are prepared per **package directory** (the nearest directory with
a lockfile), not per repository: a monorepo with a lockfile per service installs
only what the task needs.

## Fingerprint

sha256 over: install recipe, lockfile, every tracked `package.json`,
`.yarnrc.yml`/`.yarnrc`/`.npmrc`/`.pnpmfile.cjs`/`pnpm-workspace.yaml`/`.nvmrc`
in the package dir and its ancestors, patches (`patches/`, `.yarn/patches/`),
`node -v`, OS and CPU architecture. `node_modules` found on disk without a
matching fingerprint is never considered compatible.

## Order

1. A cache instance with the same fingerprint → **clone** it into the tree with
   APFS clonefile (`cp -c`) or reflink. The copy is copy-on-write: nearly no
   space, and writes (Vite/Vitest caches, patches) stay in that tree.
2. Otherwise run the recipe in the tree (`yarn install --immutable`,
   `yarn install --frozen-lockfile`, `pnpm install --frozen-lockfile`,
   `npm ci`, or the profile's `install_command`), then the optional
   `verify_command`. If the result is relocatable, clone it into the cache.
3. `node_modules` is never symlinked, never hard-linked and never taken from the
   canonical checkout.

**Relocatable** means no symlink inside `node_modules` is absolute into the tree
or leaves the tree. Relative workspace links (`node_modules/x -> ../packages/x`)
are fine: after cloning they point at the current task's code. Instances that
fail this check are recorded as `unclonable` and each tree installs itself.

Without copy-on-write support (e.g. ext4) every tree installs with the package
manager's own cache. Zero reinstallation is never promised.

## Freshness

`wsp deps --check` and `wsp run` compare the recorded fingerprint with the
current files. Any change → `DEPS_INCOMPATIBLE` until `wsp deps` runs again.

## Cache cleanup

When adding an instance would exceed `limits.dependency_cache_gib`, unused
instances are removed first; if the cache is still full the tree keeps its own
install and the result says `cache_skip_reason`.

An instance is used while a tree that is not evicted references it. Unused
instances are removed by `wsp gc --apply`, except the newest instance per
package (kept to avoid reinstalls) unless the cache sub-quota is exceeded.
