import html
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.activity_formatting import detect_activity_emoji
from app.models import (
    Activity,
    EmailDelivery,
    EmailPreference,
    Notification,
    Registration,
    User,
)


MAX_EMAIL_ATTEMPTS = 5
EMAIL_BATCH_SIZE = 25

EMAIL_PREFERENCE_FIELDS = {
    "registration": "registration",
    "activity_changes": "activity_changes",
    "promotion": "promotion",
    "formed": "formed",
    "reminder_24h": "reminder_24h",
    "reminder_1h": "reminder_1h",
}
DEFAULT_EMAIL_PREFERENCES = {
    "registration": True,
    "activity_changes": True,
    "promotion": True,
    "formed": True,
    "reminder_24h": False,
    "reminder_1h": False,
}
WEEKDAYS_ZH = ("一", "二", "三", "四", "五", "六", "日")


def _email_reason(message: str) -> str:
    reason_rules = (
        ("成功報名", "報名成功"),
        ("加入候補", "候補通知"),
        ("候補轉為正式", "候補轉正"),
        ("已達最低人數", "活動已成團"),
        ("內容已更新", "活動內容更新"),
        ("已由建立者取消", "活動取消"),
        ("將在 24 小時", "活動前一天提醒"),
        ("將在 1 小時", "活動前一小時提醒"),
    )
    return next((label for keyword, label in reason_rules if keyword in message), "活動通知")


def _activity_email_details(
    activity: Activity,
    *,
    confirmed_count: int,
) -> tuple[str, str]:
    starts_at = activity.starts_at
    date_text = (
        f"{starts_at.year}/{starts_at.month}/{starts_at.day}"
        f"（{WEEKDAYS_ZH[starts_at.weekday()]}） {starts_at.strftime('%H:%M')}"
    )
    fee_text = f"每人 ${activity.fee}" if activity.fee else "免費"
    people_text = f"正取 {confirmed_count}/{activity.max_people} 名"
    if confirmed_count >= activity.max_people:
        people_text += "，開放候補"
    text_details = (
        f"類型：{activity.activity_type}\n"
        f"時間：{date_text}\n"
        f"地點：{activity.location}\n"
        f"費用：{fee_text}\n"
        f"人數：{people_text}"
    )
    html_details = (
        f"<strong>類型：</strong>{html.escape(activity.activity_type)}<br>"
        f"<strong>時間：</strong>{date_text}<br>"
        f"<strong>地點：</strong>{html.escape(activity.location)}<br>"
        f"<strong>費用：</strong>{html.escape(fee_text)}<br>"
        f"<strong>人數：</strong>{html.escape(people_text)}"
    )
    return text_details, html_details


def get_email_preferences(database: Session, user_id: int) -> EmailPreference:
    preferences = database.scalar(
        select(EmailPreference).where(EmailPreference.user_id == user_id)
    )
    if preferences is None:
        preferences = EmailPreference(user_id=user_id, **DEFAULT_EMAIL_PREFERENCES)
        database.add(preferences)
        database.flush()
    return preferences


def email_is_enabled(database: Session, user_id: int, event_type: str | None) -> bool:
    field = EMAIL_PREFERENCE_FIELDS.get(event_type or "")
    if field is None:
        return False
    preferences = database.scalar(
        select(EmailPreference).where(EmailPreference.user_id == user_id)
    )
    if preferences is None:
        return DEFAULT_EMAIL_PREFERENCES[field]
    return bool(getattr(preferences, field))


def queue_notification(
    database: Session,
    *,
    user_id: int,
    activity_id: int,
    message: str,
    email_event: str | None = None,
) -> Notification:
    """Create an in-app notification and queue Email when the user opted in."""
    notification = Notification(
        user_id=user_id,
        activity_id=activity_id,
        message=message,
    )
    database.add(notification)
    database.flush()
    if email_is_enabled(database, user_id, email_event):
        database.add(EmailDelivery(notification_id=notification.id))
    return notification


