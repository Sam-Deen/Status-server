"""SQLite schema and all application persistence operations."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import Monitor, MonitorConfig, Transition, utc_now


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL, target TEXT NOT NULL, interval INTEGER NOT NULL,
                timeout INTEGER NOT NULL, threshold INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown', response_ms INTEGER,
                last_checked TEXT, last_success TEXT, state_since TEXT,
                docker_state TEXT, docker_health TEXT, docker_state_since TEXT,
                failures INTEGER NOT NULL DEFAULT 0, next_check REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY, monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                checked_at TEXT NOT NULL, success INTEGER NOT NULL, response_ms INTEGER,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS checks_monitor_time ON checks(monitor_id, checked_at);
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY, monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL, ended_at TEXT, severity TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS incidents_time ON incidents(started_at DESC);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL
            );
            """)
            # Databases created before Docker monitoring did not have these fields.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(monitors)")}
            for column in ("docker_state", "docker_health", "docker_state_since"):
                if column not in columns:
                    db.execute(f"ALTER TABLE monitors ADD COLUMN {column} TEXT")
            # Previous Compose-generated names change when fixed container names are adopted.
            # Preserve monitor IDs, checks, and incidents; schedule a fresh state check.
            for previous, current in (("status-status-1", "status-server"),
                                      ("homelab-status-docker-proxy", "status-docker-proxy")):
                db.execute("""UPDATE monitors SET target=?,status='unknown',response_ms=NULL,failures=0,
                          next_check=0,state_since=?,docker_state=NULL,docker_health=NULL,docker_state_since=NULL
                          WHERE type='docker' AND target=?""", (current, utc_now(), previous))

    def healthy(self) -> bool:
        try:
            with self.connection() as db:
                db.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


