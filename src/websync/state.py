"""SQLite-backed sync state: ID mappings, run history, conflicts, reports.

Design notes (see docs/plan/02-implementation-guide.md):
- ``id_map`` stores TWO rows per synchronized pair (one per side), each
  holding that side's native ID and the hash of its normalized fields as of
  the last successful sync. Hashes — not timestamps — drive change and
  conflict detection, so internal server clock skew cannot cause false
  conflicts.
- ``sync_runs`` has one row per (CLI run, link) execution and doubles as a
  concurrency guard: a link refuses to start while another run for the same
  link is still ``running`` (crashed runs auto-expire after 24h).
- Dry runs are recorded (``dry_run=1``) but never count as the last
  successful run and never mutate ``id_map``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS id_map (
    link_name TEXT NOT NULL,
    side      TEXT NOT NULL,
    native_id TEXT NOT NULL,
    counterpart_id TEXT NOT NULL,
    last_synced_hash TEXT NOT NULL,
    PRIMARY KEY (link_name, side, native_id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program     TEXT NOT NULL,
    config_path TEXT NOT NULL,
    link_name   TEXT NOT NULL,
    started     TEXT NOT NULL,
    finished    TEXT,
    status      TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    stats_json  TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
    run_id     INTEGER NOT NULL,
    link_name  TEXT NOT NULL,
    native_id  TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    run_id INTEGER PRIMARY KEY,
    markdown TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_link_status ON sync_runs (link_name, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_per_link
    ON sync_runs (link_name) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_conflicts_run ON conflicts (run_id);
"""

STALE_AFTER = timedelta(hours=24)


class StateError(Exception):
    """State store misuse (e.g. another run is already active)."""


def field_hash(fields: dict) -> str:
    """Stable hash of a normalized field dict (canonical JSON -> sha256)."""
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass(frozen=True)
class Pair:
    """A synchronized pair, with each side's last-synced hash."""

    source_id: str
    target_id: str
    source_hash: str
    target_hash: str


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ run guard

    def start_run(self, program: str, config_path: str, link_name: str, dry_run: bool) -> int:
        """Open a sync_runs row; refuse if this link already has an active run."""
        self._expire_stale_runs(link_name)
        row = self._conn.execute(
            "SELECT id, started FROM sync_runs WHERE link_name = ? AND status = 'running'"
            " ORDER BY id DESC LIMIT 1",
            (link_name,),
        ).fetchone()
        if row is not None:
            raise StateError(
                f"link {link_name!r} already has an active run (id={row['id']}, "
                f"started={row['started']}); refusing to start a second run"
            )
        try:
            cur = self._conn.execute(
                "INSERT INTO sync_runs (program, config_path, link_name, started, status, dry_run)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                (program, config_path, link_name, _iso(_now()), int(dry_run)),
            )
        except sqlite3.IntegrityError as exc:  # lost a race with a concurrent run
            raise StateError(f"link {link_name!r} already has an active run") from exc
        self._conn.commit()
        return int(cur.lastrowid)

    def _expire_stale_runs(self, link_name: str) -> None:
        cutoff = _iso(_now() - STALE_AFTER)
        self._conn.execute(
            "UPDATE sync_runs SET status = 'failed', finished = ?,"
            " stats_json = COALESCE(stats_json, ?)"
            " WHERE link_name = ? AND status = 'running' AND started < ?",
            (
                _iso(_now()),
                json.dumps({"errors": 1, "note": "stale run auto-expired"}),
                link_name,
                cutoff,
            ),
        )
        self._conn.commit()

    def finish_run(self, run_id: int, status: str, stats: dict | None = None) -> None:
        self._conn.execute(
            "UPDATE sync_runs SET finished = ?, status = ?, stats_json = ? WHERE id = ?",
            (_iso(_now()), status, json.dumps(stats, default=str) if stats else None, run_id),
        )
        self._conn.commit()

    def last_successful_started(self, link_name: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT started FROM sync_runs WHERE link_name = ? AND status = 'success'"
            " AND dry_run = 0 ORDER BY id DESC LIMIT 1",
            (link_name,),
        ).fetchone()
        return _from_iso(row["started"]) if row else None

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()

    # ------------------------------------------------------------ id mappings

    def get_pair(self, link_name: str, side: str, native_id: str) -> Pair | None:
        """Look up the pair containing ``native_id`` on the given side."""
        row = self._conn.execute(
            "SELECT counterpart_id, last_synced_hash FROM id_map"
            " WHERE link_name = ? AND side = ? AND native_id = ?",
            (link_name, side, native_id),
        ).fetchone()
        if row is None:
            return None
        counterpart_id = row["counterpart_id"]
        other = "source" if side == "target" else "target"
        other_row = self._conn.execute(
            "SELECT last_synced_hash FROM id_map"
            " WHERE link_name = ? AND side = ? AND native_id = ?",
            (link_name, other, counterpart_id),
        ).fetchone()
        other_hash = other_row["last_synced_hash"] if other_row else ""
        if side == "source":
            return Pair(native_id, counterpart_id, row["last_synced_hash"], other_hash)
        return Pair(counterpart_id, native_id, other_hash, row["last_synced_hash"])

    def put_pair(self, link_name: str, pair: Pair) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO id_map"
            " (link_name, side, native_id, counterpart_id, last_synced_hash)"
            " VALUES (?, 'source', ?, ?, ?), (?, 'target', ?, ?, ?)"
            " ",
            (
                link_name,
                pair.source_id,
                pair.target_id,
                pair.source_hash,
                link_name,
                pair.target_id,
                pair.source_id,
                pair.target_hash,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------ conflicts / reports

    def record_conflict(self, run_id: int, link_name: str, native_id: str, detail: dict) -> None:
        self._conn.execute(
            "INSERT INTO conflicts (run_id, link_name, native_id, detail_json) VALUES (?, ?, ?, ?)",
            (run_id, link_name, native_id, json.dumps(detail, default=str)),
        )
        self._conn.commit()

    def conflicts_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conflicts WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()

    def save_report(self, run_id: int, markdown: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO reports (run_id, markdown) VALUES (?, ?)",
            (run_id, markdown),
        )
        self._conn.commit()

    def get_report(self, run_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT markdown FROM reports WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["markdown"] if row else None