def _send_via_google_apps_script(
    *,
    recipient: str,
    subject: str,
    body: str,
    html_body: str,
) -> None:
    webhook_url = os.getenv("JOINMATE_EMAIL_WEBHOOK_URL", "").strip()
    shared_secret = os.getenv("JOINMATE_EMAIL_SECRET", "").strip()
    if not webhook_url or not shared_secret:
        raise RuntimeError("Email webhook is not configured")

    payload = json.dumps(
        {
            "action": "send_email",
            "secret": shared_secret,
            "to": recipient,
            "subject": subject,
            "body": body,
            "html_body": html_body,
        }
    ).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Email webhook request failed: {exc}") from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Email webhook returned an invalid response") from exc
    if result.get("ok") is not True:
        raise RuntimeError(str(result.get("error") or "Email webhook rejected request"))


def send_pending_emails() -> int:
    """Send pending outbox items and return the number sent successfully."""
    if not os.getenv("JOINMATE_EMAIL_WEBHOOK_URL", "").strip():
        return 0
    if not os.getenv("JOINMATE_EMAIL_SECRET", "").strip():
        return 0

    sent_count = 0
    with SessionLocal() as database:
        statement = (
            select(EmailDelivery)
            .where(
                EmailDelivery.sent_at.is_(None),
                EmailDelivery.attempts < MAX_EMAIL_ATTEMPTS,
            )
            .order_by(EmailDelivery.id.asc())
            .limit(EMAIL_BATCH_SIZE)
        )
        if engine.dialect.name != "sqlite":
            statement = statement.with_for_update(skip_locked=True)
        deliveries = database.scalars(statement).all()

        for delivery in deliveries:
            notification = database.get(Notification, delivery.notification_id)
            if notification is None:
                delivery.attempts = MAX_EMAIL_ATTEMPTS
                delivery.last_error = "Notification no longer exists"
                continue

            user = database.get(User, notification.user_id)
            activity = database.get(Activity, notification.activity_id)
            if user is None or activity is None:
                delivery.attempts = MAX_EMAIL_ATTEMPTS
                delivery.last_error = "User or activity no longer exists"
                continue

            public_url = os.getenv(
                "JOINMATE_PUBLIC_URL", "https://joinmate.onrender.com"
            ).rstrip("/")
            activity_url = f"{public_url}/activities/{activity.id}"
            confirmed_count = database.scalar(
                select(func.count(Registration.id)).where(
                    Registration.activity_id == activity.id,
                    Registration.status == "registered",
                )
            ) or 0
            text_details, html_details = _activity_email_details(
                activity,
                confirmed_count=confirmed_count,
            )
            reason = _email_reason(notification.message)
            activity_emoji = detect_activity_emoji(activity)
            subject = f"[JoinMate] {reason}｜{activity_emoji} {activity.title}"
            body = (
                f"{user.name} 您好：\n\n"
                f"{notification.message}\n\n"
                "活動資訊\n"
                f"活動：{activity_emoji} {activity.title}\n"
                f"{text_details}\n"
                f"報名／查看連結：{activity_url}\n\n"
                "此信由 JoinMate 自動寄出。"
            )
            html_body = (
                f"<p>{html.escape(user.name)} 您好：</p>"
                f"<p>{html.escape(notification.message)}</p>"
                "<h3 style=\"margin-bottom:8px\">活動資訊</h3>"
                f"<p><strong>活動：</strong>{activity_emoji} {html.escape(activity.title)}<br>"
                f"{html_details}</p>"
                f'<p><a href="{html.escape(activity_url, quote=True)}">'
                "前往 JoinMate 報名／查看活動</a></p>"
                "<p style=\"color:#666\">此信由 JoinMate 自動寄出。</p>"
            )

            try:
                _send_via_google_apps_script(
                    recipient=user.email,
                    subject=subject,
                    body=body,
                    html_body=html_body,
                )
            except RuntimeError as exc:
                delivery.attempts += 1
                delivery.last_error = str(exc)[:1000]
            else:
                delivery.sent_at = datetime.now()
                delivery.last_error = None
                sent_count += 1

        database.commit()

    return sent_count
