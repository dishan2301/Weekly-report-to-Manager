from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


class WorkflowError(RuntimeError):
    """Raised when a requested report transition is unsafe or invalid."""


@dataclass(slots=True)
class Report:
    id: int
    week_key: str
    report_date: str
    original_notes: str
    edit_instructions: str
    generated_report: str
    subject: str
    recipient: str
    approval_status: str
    sent_status: bool
    send_state: str
    approval_hash: str
    gmail_message_id: str
    error: str
    created_at: str
    updated_at: str
    sent_at: str | None


def approval_fingerprint(report: Report) -> str:
    artifact = {
        "version": 1,
        "week_key": report.week_key,
        "recipient": report.recipient.strip().lower(),
        "subject": report.subject,
        "body": report.generated_report,
    }
    canonical = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
