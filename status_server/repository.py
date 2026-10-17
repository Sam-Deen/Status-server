"""SQLite schema and all application persistence operations."""
from __future__ import annotations

import sqlite3
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import Monitor, MonitorConfig, Transition, utc_now, INCIDENT_FAILURES
from health_contract import instant
from .incidents import merge_details


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
            if "application_health" not in columns:
                db.execute("ALTER TABLE monitors ADD COLUMN application_health TEXT")
            if "application_health_valid" not in columns:
                db.execute("ALTER TABLE monitors ADD COLUMN application_health_valid INTEGER NOT NULL DEFAULT 0")
            for column in ('last_attempt', 'last_failure', 'last_measurement'):
                if column not in columns:
                    db.execute(f'ALTER TABLE monitors ADD COLUMN {column} TEXT')
            if 'retry_attempts' not in columns:
                db.execute('ALTER TABLE monitors ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0')
                db.execute('UPDATE monitors SET failures=0,next_check=0')
            db.execute('UPDATE monitors SET threshold=? WHERE threshold<>?', (INCIDENT_FAILURES, INCIDENT_FAILURES))
            check_columns = {row["name"] for row in db.execute("PRAGMA table_info(checks)")}
            for column, kind in (("core_available", "INTEGER"), ("full_available", "INTEGER"),
                                 ("observed_at", "TEXT"), ("valid_until", "TEXT"),
                                 ("interval_seconds", "INTEGER"), ("application_health", "TEXT"),
                                 ("stale_after_seconds", "REAL"), ("reason", "TEXT"),
                                 ("monitor_type", "TEXT"), ("docker_state", "TEXT"), ("docker_health", "TEXT"),
                                 ("reported_status", "TEXT")):
                if column not in check_columns:
                    db.execute(f"ALTER TABLE checks ADD COLUMN {column} {kind}")
            incident_columns = {row["name"] for row in db.execute("PRAGMA table_info(incidents)")}
            if "ended_reason" not in incident_columns:
                db.execute("ALTER TABLE incidents ADD COLUMN ended_reason TEXT")
            if "details" not in incident_columns:
                db.execute("ALTER TABLE incidents ADD COLUMN details TEXT")
            # Previous Compose-generated names change when fixed container names are adopted.
            # Preserve monitor IDs, checks, and incidents; schedule a fresh state check.
            for previous, current in (("status-status-1", "status-server"),
                                      ("homelab-status-docker-proxy", "status-docker-proxy")):
                db.execute("""UPDATE monitors SET target=?,status='unknown',response_ms=NULL,failures=0,
                          next_check=0,retry_attempts=0,state_since=?,docker_state=NULL,docker_health=NULL,docker_state_since=NULL
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

    def latest_check(self, monitor_id: int) -> dict | None:
        with self.database.connection() as db:
            row = db.execute('''SELECT checked_at,interval_seconds,stale_after_seconds,valid_until
                                FROM checks WHERE monitor_id=? ORDER BY checked_at DESC,id DESC LIMIT 1''',
                             (monitor_id,)).fetchone()
            return dict(row) if row else None

    def update(self, old: Monitor, config: MonitorConfig, reset: bool) -> bool:
        neutral = reset or not config.enabled
        status = "unknown" if neutral else old.status
        failures = 0 if neutral else old.failures
        with self.database.connection() as db:
            if neutral:
                # Explicit configuration boundaries stop the previous observation's coverage.
                db.execute("""INSERT INTO checks(monitor_id,checked_at,success,status,interval_seconds)
                              VALUES(?,?,0,'unknown',0)""", (old.id, utc_now()))
            if not config.enabled:
                db.execute("UPDATE incidents SET ended_at=?,ended_reason='monitor disabled' WHERE monitor_id=? AND ended_at IS NULL",
                           (utc_now(), old.id))
            elif reset:
                db.execute("UPDATE incidents SET ended_at=?,ended_reason='configuration changed' WHERE monitor_id=? AND ended_at IS NULL",
                           (utc_now(), old.id))
            cursor = db.execute("""UPDATE monitors SET name=?,description=?,type=?,target=?,interval=?,timeout=?,threshold=?,enabled=?,
                     status=?,failures=?,retry_attempts=CASE WHEN ? THEN 0 ELSE retry_attempts END,
                     response_ms=CASE WHEN ? THEN NULL ELSE response_ms END,
                     next_check=0,state_since=CASE WHEN ? THEN ? ELSE state_since END,
                     docker_state=CASE WHEN ? THEN NULL ELSE docker_state END,
                     docker_health=CASE WHEN ? THEN NULL ELSE docker_health END,
                     docker_state_since=CASE WHEN ? THEN NULL ELSE docker_state_since END,
                     application_health=CASE WHEN ? THEN NULL ELSE application_health END,
                     application_health_valid=CASE WHEN ? THEN 0 ELSE application_health_valid END WHERE id=?""",
                     (*config.db_values(), status, failures, int(neutral), int(neutral), int(neutral), utc_now(),
                      int(neutral), int(neutral), int(neutral), int(neutral), int(neutral), old.id))
            return cursor.rowcount > 0

    def delete(self, monitor_id: int) -> bool:
        with self.database.connection() as db:
            return db.execute("DELETE FROM monitors WHERE id=?", (monitor_id,)).rowcount > 0

    def save_check(self, monitor: Monitor, result: Transition) -> bool:
        docker = result.docker
        detail = (json.dumps(docker.application_health, separators=(",", ":")) if docker.application_health
                  else monitor.application_health) if docker else None
        with self.database.connection() as db:
            cursor = db.execute("""UPDATE monitors SET status=?,response_ms=?,last_checked=?,
                       last_success=CASE WHEN ? THEN ? ELSE last_success END,
                       state_since=CASE WHEN ? THEN ? ELSE state_since END,
                       failures=?,retry_attempts=?,next_check=?,docker_state=?,docker_health=?,docker_state_since=?,application_health=?,
                       application_health_valid=?,last_attempt=?,last_measurement=?,
                       last_failure=CASE WHEN ? THEN ? ELSE last_failure END
                       WHERE id=? AND enabled=1 AND type=? AND target=?""",
                       (result.status, result.response_ms, result.checked_at,
                        int(result.core_available is True), result.observed_at or result.checked_at,
                        int(result.state_changed), result.checked_at,
                        result.failures, result.retry_attempts, result.next_check, docker.state if docker else None,
                        docker.health if docker else None, docker.state_since if docker else None, detail,
                        int(result.valid_until is not None), result.attempted_at or result.checked_at,
                        result.observed_at or result.checked_at, int(result.failed), result.checked_at,
                        monitor.id, monitor.type, monitor.target))
            if not cursor.rowcount:
                return False
            previous_check = db.execute("""SELECT checked_at,interval_seconds,valid_until,stale_after_seconds FROM checks
                                           WHERE monitor_id=? ORDER BY checked_at DESC,id DESC LIMIT 1""",
                                        (monitor.id,)).fetchone()
            check_id = db.execute("""INSERT INTO checks(monitor_id,checked_at,success,response_ms,status,core_available,
                          full_available,observed_at,valid_until,interval_seconds,application_health,stale_after_seconds,
                          reason,monitor_type,docker_state,docker_health,reported_status)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (monitor.id, result.checked_at, int(result.success), result.response_ms, result.observed_status or result.status,
                        result.core_available, result.full_available, result.observed_at, result.valid_until,
                        monitor.interval, detail, result.stale_after_seconds, result.reason, monitor.type,
                        docker.state if docker else None, docker.health if docker else None, result.status)).lastrowid
            open_incident = db.execute("SELECT * FROM incidents WHERE monitor_id=? AND ended_at IS NULL",
                                       (monitor.id,)).fetchone()
            boundary = instant(result.checked_at)
            if previous_check:
                boundary = min(boundary, instant(previous_check['checked_at']) + timedelta(
                    seconds=previous_check['stale_after_seconds'] or 2 * (previous_check['interval_seconds'] or monitor.interval)))
                if previous_check['valid_until']:
                    boundary = min(boundary, instant(previous_check['valid_until']))
            if open_incident and boundary < instant(result.checked_at):
                db.execute("UPDATE incidents SET ended_at=?,ended_reason='monitoring gap' WHERE id=?",
                           (boundary.isoformat(), open_incident['id']))
                open_incident = None
            if result.status == "operational" and open_incident:
                db.execute("UPDATE incidents SET ended_at=?,ended_reason='recovered' WHERE id=?", (result.checked_at, open_incident["id"]))
            elif result.status in {"degraded", "down"} and result.failures >= INCIDENT_FAILURES:
                previous = self._incident_details(db, dict(open_incident), before_check_id=check_id) if open_incident else None
                started_at = result.checked_at
                severity = result.status
                if not open_incident:
                    # Include the confirming streak, so the incident retains its
                    # original onset and all three pieces of evidence.
                    pending = db.execute('''SELECT * FROM checks WHERE monitor_id=? AND id<?
                                            ORDER BY checked_at DESC,id DESC LIMIT ?''',
                                         (monitor.id, check_id, INCIDENT_FAILURES - 1)).fetchall()
                    for row in reversed(pending):
                        if row['status'] in {'degraded', 'down'}:
                            check = dict(row)
                            check['application_health'] = json.loads(check['application_health']) if check['application_health'] else None
                            previous = merge_details(previous, check)
                            started_at = min(started_at, check['checked_at'])
                            if check['status'] == 'down':
                                severity = 'down'
                evidence = merge_details(previous, {
                    'checked_at': result.checked_at, 'status': result.status, 'response_ms': result.response_ms,
                    'reason': result.reason, 'monitor_type': monitor.type, 'valid_until': result.valid_until,
                    'application_health': json.loads(detail) if detail else None,
                    'docker_state': docker.state if docker else None, 'docker_health': docker.health if docker else None})
                encoded = json.dumps(evidence, separators=(',', ':'))
                if not open_incident:
                    db.execute("INSERT INTO incidents(monitor_id,started_at,severity,details) VALUES(?,?,?,?)",
                               (monitor.id, started_at, severity, encoded))
                else:
                    db.execute("UPDATE incidents SET severity=CASE WHEN ?='down' THEN 'down' ELSE severity END,details=? WHERE id=?",
                               (result.status, encoded, open_incident['id']))
            elif result.status == "unknown" and open_incident:
                # End the observed incident at the coverage boundary, without claiming recovery.
                db.execute("UPDATE incidents SET ended_at=?,ended_reason='monitoring gap' WHERE id=?",
                           (boundary.isoformat(), open_incident["id"]))
            return True

    def daily_counts(self, monitor_id: int, since: str) -> dict[str, tuple[int, int]]:
        with self.database.connection() as db:
            rows = db.execute("""SELECT substr(checked_at,1,10) day, sum(success) good, count(*) total
                    FROM checks WHERE monitor_id=? AND checked_at>=? AND status<>'unknown' GROUP BY day""",
                    (monitor_id, since)).fetchall()
        return {row["day"]: (row["good"], row["total"]) for row in rows}

    def history(self, monitor_id: int, limit: int = 200) -> list[dict]:
        with self.database.connection() as db:
            rows = db.execute("""SELECT checked_at,success,response_ms,status,core_available,full_available,
                                observed_at,valid_until,application_health,reason FROM checks
                                WHERE monitor_id=? ORDER BY checked_at DESC,id DESC LIMIT ?""", (monitor_id, limit))
            output = [dict(row) for row in rows]
            for row in output:
                row["application_health"] = json.loads(row["application_health"]) if row["application_health"] else None
            return output

    def latency_checks(self, monitor_id: int, start: datetime, end: datetime) -> list[dict]:
        """Read the full chart window, independently of the admin history limit."""
        with self.database.connection() as db:
            rows = db.execute("""SELECT checked_at,response_ms,application_health,valid_until,
                                        interval_seconds,stale_after_seconds,monitor_type,docker_state
                                 FROM checks WHERE monitor_id=? AND checked_at>=? AND checked_at<=?
                                 ORDER BY checked_at,id""", (monitor_id, start.isoformat(), end.isoformat()))
            return [dict(row) for row in rows]

    def availability(self, monitor_id: int, start: datetime, end: datetime) -> dict:
        from .availability import measured_availability
        with self.database.connection() as db:
            rows = db.execute("""SELECT checked_at,interval_seconds,valid_until,COALESCE(reported_status,status) status,
                                        core_available,full_available,stale_after_seconds
                                 FROM checks WHERE monitor_id=? AND checked_at>=? ORDER BY checked_at,id""",
                              (monitor_id, (start - timedelta(hours=2)).isoformat()))
            return measured_availability(rows, start, end)

    def incidents(self, limit: int, public: bool = False) -> list[dict]:
        if public:
            query = """SELECT i.id,i.monitor_id,m.name monitor_name,i.started_at,i.ended_at,i.severity,i.ended_reason,i.details FROM incidents i
                       JOIN monitors m ON m.id=i.monitor_id WHERE m.enabled=1 ORDER BY i.started_at DESC LIMIT ?"""
        else:
            query = """SELECT i.*,m.name monitor_name FROM incidents i JOIN monitors m ON m.id=i.monitor_id
                       ORDER BY i.started_at DESC LIMIT ?"""
        with self.database.connection() as db:
            output = [dict(row) for row in db.execute(query, (limit,))]
            for incident in output:
                encoded = incident['details']
                incident['details'] = self._incident_details(db, incident)
                if encoded is None:
                    # Reconstruct older incidents once from their own retained checks.
                    # Never borrow a recovered monitor's current component snapshot.
                    db.execute('UPDATE incidents SET details=? WHERE id=? AND details IS NULL',
                               (json.dumps(incident['details'], separators=(',', ':')), incident['id']))
            return output

    @staticmethod
    def _incident_details(db: sqlite3.Connection, incident: dict, before_check_id: int | None = None) -> dict:
        if incident['details']:
            return json.loads(incident['details'])
        query = """SELECT checked_at,status,response_ms,reason,monitor_type,valid_until,
                          application_health,docker_state,docker_health FROM checks
                   WHERE monitor_id=? AND checked_at>=? AND status IN ('degraded','down')"""
        args = [incident['monitor_id'], incident['started_at']]
        if incident['ended_at']:
            query += ' AND checked_at<?'
            args.append(incident['ended_at'])
        if before_check_id is not None:
            query += ' AND id<?'
            args.append(before_check_id)
        details = None
        for row in db.execute(query + ' ORDER BY checked_at,id', args):
            check = dict(row)
            check['application_health'] = json.loads(check['application_health']) if check['application_health'] else None
            details = merge_details(details, check)
        return details or merge_details(None, {'status': 'unknown'})

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
