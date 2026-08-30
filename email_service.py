from __future__ import annotations

import base64
import logging
import os
import socket
import time
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import Settings


LOGGER = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class GmailSendError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


class EmailService:
    def __init__(self, settings: Settings, template_dir: Path | None = None):
        self.settings = settings
        directory = template_dir or Path("templates")
        self.templates = Environment(
            loader=FileSystemLoader(directory),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def send(
        self,
        recipient: str,
        subject: str,
        body: str,
        stable_message_id: str,
    ) -> str:
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = stable_message_id
        message.set_content(body)
        html = self.templates.get_template("email_template.html").render(body=body)
        message.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        try:
            service = build(
                "gmail", "v1", credentials=self._credentials(), cache_discovery=False
            )
            request = service.users().messages().send(userId="me", body={"raw": raw})
            result = self._execute_with_retry(request)
        except GmailSendError:
            raise
        except (TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise GmailSendError(
                f"Gmail connection ended without a confirmed result: {exc}", uncertain=True
            ) from exc
        except Exception as exc:
            raise GmailSendError(f"Gmail setup or authentication failed: {exc}") from exc

        message_id = result.get("id", "")
        if not message_id:
            raise GmailSendError("Gmail did not return a message ID.", uncertain=True)
        return message_id

    def _credentials(self) -> Credentials:
        token_path = self.settings.gmail_token_file
        credentials: Credentials | None = None
        if token_path.exists():
            credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            credentials_path = self.settings.gmail_credentials_file
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth client file not found: {credentials_path}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            credentials = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        os.chmod(token_path, 0o600)
        return credentials

    def _execute_with_retry(self, request):
        retries = max(0, self.settings.gmail_max_retries)
        for attempt in range(retries + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status not in TRANSIENT_HTTP_STATUSES or attempt == retries:
                    raise GmailSendError(f"Gmail API error ({status}): {exc}") from exc
                delay = 2**attempt
                LOGGER.warning(
                    "Transient Gmail API error (status %s); retrying in %s second(s)",
                    status,
                    delay,
                )
                time.sleep(delay)
        raise AssertionError("retry loop exhausted")
