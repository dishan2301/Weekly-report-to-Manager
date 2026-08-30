import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httplib2
from googleapiclient.errors import HttpError

from config import Settings
from database import Database
from email_service import EmailService, GmailSendError
from models import WorkflowError, approval_fingerprint
from report_agent import ReportAgent
from scheduler import reconcile_due_report, week_key


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "reports.db")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_approval_is_bound_to_exact_content_and_duplicate_claim_is_blocked(self):
        report = self.database.get_or_create_report(
            "2026-W35", "2026-08-27", "manager@example.com"
        )
        report = self.database.save_generated(
            report.id,
            "- Fixed responsive issues",
            "I fixed responsive issues.",
            "Weekly Report - 2026-W35",
            "manager@example.com",
        )
        expected = approval_fingerprint(report)
        claimed, _, _ = self.database.approve_and_claim(report.id)
        self.assertEqual(claimed.approval_hash, expected)
        with self.assertRaises(WorkflowError):
            self.database.approve_and_claim(report.id)
        with self.assertRaises(WorkflowError):
            self.database.save_generated(
                report.id,
                report.original_notes,
                "Changed after approval",
                report.subject,
                report.recipient,
            )

    def test_successful_week_cannot_be_sent_twice(self):
        report = self.database.get_or_create_report(
            "2026-W35", "2026-08-27", "manager@example.com"
        )
        report = self.database.save_generated(
            report.id, "Did work", "Did work.", "Weekly", report.recipient
        )
        _, attempt_id, _ = self.database.approve_and_claim(report.id)
        self.database.complete_send(report.id, attempt_id, "gmail-123")
        with self.assertRaises(WorkflowError):
            self.database.approve_and_claim(report.id)
        saved = self.database.get_report(report.id)
        self.assertTrue(saved.sent_status)
        self.assertEqual(saved.gmail_message_id, "gmail-123")

    def test_uncertain_delivery_is_quarantined(self):
        report = self.database.get_or_create_report(
            "2026-W35", "2026-08-27", "manager@example.com"
        )
        report = self.database.save_generated(
            report.id, "Did work", "Did work.", "Weekly", report.recipient
        )
        _, attempt_id, _ = self.database.approve_and_claim(report.id)
        self.database.fail_send(report.id, attempt_id, "timeout", uncertain=True)
        with self.assertRaises(WorkflowError):
            self.database.approve_and_claim(report.id)

    def test_week_key_uses_local_iso_week(self):
        now = datetime(2026, 8, 30, 12, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(week_key(now), "2026-W35")

    def test_restart_reconciliation_creates_due_report_once(self):
        settings = Settings(
            manager_email="manager@example.com",
            schedule_day="thu",
            schedule_hour=17,
            schedule_minute=0,
        )
        before = datetime(2026, 8, 27, 16, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        reconcile_due_report(self.database, settings, before)
        self.assertEqual(self.database.list_reports(), [])

        after = datetime(2026, 8, 28, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        reconcile_due_report(self.database, settings, after)
        reconcile_due_report(self.database, settings, after)
        reports = self.database.list_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].week_key, "2026-W35")
        self.assertEqual(reports[0].report_date, "2026-08-27")

    def test_errors_are_persisted(self):
        report = self.database.get_or_create_report(
            "2026-W35", "2026-08-27", "manager@example.com"
        )
        self.database.record_error(report.id, "OpenAI unavailable")
        self.assertEqual(self.database.get_report(report.id).error, "OpenAI unavailable")


class GmailRetryTests(unittest.TestCase):
    def test_transient_http_failures_are_retried_with_a_bound(self):
        service = EmailService(Settings(gmail_max_retries=2))
        unavailable = HttpError(httplib2.Response({"status": "503"}), b"unavailable")

        class Request:
            calls = 0

            def execute(self, num_retries=0):
                self.calls += 1
                if self.calls < 3:
                    raise unavailable
                return {"id": "gmail-123"}

        request = Request()
        with patch("email_service.time.sleep") as sleep:
            result = service._execute_with_retry(request)
        self.assertEqual(result["id"], "gmail-123")
        self.assertEqual(request.calls, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_http_failure_is_not_retried(self):
        service = EmailService(Settings(gmail_max_retries=3))
        forbidden = HttpError(httplib2.Response({"status": "403"}), b"forbidden")

        class Request:
            calls = 0

            def execute(self, num_retries=0):
                self.calls += 1
                raise forbidden

        request = Request()
        with self.assertRaises(GmailSendError):
            service._execute_with_retry(request)
        self.assertEqual(request.calls, 1)


class OpenAIIntegrationTests(unittest.TestCase):
    def test_generation_uses_responses_api_and_keeps_source_notes(self):
        captured = {}

        class Responses:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(output_text="Professional report body")

        class FakeOpenAI:
            def __init__(self, api_key):
                self.responses = Responses()

        agent = ReportAgent(
            Settings(openai_api_key="test-key", sender_name="Chaudhary Dishan")
        )
        with patch("report_agent.OpenAI", FakeOpenAI):
            result = agent.generate("- Fixed responsive issues")

        self.assertEqual(result, "Professional report body")
        self.assertEqual(captured["model"], "gpt-5-mini")
        self.assertFalse(captured["store"])
        self.assertIn("- Fixed responsive issues", captured["input"])
        self.assertIn("Never invent", captured["instructions"])
        self.assertIn("Chaudhary Dishan", captured["instructions"])


if __name__ == "__main__":
    unittest.main()
