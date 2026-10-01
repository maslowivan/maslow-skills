import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Sandbox  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "skills", "task-workspaces", "scripts"))
from wsp_core import sparse  # noqa: E402

PROFILES = os.path.join(".wsp", "SC-PROFILES.md")


class SparseSandbox(Sandbox):
    repos = ("mono",)

    def setUp(self):
        super().setUp()
        c = self.canon["mono"]
        w = self.write
        # apps/web: its own lockfile, imports shared code and a sibling package
        w(os.path.join(c, "apps/web/package.json"), json.dumps(
            {"name": "@acme/web", "dependencies": {"@acme/utils": "workspace:*"}}))
        w(os.path.join(c, "apps/web/yarn.lock"), "# lock\n")
        w(os.path.join(c, "apps/web/src/index.ts"), 'import { button } from "../../../shared/ui/button";\n')
        w(os.path.join(c, "apps/web/.env.example"), "X=\n")
        # packages/utils: a workspace package referenced by name; no routes, docs mention hosts
        w(os.path.join(c, "packages/utils/package.json"), json.dumps({"name": "@acme/utils"}))
        w(os.path.join(c, "packages/utils/index.ts"), "export const u = 1;\n")
        w(os.path.join(c, "packages/utils/README.md"),
          "Docs at https://utils.acme.dev and https://www.acme.dev; company site https://acme.dev\n")
        # shared: imports libs (followed), its test mentions services (not followed)
        w(os.path.join(c, "shared/ui/button.ts"), 'import { x } from "../../libs/core/x";\nexport const button = x;\n')
        w(os.path.join(c, "shared/ui/button.test.ts"), 'import { api } from "../../services/api/src/main";\n')
        w(os.path.join(c, "libs/core/x.ts"), "export const x = 1;\n")
        # services/api: a worker with routes in a per-environment wrangler file, and docs
        w(os.path.join(c, "services/api/package.json"), json.dumps(
            {"name": "api-worker", "description": "Public REST API for the shop"}))
        w(os.path.join(c, "services/api/package-lock.json"), "{}\n")
        w(os.path.join(c, "services/api/wrangler.production.jsonc"), "\n".join([
            '{', '  "name": "acme-api",', '  "routes": [',
            '    { "pattern": "api.acme.dev/*", "zone_name": "acme.dev" },',
            '    { "pattern": "api-staging.acme.dev", "custom_domain": true },',
            '    { "pattern": "www.api.acme.io", "custom_domain": true },',
            '    { "pattern": "api.acme.io", "custom_domain": true }',
            '    // { "pattern": "old.acme.dev", "custom_domain": true }',
            '  ],', '  "vars": { "UPSTREAM_URL": "https://upstream.vendor.io" }', '}']))
        w(os.path.join(c, "services/api/README.md"),
          "Served at https://api.acme.dev, see also https://status.acme.dev; built with https://vitejs.dev\n"
          "Code lives in lib.rs, schema.sql and Button.svelte; see docs.acme.dev\n")
        w(os.path.join(c, "services/api/src/main.ts"), "export const api = 1;\n")
        # agent tooling in the repository root: not subprojects, included in every profile
        w(os.path.join(c, ".claude/skills/review/SKILL.md"), "# review\n")
        w(os.path.join(c, ".claude/mcp/tool/package.json"), json.dumps({"name": "mcp-tool"}))
        self.git(c, "add", "-A")
        self.git(c, "commit", "-q", "-m", "monorepo")
        for i in range(2):  # services/api is the most active subproject
            w(os.path.join(c, f"services/api/src/change{i}.ts"), f"export const c{i} = {i};\n")
            self.git(c, "add", "-A")
            self.git(c, "commit", "-q", "-m", f"api change {i}")
        self.git(c, "push", "-q", "origin", "main")

    def profiles_file(self):
        return os.path.join(self.canon["mono"], PROFILES)


