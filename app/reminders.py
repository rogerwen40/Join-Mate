import asyncio
import os
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.email_notifications import queue_notification, send_pending_emails
from app.models import Activity, Registration, ReminderDelivery


DEFAULT_INTERVAL_SECONDS = 30


def _select_reminder(remaining: timedelta) -> tuple[str, str] | None:
    if timedelta(0) < remaining <= timedelta(hours=1):
        return "1_hour", "將在 1 小時內開始"
    if timedelta(hours=1) < remaining <= timedelta(hours=24):
        return "24_hours", "將在 24 小時內開始"
    return None


def check_activity_reminders(now: datetime | None = None) -> int:
    """Create any currently due reminders and return the number delivered."""
    current_time = now or datetime.now()
    delivered_count = 0

    with SessionLocal() as database:
        activities = database.scalars(
            select(Activity).where(
                Activity.status == "open",
                Activity.starts_at > current_time,
                Activity.starts_at <= current_time + timedelta(hours=24),
            )
        ).all()

        for activity in activities:
            reminder = _select_reminder(activity.starts_at - current_time)
            if reminder is None:
                continue
            reminder_type, reminder_text = reminder

            registered_user_ids = database.scalars(
                select(Registration.user_id).where(
                    Registration.activity_id == activity.id,
                    Registration.status == "registered",
                )
            ).all()

            for user_id in registered_user_ids:
                already_delivered = database.scalar(
                    select(ReminderDelivery.id).where(
                        ReminderDelivery.activity_id == activity.id,
                        ReminderDelivery.user_id == user_id,
                        ReminderDelivery.reminder_type == reminder_type,
                    )
                )
                if already_delivered is not None:
                    continue

                queue_notification(
                    database,
                    user_id=user_id,
                    activity_id=activity.id,
                    message=(
                        f"「{activity.title}」{reminder_text}，"
                        f"時間為 {activity.starts_at.strftime('%m/%d %H:%M')}。"
                    ),
                    email_event=(
                        "reminder_1h" if reminder_type == "1_hour" else "reminder_24h"
                    ),
                )
                database.add(
                    ReminderDelivery(
                        activity_id=activity.id,
                        user_id=user_id,
                        reminder_type=reminder_type,
                    )
                )
                delivered_count += 1

        database.commit()

    return delivered_count


async def reminder_worker() -> None:
    raw_interval = os.getenv(
        "JOINMATE_REMINDER_INTERVAL_SECONDS",
        str(DEFAULT_INTERVAL_SECONDS),
    )
    try:
        interval_seconds = max(1, int(raw_interval))
    except ValueError:
        interval_seconds = DEFAULT_INTERVAL_SECONDS

    while True:
        await asyncio.to_thread(check_activity_reminders)
        await asyncio.to_thread(send_pending_emails)
        await asyncio.sleep(interval_seconds)
