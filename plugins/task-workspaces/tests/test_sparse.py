import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT, Sandbox  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "skills", "task-workspaces", "scripts"))
from wsp_core import sparse  # noqa: E402


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
        # packages/utils: a workspace package referenced by name
        w(os.path.join(c, "packages/utils/package.json"), json.dumps({"name": "@acme/utils"}))
        w(os.path.join(c, "packages/utils/index.ts"), "export const u = 1;\n")
        # shared: imports libs (followed), its test mentions services (not followed)
        w(os.path.join(c, "shared/ui/button.ts"), 'import { x } from "../../libs/core/x";\nexport const button = x;\n')
        w(os.path.join(c, "shared/ui/button.test.ts"), 'import { api } from "../../services/api/src/main";\n')
        w(os.path.join(c, "libs/core/x.ts"), "export const x = 1;\n")
        # services/api: a worker with routes and docs
        w(os.path.join(c, "services/api/package.json"), json.dumps({"name": "api-worker"}))
        w(os.path.join(c, "services/api/package-lock.json"), "{}\n")
        w(os.path.join(c, "services/api/wrangler.jsonc"), json.dumps({
            "name": "acme-api", "routes": [{"pattern": "api.acme.dev/*", "custom_domain": True}],
            "vars": {"UPSTREAM_URL": "https://upstream.vendor.io"}}, indent=2))
        w(os.path.join(c, "services/api/README.md"),
          "Served at https://api.acme.dev and https://status.acme.dev; built with https://vitejs.dev\n"
          "Code lives in lib.rs, schema.sql and Button.svelte; see docs.acme.dev\n")
        w(os.path.join(c, "services/api/src/main.ts"), "export const api = 1;\n")
        self.git(c, "add", "-A")
        self.git(c, "commit", "-q", "-m", "monorepo")
        self.git(c, "push", "-q", "origin", "main")


class ScanTests(SparseSandbox):
    def test_scan_detects_subprojects_folders_and_domains(self):
        res = self.wsp("sparse", "scan", self.canon["mono"])
        profiles = res["profiles"]
        self.assertEqual(sorted(profiles), ["api", "utils", "web"])
        web = profiles["web"]
        self.assertEqual(web["folders"][0], "apps/web")
        self.assertIn("shared", web["folders"])          # ../../../shared/ui/button -> whole shared
        self.assertIn("libs", web["folders"])            # followed from shared; included whole
        self.assertIn("packages/utils", web["folders"])  # workspace dependency by package name
        self.assertNotIn("services/api", web["folders"])  # only a test in shared mentions it
        api = profiles["api"]
        self.assertIn("api.acme.dev", api["tags"])
        self.assertIn("status.acme.dev", api["tags"])     # README host of the repo's own domain
        self.assertIn("acme-api", api["tags"])            # worker name
        self.assertNotIn("upstream.vendor.io", api["tags"])  # vars are not where the service is served
        self.assertNotIn("vitejs.dev", api["tags"])
        for filename in ("lib.rs", "schema.sql", "button.svelte"):   # file names are not hosts
            self.assertNotIn(filename, api["tags"])
        self.assertIn("docs.acme.dev", api["tags"])      # bare host of the repo's own domain
        self.assertFalse(os.path.exists(os.path.join(self.canon["mono"], "SC-PROFILES.md")))  # preview only

    def test_rescan_keeps_user_edits(self):
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        path = os.path.join(self.canon["mono"], "SC-PROFILES.md")
        with open(path) as fh:
            text = fh.read()
        text = text.replace("- tags: api,", "- tags: api, payments-team,")
        text += "\n## docs-only\n\n- folders: `docs`\n- tags: documentation\n"
        with open(path, "w") as fh:
            fh.write(text)
        res = self.wsp("sparse", "scan", self.canon["mono"], "--write")
        self.assertIn("payments-team", res["profiles"]["api"]["tags"])
        self.assertIn("docs-only", res["profiles"])
        self.assertEqual(res["report"]["kept_manual"], ["docs-only"])
        parsed = sparse.load(self.canon["mono"])
        self.assertEqual(parsed["docs-only"]["folders"], ["docs"])

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


class SparseTreeTests(SparseSandbox):
    def setUp(self):
        super().setUp()
        self.wsp("sparse", "scan", self.canon["mono"], "--write")
        self.git(self.canon["mono"], "add", "SC-PROFILES.md")
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
