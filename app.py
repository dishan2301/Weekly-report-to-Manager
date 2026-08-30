from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import get_settings
from database import Database
from email_service import EmailService, GmailSendError
from models import Report, WorkflowError
from report_agent import ReportAgent
from scheduler import create_scheduler, next_report_time, reconcile_due_report, week_key


settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)
database = Database(settings.database_path)
report_agent = ReportAgent(settings)
email_service = EmailService(settings)
scheduler = create_scheduler(database, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    if settings.agent_enabled:
        reconcile_due_report(database, settings)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard(message: str = "", error: str = "") -> str:
    reports = database.list_reports()
    next_run = next_report_time(settings)
    next_text = (
        next_run.strftime("%A, %d %B %Y at %I:%M %p %Z")
        if next_run
        else "Disabled"
    )
    disabled = " disabled" if not settings.agent_enabled else ""
    history = _history_table(reports)
    return _page(
        f"""
        {_notice(message, error)}
        <section class="hero">
          <p class="eyebrow">AUTOMATED WEEKLY CHECK-IN</p>
          <h1>Weekly Report Agent</h1>
          <p class="next">Next Report: <strong>{html.escape(next_text)}</strong></p>
        </section>
        <section class="card">
          <h2>This Week's Notes</h2>
          <p class="hint">Add 2-3 short lines or bullet points. The agent improves the writing without adding facts.</p>
          <form method="post" action="/generate">
            <textarea name="notes" rows="10" maxlength="10000" required placeholder="- Worked on Indian Infotech website\n- Fixed responsive issues\n- Learned RAG and embeddings"{disabled}></textarea>
            <button class="primary" type="submit"{disabled}>Generate Report</button>
          </form>
          {('<p class="warning">The agent is disabled by AGENT_ENABLED=false.</p>' if not settings.agent_enabled else '')}
        </section>
        <section class="card">
          <h2>Previous Reports</h2>
          {history}
        </section>
        """
    )


@app.post("/generate", response_class=HTMLResponse)
def generate_report(notes: str = Form(...)) -> str:
    if not settings.agent_enabled:
        return _error_page("The agent is disabled.", 403)
    if not settings.manager_email:
        return _error_page("MANAGER_EMAIL is not configured.", 503)
    if len(notes) > 10000:
        return _error_page("Notes must be 10,000 characters or fewer.", 400)

    now = datetime.now(ZoneInfo(settings.timezone))
    report = database.get_or_create_report(
        week_key(now), now.date().isoformat(), settings.manager_email
    )
    if report.sent_status:
        return _error_page("A report for the current week was already sent.", 409)
    try:
        body = report_agent.generate(notes)
        subject = f"Weekly Report - {report.week_key}"
        report = database.save_generated(
            report.id, notes.strip(), body, subject, settings.manager_email
        )
    except Exception as exc:
        database.record_error(report.id, str(exc))
        LOGGER.exception("Report generation failed")
        return _error_page(f"Report generation failed: {exc}", 502)
    return _preview(report, "Review the complete email before choosing an action.")


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def view_report(report_id: int) -> str:
    report = database.get_report(report_id)
    if not report:
        return _error_page("Report not found.", 404)
    return _preview(report)


@app.get("/reports/{report_id}/edit", response_class=HTMLResponse)
def edit_report(report_id: int) -> str:
    report = database.get_report(report_id)
    if not report:
        return _error_page("Report not found.", 404)
    if report.sent_status or report.send_state in {"sending", "uncertain"}:
        return _error_page("This report can no longer be edited safely.", 409)
    return _page(
        f"""
        <section class="card">
          <p class="eyebrow">EDIT {html.escape(report.week_key)}</p>
          <h1>Edit Report</h1>
          <form method="post" action="/reports/{report.id}/save">
            <label>Subject</label>
            <input name="subject" maxlength="300" required value="{html.escape(report.subject)}">
            <label>Email body</label>
            <textarea name="body" rows="18" maxlength="30000" required>{html.escape(report.generated_report)}</textarea>
            <button class="primary" type="submit">Save Edited Report</button>
          </form>
          <hr>
          <form method="post" action="/reports/{report.id}/regenerate">
            <label>Additional instructions or facts</label>
            <textarea name="instructions" rows="5" maxlength="5000" placeholder="Example: Make the tone more concise. Do not add details I did not provide.">{html.escape(report.edit_instructions)}</textarea>
            <button type="submit">Regenerate</button>
          </form>
          <a class="back" href="/reports/{report.id}">Back to preview</a>
        </section>
        """
    )


@app.post("/reports/{report_id}/save", response_class=HTMLResponse)
def save_edit(
    report_id: int,
    subject: str = Form(...),
    body: str = Form(...),
) -> str:
    report = database.get_report(report_id)
    if not report:
        return _error_page("Report not found.", 404)
    if not subject.strip() or not body.strip():
        return _error_page("Subject and body are required.", 400)
    try:
        report = database.save_generated(
            report.id,
            report.original_notes,
            body.strip(),
            subject.strip(),
            report.recipient,
            report.edit_instructions,
        )
    except WorkflowError as exc:
        return _error_page(str(exc), 409)
    return _preview(report, "Edits saved. Review the complete email again before sending.")


@app.post("/reports/{report_id}/regenerate", response_class=HTMLResponse)
def regenerate(report_id: int, instructions: str = Form("")) -> str:
    report = database.get_report(report_id)
    if not report:
        return _error_page("Report not found.", 404)
    try:
        body = report_agent.generate(report.original_notes, instructions)
        report = database.save_generated(
            report.id,
            report.original_notes,
            body,
            report.subject,
            report.recipient,
            instructions.strip(),
        )
    except WorkflowError as exc:
        return _error_page(str(exc), 409)
    except Exception as exc:
        database.record_error(report.id, str(exc))
        LOGGER.exception("Report regeneration failed")
        return _error_page(f"Report regeneration failed: {exc}", 502)
    return _preview(report, "Report regenerated. Review the complete email again.")


@app.post("/reports/{report_id}/send", response_class=HTMLResponse)
def send_report(report_id: int) -> str:
    if not settings.agent_enabled:
        return _error_page("The agent is disabled.", 403)
    try:
        report, attempt_id, stable_message_id = database.approve_and_claim(report_id)
    except WorkflowError as exc:
        return _error_page(str(exc), 409)

    try:
        gmail_message_id = email_service.send(
            report.recipient,
            report.subject,
            report.generated_report,
            stable_message_id,
        )
        database.complete_send(report.id, attempt_id, gmail_message_id)
    except GmailSendError as exc:
        database.fail_send(report.id, attempt_id, str(exc), exc.uncertain)
        LOGGER.exception("Gmail delivery failed for report %s", report.id)
        detail = str(exc)
        if exc.uncertain:
            detail += " Another send is blocked to prevent a possible duplicate. Check Gmail Sent mail."
        return _error_page(detail, 502)
    except Exception as exc:
        database.fail_send(report.id, attempt_id, str(exc), True)
        LOGGER.exception("Unexpected delivery failure for report %s", report.id)
        return _error_page(
            "Delivery ended without confirmation. Another send is blocked to prevent a duplicate.",
            502,
        )
    return RedirectResponse("/?message=Report+sent+successfully", status_code=303)


@app.post("/reports/{report_id}/cancel")
def cancel_report(report_id: int):
    try:
        database.cancel_report(report_id)
    except WorkflowError as exc:
        return _error_page(str(exc), 409)
    return RedirectResponse("/?message=Report+cancelled.+Nothing+was+sent", status_code=303)


def _preview(report: Report, message: str = "") -> str:
    send_disabled = " disabled" if report.sent_status or report.send_state in {"sending", "uncertain"} else ""
    status = "Sent" if report.sent_status else report.send_state.title()
    return _page(
        f"""
        {_notice(message, report.error)}
        <section class="card">
          <p class="eyebrow">COMPLETE EMAIL PREVIEW · {html.escape(status)}</p>
          <h1>{html.escape(report.subject or 'Weekly Report')}</h1>
          <dl>
            <dt>To</dt><dd>{html.escape(report.recipient)}</dd>
            <dt>Subject</dt><dd>{html.escape(report.subject)}</dd>
          </dl>
          <div class="email-body">{html.escape(report.generated_report)}</div>
          <div class="actions">
            <form method="post" action="/reports/{report.id}/send">
              <button class="primary" type="submit"{send_disabled}>Send Report</button>
            </form>
            <a class="button" href="/reports/{report.id}/edit">Edit</a>
            <form method="post" action="/reports/{report.id}/cancel">
              <button class="danger" type="submit"{send_disabled}>Cancel</button>
            </form>
          </div>
          <p class="hint">Send is the approval action. The exact email shown above is fingerprinted before Gmail is called.</p>
          <a class="back" href="/">Back to dashboard</a>
        </section>
        """
    )


def _history_table(reports: list[Report]) -> str:
    if not reports:
        return '<p class="hint">No reports yet.</p>'
    rows = "".join(
        f"""
        <tr>
          <td><a href="/reports/{report.id}">{html.escape(report.week_key)}</a></td>
          <td>{html.escape(report.report_date)}</td>
          <td><span class="pill">{'Sent' if report.sent_status else html.escape(report.approval_status.title())}</span></td>
          <td>{html.escape(report.gmail_message_id or '—')}</td>
        </tr>
        """
        for report in reports
    )
    return f"""
    <div class="table-wrap"><table>
      <thead><tr><th>Week</th><th>Date</th><th>Status</th><th>Gmail ID</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
    """


def _notice(message: str = "", error: str = "") -> str:
    if error:
        return f'<div class="notice error">{html.escape(error)}</div>'
    if message:
        return f'<div class="notice success">{html.escape(message)}</div>'
    return ""


def _error_page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        _page(
            f'<section class="card"><h1>Could not complete that action</h1>'
            f'<div class="notice error">{html.escape(message)}</div>'
            '<a class="back" href="/">Back to dashboard</a></section>'
        ),
        status_code=status_code,
    )


