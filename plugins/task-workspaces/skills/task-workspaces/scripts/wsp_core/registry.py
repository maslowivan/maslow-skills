"""SQLite registry of tasks, sessions, trees, leases, checkpoints and operations.

SQLite serialises record changes; long Git/build operations never run inside a
registry transaction. Portable per-tree manifests (see manifests.py) allow the
registry to be rebuilt if the database is lost.
"""

import contextlib
import json
import os
import sqlite3
import uuid

from . import locks, util

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    tracker_ref TEXT,
    status TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    app TEXT, session_id TEXT, holder TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'owner',
    attached_at TEXT, last_seen TEXT,
    UNIQUE(task_id, holder)
);
CREATE TABLE IF NOT EXISTS trees (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    source_path TEXT, origin TEXT,
    path TEXT NOT NULL,
    branch TEXT, base_sha TEXT, base_fresh INTEGER,
    head_sha TEXT, remote_covered_sha TEXT,
    admin_id TEXT,
    generation INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL,
    deps_state TEXT,
    size_bytes INTEGER, size_measured_at TEXT,
    created_at TEXT, updated_at TEXT,
    UNIQUE(task_id, repo)
);
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY,
    tree_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    state TEXT NOT NULL,
    pid INTEGER, pid_start TEXT, pgid INTEGER,
    acquired_at TEXT, heartbeat_at TEXT, released_at TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY,
    tree_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ref TEXT NOT NULL,
    branch TEXT, head_sha TEXT,
    index_tree TEXT, work_tree TEXT, work_commit TEXT,
    bundle_path TEXT, bundle_sha256 TEXT,
    ignored_archive TEXT, ignored_sha256 TEXT,
    manifest_path TEXT,
    secrets_json TEXT,
    reason TEXT,
    state TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL, task_id TEXT,
    stage TEXT, status TEXT NOT NULL,
    pid INTEGER, pid_start TEXT,
    started_at TEXT, updated_at TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    operation_id INTEGER, path TEXT, bytes INTEGER,
    created_at TEXT, released_at TEXT
);
CREATE TABLE IF NOT EXISTS dep_instances (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL, package_dir TEXT NOT NULL, fingerprint TEXT NOT NULL,
    path TEXT NOT NULL, state TEXT NOT NULL,
    manifest_json TEXT, size_bytes INTEGER,
    created_at TEXT, last_used_at TEXT,
    UNIQUE(repo, package_dir, fingerprint)
);
CREATE TABLE IF NOT EXISTS dep_uses (
    tree_id TEXT NOT NULL, package_dir TEXT NOT NULL,
    instance_id INTEGER, fingerprint TEXT, mode TEXT, created_at TEXT,
    PRIMARY KEY(tree_id, package_dir)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT, task_id TEXT, tree_id TEXT, kind TEXT, detail_json TEXT
);
"""

TREE_STATES = (
    "provisioning", "ready", "snapshotting", "evicting", "evicted",
    "restoring", "missing", "unknown", "recovery_failed", "closed",
)
TASK_STATES = ("open", "in_progress", "paused", "completed", "cancelled")


class Registry:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        # Switching journal mode and migrating need exclusive access; SQLite does not
        # apply the busy handler to that, so concurrent first opens serialise on a file lock.
        with locks.locked(os.path.dirname(path), "registry:init"):
            if self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.conn.close()

    def _migrate(self):
        with self.tx():
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"registry schema {row['value']} is newer than this wsp ({SCHEMA_VERSION})")

    @contextlib.contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # --- events ---------------------------------------------------------------
    def event(self, kind, task_id=None, tree_id=None, **detail):
        self.conn.execute(
            "INSERT INTO events(ts, task_id, tree_id, kind, detail_json) VALUES(?,?,?,?,?)",
            (util.now_iso(), task_id, tree_id, kind, json.dumps(detail, default=str)),
        )

    # --- tasks ------------------------------------------------------------------
    def get_task(self, task_id):
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self):
        return self._all("SELECT * FROM tasks ORDER BY created_at")

    def upsert_task(self, task_id, title=None, tracker_ref=None, status=None):
        now = util.now_iso()
        with self.tx():
            existing = self.get_task(task_id)
            if existing is None:
                self.conn.execute(
                    "INSERT INTO tasks(id, title, tracker_ref, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (task_id, title, tracker_ref, status or "in_progress", now, now),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET title=COALESCE(?, title), tracker_ref=COALESCE(?, tracker_ref),"
                    " status=COALESCE(?, status), updated_at=?,"
                    " closed_at=CASE WHEN ? IS NOT NULL AND ? NOT IN ('completed','cancelled') THEN NULL ELSE closed_at END"
                    " WHERE id=?",
                    (title, tracker_ref, status, now, status, status, task_id),
                )
        return self.get_task(task_id)

    def set_task_status(self, task_id, status):
        now = util.now_iso()
        closed = now if status in ("completed", "cancelled") else None
        with self.tx():
            self.conn.execute("UPDATE tasks SET status=?, updated_at=?, closed_at=? WHERE id=?", (status, now, closed, task_id))

    def set_pinned(self, task_id, pinned):
        with self.tx():
            self.conn.execute("UPDATE tasks SET pinned=?, updated_at=? WHERE id=?", (1 if pinned else 0, util.now_iso(), task_id))

    # --- sessions ---------------------------------------------------------------
    def attach_session(self, task_id, holder, app=None, session_id=None, confirmed=False, role="owner"):
        now = util.now_iso()
        with self.tx():
            self.conn.execute(
                "INSERT INTO sessions(task_id, app, session_id, holder, confirmed, role, attached_at, last_seen)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id, holder) DO UPDATE SET last_seen=excluded.last_seen,"
                " confirmed=MAX(confirmed, excluded.confirmed), role=excluded.role",
                (task_id, app, session_id, holder, 1 if confirmed else 0, role, now, now),
            )

    def sessions_for_task(self, task_id):
        return self._all("SELECT * FROM sessions WHERE task_id=? ORDER BY attached_at", (task_id,))

    def sessions_for_holder(self, holder):
        return self._all("SELECT * FROM sessions WHERE holder=?", (holder,))

    # --- trees ------------------------------------------------------------------
    def get_tree(self, tree_id):
        return self._one("SELECT * FROM trees WHERE id=?", (tree_id,))

    def find_tree(self, task_id, repo):
        return self._one("SELECT * FROM trees WHERE task_id=? AND repo=?", (task_id, repo))

    def find_tree_by_path(self, path):
        real = os.path.realpath(path)
        for tree in self._all("SELECT * FROM trees WHERE state != 'closed'"):
            tpath = os.path.realpath(tree["path"])
            if real == tpath or real.startswith(tpath + os.sep):
                return tree
        return None

    def trees_for_task(self, task_id, include_closed=False):
        sql = "SELECT * FROM trees WHERE task_id=?"
        if not include_closed:
            sql += " AND state != 'closed'"
        return self._all(sql + " ORDER BY repo", (task_id,))

    def all_trees(self, include_closed=False):
        sql = "SELECT * FROM trees"
        if not include_closed:
            sql += " WHERE state != 'closed'"
        return self._all(sql + " ORDER BY task_id, repo")

    def insert_tree(self, **fields):
        now = util.now_iso()
        fields.setdefault("id", uuid.uuid4().hex[:12])
        fields.setdefault("created_at", now)
        fields["updated_at"] = now
        cols = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        with self.tx():
            self.conn.execute(f"INSERT INTO trees({cols}) VALUES({marks})", tuple(fields.values()))
        return self.get_tree(fields["id"])

    def update_tree(self, tree_id, **fields):
        fields["updated_at"] = util.now_iso()
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx():
            self.conn.execute(f"UPDATE trees SET {sets} WHERE id=?", (*fields.values(), tree_id))
        return self.get_tree(tree_id)

    # --- leases -----------------------------------------------------------------
    def active_leases(self, tree_id):
        return self._all("SELECT * FROM leases WHERE tree_id=? AND state IN ('held','uncertain')", (tree_id,))

    def leases_for_holder(self, holder):
        return self._all("SELECT * FROM leases WHERE holder=? AND state IN ('held','uncertain')", (holder,))

    def acquire_lease(self, tree_id, holder, pid=None, pid_start=None, pgid=None):
        now = util.now_iso()
        with self.tx():
            held = self.conn.execute(
                "SELECT * FROM leases WHERE tree_id=? AND state IN ('held','uncertain')", (tree_id,)
            ).fetchall()
            for lease in held:
                if lease["holder"] != holder:
                    return dict(lease), False
            mine = [dict(l) for l in held if l["holder"] == holder]
            if mine:
                self.conn.execute(
                    "UPDATE leases SET heartbeat_at=?, state='held', pid=COALESCE(?, pid), pid_start=COALESCE(?, pid_start),"
                    " pgid=COALESCE(?, pgid) WHERE id=?",
                    (now, pid, pid_start, pgid, mine[0]["id"]),
                )
                return self._one("SELECT * FROM leases WHERE id=?", (mine[0]["id"],)), True
            cur = self.conn.execute(
                "INSERT INTO leases(tree_id, holder, state, pid, pid_start, pgid, acquired_at, heartbeat_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (tree_id, holder, "held", pid, pid_start, pgid, now, now),
            )
            return self._one("SELECT * FROM leases WHERE id=?", (cur.lastrowid,)), True

    def heartbeat(self, lease_id):
        with self.tx():
            self.conn.execute("UPDATE leases SET heartbeat_at=? WHERE id=?", (util.now_iso(), lease_id))

    def set_lease_process(self, lease_id, pid, pid_start, pgid):
        with self.tx():
            self.conn.execute("UPDATE leases SET pid=?, pid_start=?, pgid=? WHERE id=?", (pid, pid_start, pgid, lease_id))

    def release_lease(self, lease_id, state="released"):
        released_at = util.now_iso() if state == "released" else None
        with self.tx():
            self.conn.execute(
                "UPDATE leases SET state=?, released_at=? WHERE id=?", (state, released_at, lease_id)
            )

    # --- checkpoints --------------------------------------------------------------
    def next_checkpoint_seq(self, tree_id):
        row = self.conn.execute("SELECT MAX(seq) AS m FROM checkpoints WHERE tree_id=?", (tree_id,)).fetchone()
        return (row["m"] or 0) + 1

    def insert_checkpoint(self, **fields):
        fields.setdefault("created_at", util.now_iso())
        cols = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        with self.tx():
            cur = self.conn.execute(f"INSERT INTO checkpoints({cols}) VALUES({marks})", tuple(fields.values()))
        return self._one("SELECT * FROM checkpoints WHERE id=?", (cur.lastrowid,))

    def valid_checkpoints(self, tree_id):
        return self._all("SELECT * FROM checkpoints WHERE tree_id=? AND state='valid' ORDER BY seq", (tree_id,))

    def latest_checkpoint(self, tree_id):
        return self._one(
            "SELECT * FROM checkpoints WHERE tree_id=? AND state='valid' ORDER BY seq DESC LIMIT 1", (tree_id,)
        )

    def mark_checkpoint(self, checkpoint_id, state):
        with self.tx():
            self.conn.execute("UPDATE checkpoints SET state=? WHERE id=?", (state, checkpoint_id))

    # --- operations -----------------------------------------------------------------
    def begin_operation(self, kind, task_id, pid, pid_start, **detail):
        now = util.now_iso()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO operations(kind, task_id, stage, status, pid, pid_start, started_at, updated_at, detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, task_id, "start", "running", pid, pid_start, now, now, json.dumps(detail, default=str)),
            )
        return cur.lastrowid

    def operation_stage(self, op_id, stage, **detail):
        row = self._one("SELECT detail_json FROM operations WHERE id=?", (op_id,))
        merged = json.loads(row["detail_json"] or "{}") if row else {}
        merged.update(detail)
        with self.tx():
            self.conn.execute(
                "UPDATE operations SET stage=?, updated_at=?, detail_json=? WHERE id=?",
                (stage, util.now_iso(), json.dumps(merged, default=str), op_id),
            )

    def finish_operation(self, op_id, status, **detail):
        self.operation_stage(op_id, "end", **detail)
        with self.tx():
            self.conn.execute("UPDATE operations SET status=?, updated_at=? WHERE id=?", (status, util.now_iso(), op_id))
            self.conn.execute(
                "UPDATE reservations SET released_at=? WHERE operation_id=? AND released_at IS NULL",
                (util.now_iso(), op_id),
            )

    def running_operations(self):
        return self._all("SELECT * FROM operations WHERE status='running' ORDER BY id")

    # --- reservations ------------------------------------------------------------------
    def reserve(self, op_id, path, nbytes):
        with self.tx():
            self.conn.execute(
                "INSERT INTO reservations(operation_id, path, bytes, created_at) VALUES(?,?,?,?)",
                (op_id, path, int(nbytes), util.now_iso()),
            )

    def active_reservations(self):
        return self._all(
            "SELECT r.* FROM reservations r JOIN operations o ON o.id = r.operation_id"
            " WHERE r.released_at IS NULL AND o.status='running'"
        )

    # --- dependencies --------------------------------------------------------------------
    def get_dep_instance(self, repo, package_dir, fingerprint):
        return self._one(
            "SELECT * FROM dep_instances WHERE repo=? AND package_dir=? AND fingerprint=?",
            (repo, package_dir, fingerprint),
        )

    def upsert_dep_instance(self, repo, package_dir, fingerprint, path, state, manifest, size_bytes):
        now = util.now_iso()
        with self.tx():
            self.conn.execute(
                "INSERT INTO dep_instances(repo, package_dir, fingerprint, path, state, manifest_json, size_bytes, created_at, last_used_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(repo, package_dir, fingerprint) DO UPDATE SET path=excluded.path, state=excluded.state,"
                " manifest_json=excluded.manifest_json, size_bytes=excluded.size_bytes, last_used_at=excluded.last_used_at",
                (repo, package_dir, fingerprint, path, state, json.dumps(manifest), size_bytes, now, now),
            )
        return self.get_dep_instance(repo, package_dir, fingerprint)

    def dep_instances(self):
        return self._all("SELECT * FROM dep_instances ORDER BY repo, package_dir, last_used_at")

    def delete_dep_instance(self, instance_id):
        with self.tx():
            self.conn.execute("DELETE FROM dep_instances WHERE id=?", (instance_id,))

    def touch_dep_instance(self, instance_id):
        with self.tx():
            self.conn.execute("UPDATE dep_instances SET last_used_at=? WHERE id=?", (util.now_iso(), instance_id))

    def set_dep_use(self, tree_id, package_dir, instance_id, fingerprint, mode):
        with self.tx():
            self.conn.execute(
                "INSERT INTO dep_uses(tree_id, package_dir, instance_id, fingerprint, mode, created_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(tree_id, package_dir) DO UPDATE SET instance_id=excluded.instance_id,"
                " fingerprint=excluded.fingerprint, mode=excluded.mode, created_at=excluded.created_at",
                (tree_id, package_dir, instance_id, fingerprint, mode, util.now_iso()),
            )

    def dep_uses_for_tree(self, tree_id):
        return self._all("SELECT * FROM dep_uses WHERE tree_id=?", (tree_id,))

    def dep_use_count(self, instance_id):
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM dep_uses WHERE instance_id=? AND mode != 'evicted'", (instance_id,)
        ).fetchone()
        return row["n"]

    def mark_dep_uses_evicted(self, tree_id):
        """Keep which packages the tree needs (for restore) without pinning cache instances."""
        with self.tx():
            self.conn.execute("UPDATE dep_uses SET mode='evicted' WHERE tree_id=?", (tree_id,))

    def clear_dep_uses(self, tree_id):
        with self.tx():
            self.conn.execute("DELETE FROM dep_uses WHERE tree_id=?", (tree_id,))