class ScanTests(SparseSandbox):
    def test_scan_detects_subprojects_folders_and_domains(self):
        res = self.wsp("sparse", "scan", self.canon["mono"])
        profiles = res["profiles"]
        self.assertEqual(list(profiles), ["api", "web", "utils"])  # most recent commits first, then by path
        web = profiles["web"]
        self.assertEqual(web["folders"][0], "apps/web")
        self.assertIn("shared", web["folders"])          # ../../../shared/ui/button -> whole shared
        self.assertIn("libs", web["folders"])            # followed from shared; included whole
        self.assertIn("packages/utils", web["folders"])  # workspace dependency by package name
        self.assertIn(".claude", web["folders"])         # agent tooling goes into every profile
        self.assertNotIn("services/api", web["folders"])  # only a test in shared mentions it
        self.assertNotIn("tool", profiles)               # .claude/mcp/tool is not a subproject
        api = profiles["api"]
        self.assertEqual(api["about"], "Public REST API for the shop")
        # not: the profile name, the zone, staging variant, www duplicate, commented route,
        # vars, doc hosts (api has routes), library links or file names
        self.assertEqual(api["tags"], ["api-worker", "acme-api", "api.acme.dev", "api.acme.io"])
        utils = profiles["utils"]
        self.assertIn("utils.acme.dev", utils["tags"])   # no routes -> specific doc hosts only
        self.assertNotIn("acme.dev", utils["tags"])
        self.assertNotIn("www.acme.dev", utils["tags"])
        self.assertFalse(os.path.exists(self.profiles_file()))  # preview only

    def test_file_lives_in_wsp_folder_and_legacy_root_file_moves(self):
        legacy = os.path.join(self.canon["mono"], "SC-PROFILES.md")
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        self.assertTrue(os.path.isfile(self.profiles_file()))
        os.replace(self.profiles_file(), legacy)          # a file written by an older version
        self.assertIn("api", sparse.load(self.canon["mono"]))  # still read from the root
        res = self.wsp("sparse", "scan", self.canon["mono"], "--write")
        self.assertEqual(res["moved_from"], "SC-PROFILES.md")
        self.assertTrue(os.path.isfile(self.profiles_file()))
        self.assertFalse(os.path.exists(legacy))

    def test_rescan_keeps_user_edits_and_drops_stale_generated_items(self):
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        with open(self.profiles_file()) as fh:
            text = fh.read()
        self.assertIn("<!-- wsp:auto", text)
        text = text.replace("- tags: api-worker, acme-api,", "- tags: API Worker, acme-api, payments-team,")
        text = text.replace("- about: Public REST API for the shop", "- about: Checkout and payments API")
        text += "\n## docs-only\n\n- folders: `docs`\n- tags: documentation\n"
        with open(self.profiles_file(), "w") as fh:
            fh.write(text)
        wrangler = os.path.join(self.canon["mono"], "services/api/wrangler.production.jsonc")
        with open(wrangler) as fh:
            config = fh.read()
        with open(wrangler, "w") as fh:  # the route to api.acme.io is replaced
            fh.write(config.replace("api.acme.io", "api.acme.net"))
        res = self.wsp("sparse", "scan", self.canon["mono"], "--write")
        api = res["profiles"]["api"]
        self.assertIn("payments-team", api["tags"])          # user tag kept
        self.assertIn("API Worker", api["tags"])             # user spelling kept
        self.assertNotIn("api-worker", api["tags"])
        self.assertIn("api.acme.net", api["tags"])           # new route
        self.assertNotIn("api.acme.io", api["tags"])         # stale generated tag dropped
        self.assertEqual(api["about"], "Checkout and payments API")  # user about wins
        self.assertIn("docs-only", res["profiles"])
        self.assertEqual(res["report"]["kept_manual"], ["docs-only"])
        self.assertFalse(self.wsp("sparse", "scan", self.canon["mono"])["changed"])  # stable

    def test_rebuild_regenerates_files_without_markers(self):
        os.makedirs(os.path.dirname(self.profiles_file()), exist_ok=True)
        with open(self.profiles_file(), "w") as fh:
            fh.write("## api\n\n- path: `services/api`\n- folders: `services/api`\n- tags: old.acme.dev\n")
        kept = self.wsp("sparse", "scan", self.canon["mono"])
        self.assertIn("old.acme.dev", kept["profiles"]["api"]["tags"])   # unknown origin: treated as manual
        rebuilt = self.wsp("sparse", "scan", self.canon["mono"], "--rebuild")
        self.assertNotIn("old.acme.dev", rebuilt["profiles"]["api"]["tags"])

    def test_list_match_and_consumers(self):
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        listing = self.wsp("sparse", "list", "--repo", "mono")
        self.assertIn("web", listing["profiles"])
        match = self.wsp("sparse", "match", "api.acme.dev", "--repo", "mono")
        self.assertEqual(match["matches"][0]["name"], "api")
        match = self.wsp("sparse", "match", "hub", "--repo", "mono")   # no substring hits (e.g. in .github)
        self.assertEqual(match["matches"], [])
        consumers = self.wsp("sparse", "consumers", "--repo", "mono", "--of", "shared/ui")
        self.assertEqual(consumers["profiles"], ["web"])

    def test_queries_by_repo_fetch_the_default_branch_first(self):
        # profiles land on origin from another clone; the canonical checkout has not fetched them
        other = os.path.join(self.tmp, "other-mono")
        self.git(self.tmp, "clone", "-q", self.origins["mono"], other)
        self.wsp("sparse", "scan", other, "--write")
        self.git(other, "add", ".wsp")
        self.git(other, "commit", "-q", "-m", "profiles")
        self.git(other, "push", "-q", "origin", "main")
        canon_head = self.git(self.canon["mono"], "rev-parse", "HEAD").stdout
        err = self.wsp("sparse", "match", "api", "--repo", "mono", "--no-fetch", ok=False)
        self.assertEqual(err["error"]["code"], "SPARSE_PROFILE_UNKNOWN")
        match = self.wsp("sparse", "match", "api", "--repo", "mono")
        self.assertEqual(match["matches"][0]["name"], "api")
        self.assertTrue(match["base"]["fetched"])
        self.assertEqual(match["base"]["sha"], self.git(other, "rev-parse", "HEAD").stdout.strip())
        # only the remote-tracking ref moved: the canonical checkout is untouched
        self.assertEqual(self.git(self.canon["mono"], "rev-parse", "HEAD").stdout, canon_head)
        self.assertFalse(os.path.exists(self.profiles_file()))