class MonitorRepository:
    def __init__(self, database: Database):
        self.database = database

    def all(self, enabled_only: bool = False) -> list[Monitor]:
        query = "SELECT * FROM monitors " + ("WHERE enabled=1 " if enabled_only else "") + "ORDER BY name COLLATE NOCASE"
        with self.database.connection() as db:
            return [Monitor.from_record(row) for row in db.execute(query)]

    def get(self, monitor_id: int) -> Monitor | None:
        with self.database.connection() as db:
            row = db.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        return Monitor.from_record(row) if row else None

    def due(self, at: float, limit: int = 10) -> list[Monitor]:
        with self.database.connection() as db:
            rows = db.execute("SELECT * FROM monitors WHERE enabled=1 AND next_check<=? ORDER BY next_check LIMIT ?",
                              (at, limit)).fetchall()
        return [Monitor.from_record(row) for row in rows]

    def create(self, config: MonitorConfig) -> int:
        with self.database.connection() as db:
            cursor = db.execute("""INSERT INTO monitors(name,description,type,target,interval,timeout,threshold,enabled)
                                   VALUES(?,?,?,?,?,?,?,?)""", config.db_values())
            return cursor.lastrowid

    def update(self, old: Monitor, config: MonitorConfig, reset: bool) -> bool:
        neutral = reset or not config.enabled
        status = "unknown" if neutral else old.status
        failures = 0 if neutral else old.failures
        with self.database.connection() as db:
            if not config.enabled:
                db.execute("UPDATE incidents SET ended_at=? WHERE monitor_id=? AND ended_at IS NULL", (utc_now(), old.id))
            cursor = db.execute("""UPDATE monitors SET name=?,description=?,type=?,target=?,interval=?,timeout=?,threshold=?,enabled=?,
                     status=?,failures=?,response_ms=CASE WHEN ? THEN NULL ELSE response_ms END,
                     next_check=0,state_since=CASE WHEN ? THEN ? ELSE state_since END,
                     docker_state=CASE WHEN ? THEN NULL ELSE docker_state END,
                     docker_health=CASE WHEN ? THEN NULL ELSE docker_health END,
                     docker_state_since=CASE WHEN ? THEN NULL ELSE docker_state_since END WHERE id=?""",
                     (*config.db_values(), status, failures, int(neutral), int(neutral), utc_now(),
                      int(neutral), int(neutral), int(neutral), old.id))
            return cursor.rowcount > 0

    def delete(self, monitor_id: int) -> bool:
        with self.database.connection() as db:
            return db.execute("DELETE FROM monitors WHERE id=?", (monitor_id,)).rowcount > 0

    def save_check(self, monitor: Monitor, result: Transition) -> bool:
        docker = result.docker
        with self.database.connection() as db:
            cursor = db.execute("""UPDATE monitors SET status=?,response_ms=?,last_checked=?,
                       last_success=CASE WHEN ? THEN ? ELSE last_success END,
                       state_since=CASE WHEN ? THEN ? ELSE state_since END,
                       failures=?,next_check=?,docker_state=?,docker_health=?,docker_state_since=?
                       WHERE id=? AND enabled=1 AND type=? AND target=?""",
                       (result.status, result.response_ms, result.checked_at,
                        int(result.success), result.checked_at, int(result.state_changed), result.checked_at,
                        result.failures, result.next_check, docker.state if docker else None,
                        docker.health if docker else None, docker.state_since if docker else None,
                        monitor.id, monitor.type, monitor.target))
            if not cursor.rowcount:
                return False
            db.execute("INSERT INTO checks(monitor_id,checked_at,success,response_ms,status) VALUES(?,?,?,?,?)",
                       (monitor.id, result.checked_at, int(result.success), result.response_ms, result.status))
            open_incident = db.execute("SELECT id FROM incidents WHERE monitor_id=? AND ended_at IS NULL",
                                       (monitor.id,)).fetchone()
            if result.status == "operational" and open_incident:
                db.execute("UPDATE incidents SET ended_at=? WHERE id=?", (result.checked_at, open_incident["id"]))
            elif result.status in {"degraded", "down"}:
                if not open_incident:
                    db.execute("INSERT INTO incidents(monitor_id,started_at,severity) VALUES(?,?,?)",
                               (monitor.id, result.checked_at, result.status))
                elif result.status == "down":
                    db.execute("UPDATE incidents SET severity='down' WHERE id=?", (open_incident["id"],))
            return True

    def daily_counts(self, monitor_id: int, since: str) -> dict[str, tuple[int, int]]:
        with self.database.connection() as db:
            rows = db.execute("""SELECT substr(checked_at,1,10) day, sum(success) good, count(*) total
                    FROM checks WHERE monitor_id=? AND checked_at>=? AND status<>'unknown' GROUP BY day""",
                    (monitor_id, since)).fetchall()
        return {row["day"]: (row["good"], row["total"]) for row in rows}

    def history(self, monitor_id: int, limit: int = 200) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("""SELECT checked_at,success,response_ms,status FROM checks
                                WHERE monitor_id=? ORDER BY checked_at DESC LIMIT ?""", (monitor_id, limit))
            return [dict(row) for row in rows]

    def incidents(self, limit: int, public: bool = False) -> list[dict]:
        if public:
            query = """SELECT i.id,m.name monitor_name,i.started_at,i.ended_at,i.severity FROM incidents i
                       JOIN monitors m ON m.id=i.monitor_id WHERE m.enabled=1 ORDER BY i.started_at DESC LIMIT ?"""
        else:
            query = """SELECT i.*,m.name monitor_name FROM incidents i JOIN monitors m ON m.id=i.monitor_id
                       ORDER BY i.started_at DESC LIMIT ?"""
        with self.database.connection() as db:
            return [dict(row) for row in db.execute(query, (limit,))]

    def cleanup(self, retention_days: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self.database.connection() as db:
            db.execute("DELETE FROM checks WHERE checked_at<?", (cutoff,))
            db.execute("DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at<?", (cutoff,))


class SessionRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, token_hash: str, csrf: str, expires: int) -> None:
        with self.database.connection() as db:
            db.execute("INSERT INTO sessions(token_hash,csrf,expires) VALUES(?,?,?)", (token_hash, csrf, expires))

    def get(self, token_hash: str) -> dict | None:
        with self.database.connection() as db:
            row = db.execute("SELECT csrf,expires FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def delete(self, token_hash: str) -> None:
        with self.database.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def cleanup(self) -> None:
        with self.database.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
