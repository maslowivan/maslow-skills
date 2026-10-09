import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Sandbox, sh  # noqa: E402


class SandboxGitTests(Sandbox):
    def test_push_never_repacks_the_origin_in_the_background(self):
        # Git 2.54+ samples objects/17 to estimate loose objects: two files there start a geometric
        # repack after a push, racing with local clones of the origin (flaky test_sparse clone)
        canon, origin = self.canon["app"], self.origins["app"]
        sharded, i = [], 0
        while len(sharded) < 2:
            content = f"shard {i}\n"
            oid = hashlib.sha1(f"blob {len(content)}\0{content}".encode()).hexdigest()
            if oid.startswith("17"):
                self.write(os.path.join(canon, f"s{i}.txt"), content)
                sharded.append(oid)
            i += 1
        self.git(canon, "add", "-A")
        self.git(canon, "commit", "-q", "-m", "objects in shard 17")
        trace = os.path.join(self.tmp, "trace2.txt")
        sh(["git", "-C", canon, "push", "-q", "origin", "main"], env={**self.env, "GIT_TRACE2": trace})
        with open(trace) as fh:
            self.assertEqual([line for line in fh if "maintenance" in line], [])
        for oid in sharded:  # still loose: nothing packed and pruned them
            self.assertTrue(os.path.isfile(os.path.join(origin, "objects", oid[:2], oid[2:])), oid)
        self.assertEqual([f for f in os.listdir(os.path.join(origin, "objects", "pack")) if f.endswith(".pack")],
                         [])
