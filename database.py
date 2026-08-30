from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from models import Report, WorkflowError, approval_fingerprint


SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key TEXT NOT NULL UNIQUE,
    report_date TEXT NOT NULL,
    original_notes TEXT NOT NULL DEFAULT '',
    edit_instructions TEXT NOT NULL DEFAULT '',
    generated_report TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (approval_status IN ('draft', 'approved', 'cancelled')),
    sent_status INTEGER NOT NULL DEFAULT 0 CHECK (sent_status IN (0, 1)),
    send_state TEXT NOT NULL DEFAULT 'idle'
        CHECK (send_state IN ('idle', 'sending', 'failed', 'uncertain', 'sent', 'cancelled')),
    approval_hash TEXT NOT NULL DEFAULT '',
    gmail_message_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    stable_message_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('sending', 'failed', 'uncertain', 'sent')),
    gmail_message_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_report_id ON delivery_attempts(report_id, attempted_at);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _to_report(row: sqlite3.Row | None) -> Report | None:
        if row is None:
            return None
        values = dict(row)
        values["sent_status"] = bool(values["sent_status"])
        return Report(**values)

    def get_or_create_report(self, week_key: str, report_date: str, recipient: str) -> Report:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reports (week_key, report_date, recipient, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(week_key) DO NOTHING
                """,
                (week_key, report_date, recipient, now, now),
            )
            row = connection.execute(
                "SELECT * FROM reports WHERE week_key = ?", (week_key,)
            ).fetchone()
        report = self._to_report(row)
        assert report is not None
        return report

    def get_report(self, report_id: int) -> Report | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        return self._to_report(row)

    def list_reports(self, limit: int = 20) -> list[Report]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports ORDER BY report_date DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_report(row) for row in rows]  # type: ignore[misc]

    def save_generated(
        self,
        report_id: int,
        notes: str,
        body: str,
        subject: str,
        recipient: str,
        instructions: str = "",
    ) -> Report:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE reports
                SET original_notes = ?, edit_instructions = ?, generated_report = ?,
                    subject = ?, recipient = ?, approval_status = 'draft',
                    approval_hash = '', send_state = 'idle', error = '', updated_at = ?
                WHERE id = ? AND sent_status = 0
                    AND send_state NOT IN ('sending', 'uncertain')
                """,
                (notes, instructions, body, subject, recipient, _now(), report_id),
            )
            if result.rowcount != 1:
                raise WorkflowError("This report cannot be edited in its current state.")
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        report = self._to_report(row)
        assert report is not None
        return report

    def cancel_report(self, report_id: int) -> Report:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE reports
                SET approval_status = 'cancelled', approval_hash = '',
                    send_state = 'cancelled', updated_at = ?
                WHERE id = ? AND sent_status = 0 AND send_state != 'sending'
                """,
                (_now(), report_id),
            )
            if result.rowcount != 1:
                raise WorkflowError("This report cannot be cancelled in its current state.")
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        report = self._to_report(row)
        assert report is not None
        return report

    def record_error(self, report_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE reports SET error = ?, updated_at = ? WHERE id = ?",
                (error[:2000], _now(), report_id),
            )

    def approve_and_claim(self, report_id: int) -> tuple[Report, int, str]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            report = self._to_report(row)
            if report is None:
                raise WorkflowError("Report not found.")
            if report.sent_status or report.send_state == "sent":
                raise WorkflowError("A report for this week was already sent.")
            if report.send_state in {"sending", "uncertain"}:
                raise WorkflowError(
                    "Delivery is already in progress or has an uncertain outcome; another send is blocked."
                )
            if not report.generated_report or not report.recipient:
                raise WorkflowError("Generate a complete report before sending.")

            fingerprint = approval_fingerprint(report)
            now = _now()
            stable_message_id = (
                f"<weekly-report-{report.week_key}-{fingerprint[:20]}@weekly-report-agent.local>"
            )
            connection.execute(
                """
                UPDATE reports
                SET approval_status = 'approved', approval_hash = ?,
                    send_state = 'sending', error = '', updated_at = ?
                WHERE id = ?
                """,
                (fingerprint, now, report_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO delivery_attempts
                    (report_id, stable_message_id, attempted_at, outcome)
                VALUES (?, ?, ?, 'sending')
                """,
                (report_id, stable_message_id, now),
            )
            attempt_id = int(cursor.lastrowid)
            updated = connection.execute(
                "SELECT * FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
        claimed = self._to_report(updated)
        assert claimed is not None
        return claimed, attempt_id, stable_message_id

    def complete_send(self, report_id: int, attempt_id: int, gmail_message_id: str) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_attempts
                SET completed_at = ?, outcome = 'sent', gmail_message_id = ?
                WHERE id = ? AND report_id = ?
                """,
                (now, gmail_message_id, attempt_id, report_id),
            )
            connection.execute(
                """
                UPDATE reports
                SET sent_status = 1, send_state = 'sent', gmail_message_id = ?,
                    error = '', sent_at = ?, updated_at = ?
                WHERE id = ? AND send_state = 'sending'
                """,
                (gmail_message_id, now, now, report_id),
            )

    def fail_send(self, report_id: int, attempt_id: int, error: str, uncertain: bool) -> None:
        outcome = "uncertain" if uncertain else "failed"
        safe_error = error[:2000]
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_attempts
                SET completed_at = ?, outcome = ?, error = ?
                WHERE id = ? AND report_id = ?
                """,
                (now, outcome, safe_error, attempt_id, report_id),
            )
            connection.execute(
                """
                UPDATE reports
                SET send_state = ?, error = ?, updated_at = ?
                WHERE id = ? AND send_state = 'sending'
                """,
                (outcome, safe_error, now, report_id),
            )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