def _page(content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(settings.app_name)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#667085; --line:#dfe5ee; --brand:#2457d6; --danger:#b42318; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:#f4f6fa; color:var(--ink); font:16px/1.5 Inter,system-ui,sans-serif; }}
    main {{ width:min(920px,calc(100% - 32px)); margin:48px auto; }}
    h1 {{ margin:.2rem 0 .6rem; font-size:clamp(2rem,5vw,3.25rem); line-height:1.1; }}
    h2 {{ margin-top:0; }}
    .hero {{ margin-bottom:26px; }} .eyebrow {{ color:var(--brand); font-weight:750; letter-spacing:.12em; font-size:.75rem; }}
    .next,.hint {{ color:var(--muted); }}
    .card {{ background:white; border:1px solid var(--line); border-radius:16px; padding:28px; margin:20px 0; box-shadow:0 8px 30px rgba(25,38,68,.05); }}
    label {{ display:block; font-weight:700; margin:16px 0 7px; }}
    textarea,input {{ width:100%; border:1px solid #bdc7d6; border-radius:9px; padding:13px; font:inherit; color:inherit; background:#fff; }}
    textarea:focus,input:focus {{ outline:3px solid #d9e4ff; border-color:var(--brand); }}
    button,.button {{ display:inline-block; border:1px solid #b8c3d2; border-radius:9px; padding:10px 16px; margin-top:15px; background:white; color:var(--ink); font:inherit; font-weight:700; text-decoration:none; cursor:pointer; }}
    button.primary {{ background:var(--brand); border-color:var(--brand); color:white; }} button.danger {{ color:var(--danger); }}
    button:disabled {{ opacity:.45; cursor:not-allowed; }}
    .actions {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-top:18px; }} .actions form {{ margin:0; }}
    .email-body {{ white-space:pre-wrap; border:1px solid var(--line); background:#fbfcfe; border-radius:10px; padding:22px; margin:20px 0; }}
    dl {{ display:grid; grid-template-columns:80px 1fr; gap:6px 14px; }} dt {{ color:var(--muted); }} dd {{ margin:0; }}
    .notice {{ padding:12px 15px; border-radius:9px; margin:16px 0; }} .success {{ background:#ecfdf3; color:#067647; }} .error,.warning {{ background:#fef3f2; color:#b42318; }}
    .table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:12px; border-bottom:1px solid var(--line); }}
    .pill {{ font-size:.85rem; padding:4px 8px; border-radius:99px; background:#eef2f8; }} .back {{ display:inline-block; margin-top:18px; }} hr {{ border:0; border-top:1px solid var(--line); margin:30px 0; }}
  </style>
</head>
<body><main>{content}</main></body>
</html>"""
