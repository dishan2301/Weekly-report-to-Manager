import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from config import Settings
from database import Database


class FakeAgent:
    def generate(self, notes, additional_instructions=""):
        return "Hello,\n\nI fixed the responsive issues.\n\nRegards"


class FakeEmailService:
    def __init__(self):
        self.calls = []

    def send(self, recipient, subject, body, stable_message_id):
        self.calls.append((recipient, subject, body, stable_message_id))
        return "gmail-message-123"


class AppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "reports.db")
        self.database.initialize()
        self.email = FakeEmailService()
        self.patches = [
            patch.object(app, "database", self.database),
            patch.object(app, "report_agent", FakeAgent()),
            patch.object(app, "email_service", self.email),
            patch.object(
                app,
                "settings",
                Settings(
                    agent_enabled=True,
                    manager_email="manager@example.com",
                    database_path=Path(self.temp_dir.name) / "reports.db",
                ),
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def test_preview_precedes_send_and_duplicate_send_is_blocked(self):
        preview = app.generate_report("- Fixed responsive issues")
        self.assertIn("COMPLETE EMAIL PREVIEW", preview)
        self.assertIn("Send Report", preview)
        self.assertIn(">Edit<", preview)
        self.assertIn(">Cancel<", preview)
        self.assertEqual(self.email.calls, [])

        report = self.database.list_reports()[0]
        self.assertEqual(report.original_notes, "- Fixed responsive issues")
        response = app.send_report(report.id)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(self.email.calls), 1)

        duplicate = app.send_report(report.id)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(len(self.email.calls), 1)

    def test_cancel_never_calls_gmail(self):
        app.generate_report("- Fixed responsive issues")
        report = self.database.list_reports()[0]
        response = app.cancel_report(report.id)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.email.calls, [])
        self.assertEqual(self.database.get_report(report.id).approval_status, "cancelled")

    def test_edit_invalidates_approval_material(self):
        app.generate_report("- Fixed responsive issues")
        report = self.database.list_reports()[0]
        preview = app.save_edit(report.id, "Updated subject", "Updated body")
        self.assertIn("Updated body", preview)
        saved = self.database.get_report(report.id)
        self.assertEqual(saved.approval_status, "draft")
        self.assertEqual(saved.approval_hash, "")


if __name__ == "__main__":
    unittest.main()
