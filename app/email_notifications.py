import html
import json
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine
from app.models import Activity, EmailDelivery, Notification, User


MAX_EMAIL_ATTEMPTS = 5
EMAIL_BATCH_SIZE = 25


def queue_notification(
    database: Session,
    *,
    user_id: int,
    activity_id: int,
    message: str,
) -> Notification:
    """Create one in-app notification and its matching email outbox item."""
    notification = Notification(
        user_id=user_id,
        activity_id=activity_id,
        message=message,
    )
    database.add(notification)
    database.flush()
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
            subject = f"[JoinMate] {activity.title} 通知"
            body = (
                f"{user.name} 您好：\n\n"
                f"{notification.message}\n\n"
                f"活動：{activity.title}\n"
                f"時間：{activity.starts_at.strftime('%Y/%m/%d %H:%M')}\n"
                f"地點：{activity.location}\n"
                f"查看活動：{activity_url}\n\n"
                "此信由 JoinMate 自動寄出。"
            )
            html_body = (
                f"<p>{html.escape(user.name)} 您好：</p>"
                f"<p>{html.escape(notification.message)}</p>"
                f"<p><strong>活動：</strong>{html.escape(activity.title)}<br>"
                f"<strong>時間：</strong>"
                f"{activity.starts_at.strftime('%Y/%m/%d %H:%M')}<br>"
                f"<strong>地點：</strong>{html.escape(activity.location)}</p>"
                f'<p><a href="{html.escape(activity_url, quote=True)}">'
                "前往 JoinMate 查看活動</a></p>"
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