class TagTests(unittest.TestCase):
    def test_dedupe_prefers_readable_and_drops_profile_name(self):
        self.assertEqual(sparse.dedupe_tags(["ugc-indexer", "UGC Indexer", "ugc.example.com"], drop=["api"]),
                         ["UGC Indexer", "ugc.example.com"])
        self.assertEqual(sparse.dedupe_tags(["api", "API", "api-worker"], drop=["api"]), ["api-worker"])

    def test_prefer_public_hosts(self):
        self.assertEqual(sparse._prefer_public(["www.shop.com", "shop.com", "shop-dev.acme.org", "ridestore.dev",
                                                "x.cloudfront.net", "shop.com.pre-release.acme.org"]),
                         ["shop.com", "ridestore.dev"])
        self.assertEqual(sparse._prefer_public(["api-staging.acme.dev"]), ["api-staging.acme.dev"])


class SparseTreeTests(SparseSandbox):
    def setUp(self):
        super().setUp()
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        self.git(self.canon["mono"], "add", ".wsp")
        self.git(self.canon["mono"], "commit", "-q", "-m", "profiles")
        self.git(self.canon["mono"], "push", "-q", "origin", "main")

    def files(self, path):
        out = set()
        for base, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for n in names:
                out.add(os.path.relpath(os.path.join(base, n), path))
        return out

    def test_profile_tree_contains_only_its_folders_and_roundtrips(self):
        res = self.ensure("sp", "mono", extra=("--profile", "web"))
        tree = res["trees"][0]
        self.assertEqual(tree["sparse"]["profiles"], ["web"])
        path = tree["path"]
        files = self.files(path)
        self.assertIn("apps/web/src/index.ts", files)
        self.assertIn("shared/ui/button.ts", files)
        self.assertIn("libs/core/x.ts", files)
        self.assertIn("README.md", files)                 # root files are always present
        self.assertIn(PROFILES, files)                    # .wsp is always present
        self.assertIn(".claude/skills/review/SKILL.md", files)
        self.assertNotIn("services/api/src/main.ts", files)
        self.assertEqual(self.git(path, "status", "--porcelain").stdout, "")
        # checkpoint -> evict -> restore keeps the sparse cone and the work
        self.write(os.path.join(path, "apps/web/src/index.ts"), "changed\n")
        self.write(os.path.join(path, "shared/new.ts"), "new\n")
        before = self.git(path, "status", "--porcelain").stdout
        self.wsp("evict", "--task", "sp", "--holder", "cli:test")
        restored = self.ensure("sp", "mono")
        self.assertEqual(restored["trees"][0]["action"], "restored")
        self.assertEqual(self.git(path, "status", "--porcelain").stdout, before)
        self.assertNotIn("services/api/src/main.ts", self.files(path))
        # widen the tree when the task needs more
        added = self.wsp("sparse", "add", "--task", "sp", "--repo", "mono", "--profile", "api")
        self.assertIn("services/api", added["added"])
        self.assertIn("services/api/src/main.ts", self.files(path))

    def test_full_checkout_warns_when_profiles_exist(self):
        res = self.ensure("web-fix", "mono")
        tree = res["trees"][0]
        self.assertIsNone(tree.get("sparse"))
        self.assertEqual(tree["sparse_available"]["suggested"][0], "web")
        self.assertGreaterEqual(tree["sparse_available"]["profiles"], 3)
        self.assertTrue(any("--profile mono:<name>" in w for w in res["warnings"]))
        sparse_tree = self.ensure("web-sparse", "mono", extra=("--profile", "web"))["trees"][0]
        self.assertNotIn("sparse_available", sparse_tree)

    def test_unknown_profile_suggests_names(self):
        err = self.wsp("ensure", "--task", "bad", "--repo", "mono", "--holder", "cli:test",
                       "--profile", "wbe", ok=False)
        self.assertEqual(err["error"]["code"], "SPARSE_PROFILE_UNKNOWN")
        self.assertIn("web", err["error"]["details"]["known"])
        self.assertFalse(os.path.exists(self.tree_path("bad", "mono")))

    def test_secrets_outside_the_cone_are_not_created(self):
        repos = self.cfg["repos"]
        repos["mono"]["profile"]["secrets"] = [{"path": "services/api/.dev.vars", "source": "canonical"},
                                               {"path": ".dev.vars", "source": "canonical"}]
        self.write_config(repos=repos)
        self.write(os.path.join(self.canon["mono"], "services/api/.dev.vars"), "S=1\n")
        path = self.ensure("sec", "mono", extra=("--profile", "web"))["trees"][0]["path"]
        self.assertTrue(os.path.exists(os.path.join(path, ".dev.vars")))
        self.assertFalse(os.path.exists(os.path.join(path, "services")))


if __name__ == "__main__":
    unittest.main()
