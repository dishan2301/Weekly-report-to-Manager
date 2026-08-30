from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Settings
from database import Database


LOGGER = logging.getLogger(__name__)
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def ensure_current_week_report(database: Database, settings: Settings, now: datetime | None = None) -> None:
    if not settings.agent_enabled:
        return
    now = now or datetime.now(ZoneInfo(settings.timezone))
    scheduled_date = now.date() - timedelta(days=now.weekday() - DAY_INDEX[settings.schedule_day])
    report = database.get_or_create_report(
        week_key(now), scheduled_date.isoformat(), settings.manager_email
    )
    LOGGER.info("Weekly report %s is ready for notes at /reports/%s", report.week_key, report.id)


def reconcile_due_report(
    database: Database, settings: Settings, now: datetime | None = None
) -> None:
    """Create this week's prompt after its configured local time, including after restarts."""
    if not settings.agent_enabled:
        return
    now = now or datetime.now(ZoneInfo(settings.timezone))
    due_position = (DAY_INDEX[settings.schedule_day], settings.schedule_hour, settings.schedule_minute)
    current_position = (now.weekday(), now.hour, now.minute)
    if current_position >= due_position:
        ensure_current_week_report(database, settings, now)


def create_scheduler(database: Database, settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        ensure_current_week_report,
        CronTrigger(
            day_of_week=settings.schedule_day,
            hour=settings.schedule_hour,
            minute=settings.schedule_minute,
            timezone=settings.timezone,
        ),
        args=[database, settings],
        id="weekly-report-reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


def next_report_time(settings: Settings) -> datetime | None:
    if not settings.agent_enabled:
        return None
    now = datetime.now(ZoneInfo(settings.timezone))
    trigger = CronTrigger(
        day_of_week=settings.schedule_day,
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        timezone=settings.timezone,
    )
    return trigger.get_next_fire_time(None, now)
