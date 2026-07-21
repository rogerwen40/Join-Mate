import asyncio
import os
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import EmailStr
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import Base, SessionLocal, engine, get_db
from app.auth import get_current_user, hash_password, require_current_user, verify_password
from app.models import (
    Activity,
    ActivityFeedback,
    ActivityOwnership,
    AdminUser,
    AttendanceRecord,
    LoginAttempt,
    Notification,
    Registration,
    User,
    UserCredential,
)
from app.reminders import reminder_worker
from app.schemas import ActivityRead


BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as database:
        admin_count = database.scalar(select(func.count(AdminUser.id))) or 0
        if admin_count == 0:
            first_user = database.scalar(select(User).order_by(User.id.asc()))
            if first_user is not None:
                database.add(AdminUser(user_id=first_user.id))
                database.commit()
    reminder_task = asyncio.create_task(reminder_worker())
    try:
        yield
    finally:
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task


app = FastAPI(
    title="JoinMate",
    description="讓不同社群與朋友一起建立、報名活動的揪團系統",
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv(
        "JOINMATE_SESSION_SECRET",
        "joinmate-local-development-secret-change-before-deploy",
    ),
    same_site="lax",
    https_only=os.getenv("JOINMATE_HTTPS_ONLY", "0") == "1",
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

NOTICE_MESSAGES = {
    "registered": "報名成功，已加入正式名單。",
    "waitlisted": "活動已額滿，已加入候補名單。",
    "already_joined": "你已經報名此活動。",
    "cancelled": "已取消報名。",
    "cancelled_promoted": "已取消報名，並自動將第一位候補成員轉為正式名單。",
    "updated": "活動資料已更新。",
    "activity_cancelled": "活動已取消，參加者已收到通知。",
    "activity_completed": "活動已完成，出席紀錄與通知已更新。",
    "feedback_saved": "你的活動評價已儲存。",
}


def add_notification(
    database: Session,
    *,
    user_id: int,
    activity_id: int,
    message: str,
) -> None:
    database.add(
        Notification(
            user_id=user_id,
            activity_id=activity_id,
            message=message,
        )
    )


def require_session_user_id(request: Request) -> int:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="請先登入")
    return user_id


def user_is_admin(database: Session, user_id: int) -> bool:
    return database.scalar(
        select(AdminUser.id).where(AdminUser.user_id == user_id)
    ) is not None


def require_admin_user(request: Request, database: Session) -> User:
    current_user = require_current_user(request, database)
    if not user_is_admin(database, current_user.id):
        raise HTTPException(status_code=403, detail="只有管理者可以使用此功能")
    return current_user


def lock_activity_for_write(
    database: Session,
    activity_id: int,
) -> Activity:
    """Serialize activity capacity changes across concurrent requests."""
    if database.bind is not None and database.bind.dialect.name == "sqlite":
        database.execute(text("BEGIN IMMEDIATE"))
        activity = database.get(Activity, activity_id)
    else:
        activity = database.scalar(
            select(Activity)
            .where(Activity.id == activity_id)
            .with_for_update()
        )
    if activity is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    return activity


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str | None = None,
    activity_type: str | None = None,
    activity_date: date | None = None,
    location: str | None = None,
    status_filter: str = "open",
    available_only: bool = False,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = get_current_user(request, database)
    is_admin = (
        current_user is not None
        and user_is_admin(database, current_user.id)
    )
    registered_count_query = (
        select(func.count(Registration.id))
        .where(
            Registration.activity_id == Activity.id,
            Registration.status == "registered",
        )
        .correlate(Activity)
        .scalar_subquery()
    )
    statement = select(Activity)
    normalized_query = q.strip() if q else None
    normalized_location = location.strip() if location else None
    if normalized_query:
        search_pattern = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                Activity.title.ilike(search_pattern),
                Activity.description.ilike(search_pattern),
                Activity.location.ilike(search_pattern),
            )
        )
    if activity_type:
        statement = statement.where(Activity.activity_type == activity_type)
    if activity_date:
        day_start = datetime.combine(activity_date, time.min)
        day_end = day_start + timedelta(days=1)
        statement = statement.where(
            Activity.starts_at >= day_start,
            Activity.starts_at < day_end,
        )
    if normalized_location:
        statement = statement.where(
            Activity.location.ilike(f"%{normalized_location}%")
        )
    allowed_statuses = {"open", "completed", "cancelled", "all"}
    if status_filter not in allowed_statuses:
        status_filter = "open"
    if status_filter != "all":
        statement = statement.where(Activity.status == status_filter)
    if available_only:
        statement = statement.where(
            Activity.status == "open",
            registered_count_query < Activity.max_people,
        )

    activities = database.scalars(
        statement.order_by(Activity.starts_at.asc(), Activity.id.asc())
    ).all()
    activity_ids = [activity.id for activity in activities]
    count_rows = database.execute(
        select(Registration.activity_id, func.count(Registration.id))
        .where(
            Registration.activity_id.in_(activity_ids or [-1]),
            Registration.status == "registered",
        )
        .group_by(Registration.activity_id)
    ).all()
    registered_counts = {
        activity_id: count for activity_id, count in count_rows
    }
    rating_rows = database.execute(
        select(ActivityFeedback.activity_id, func.avg(ActivityFeedback.rating))
        .where(ActivityFeedback.activity_id.in_(activity_ids or [-1]))
        .group_by(ActivityFeedback.activity_id)
    ).all()
    average_ratings = {
        activity_id: round(float(average_rating), 1)
        for activity_id, average_rating in rating_rows
    }
    capacity_statuses: dict[int, str] = {}
    for activity in activities:
        current_count = registered_counts.get(activity.id, 0)
        if activity.status == "completed":
            capacity_statuses[activity.id] = "已完成"
        elif activity.status == "cancelled":
            capacity_statuses[activity.id] = "已取消"
        elif current_count >= activity.max_people:
            capacity_statuses[activity.id] = "額滿"
        elif current_count >= activity.min_people:
            capacity_statuses[activity.id] = "已成團"
        else:
            capacity_statuses[activity.id] = "招募中"
    unread_count = 0
    if current_user is not None:
        unread_count = database.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == current_user.id,
                Notification.is_read.is_(False),
            )
        ) or 0
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "page_title": "JoinMate｜讓興趣 Meet 夥伴",
            "activities": activities,
            "unread_count": unread_count,
            "current_user": current_user,
            "is_admin": is_admin,
            "registered_counts": registered_counts,
            "capacity_statuses": capacity_statuses,
            "average_ratings": average_ratings,
            "filters": {
                "q": normalized_query or "",
                "activity_type": activity_type or "",
                "activity_date": activity_date.isoformat() if activity_date else "",
                "location": normalized_location or "",
                "status_filter": status_filter,
                "available_only": available_only,
            },
            "activity_types": ["吃飯", "運動", "桌遊", "討論", "其他"],
        },
    )


@app.get("/me", response_class=HTMLResponse)
def my_dashboard(
    request: Request,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = get_current_user(request, database)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    registrations = database.scalars(
        select(Registration)
        .where(Registration.user_id == current_user.id)
        .options(selectinload(Registration.activity))
        .order_by(Registration.registration_time.desc(), Registration.id.desc())
    ).all()
    registration_ids = [registration.id for registration in registrations]
    attendance_records = database.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.registration_id.in_(registration_ids or [-1])
        )
    ).all()
    attendance_map = {
        record.registration_id: record for record in attendance_records
    }

    created_activities = database.scalars(
        select(Activity)
        .join(
            ActivityOwnership,
            ActivityOwnership.activity_id == Activity.id,
        )
        .where(ActivityOwnership.user_id == current_user.id)
        .order_by(Activity.starts_at.desc())
    ).all()

    current_time = datetime.now()
    upcoming_registrations = [
        registration
        for registration in registrations
        if registration.status in {"registered", "waitlisted"}
        and registration.activity.status == "open"
        and registration.activity.starts_at >= current_time
    ]
    history_registrations = [
        registration
        for registration in registrations
        if registration not in upcoming_registrations
    ]
    attended_count = sum(
        record.attendance_status == "attended" for record in attendance_records
    )
    absent_count = sum(
        record.attendance_status == "absent" for record in attendance_records
    )
    attendance_total = attended_count + absent_count
    attendance_rate = (
        round(attended_count / attendance_total * 100)
        if attendance_total > 0
        else None
    )
    cancelled_count = sum(
        registration.status == "cancelled" for registration in registrations
    )

    return templates.TemplateResponse(
        request=request,
        name="my_dashboard.html",
        context={
            "page_title": "我的紀錄",
            "current_user": current_user,
            "created_activities": created_activities,
            "upcoming_registrations": upcoming_registrations,
            "history_registrations": history_registrations,
            "attendance_map": attendance_map,
            "attended_count": attended_count,
            "absent_count": absent_count,
            "attendance_rate": attendance_rate,
            "cancelled_count": cancelled_count,
        },
    )


@app.get("/activities/new", response_class=HTMLResponse)
def new_activity_page(
    request: Request,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = get_current_user(request, database)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="new_activity.html",
        context={
            "page_title": "建立活動",
            "error": None,
            "current_user": current_user,
        },
    )


@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, database: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_user(request, database)
    members = database.scalars(select(User).order_by(User.name.asc())).all()
    return templates.TemplateResponse(
        request=request,
        name="members.html",
        context={
            "page_title": "使用者列表",
            "members": members,
            "error": None,
            "current_user": current_user,
        },
    )


@app.get("/account/setup", response_class=HTMLResponse)
def account_setup_page(
    request: Request,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = get_current_user(request, database)
    if current_user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="account_setup.html",
        context={
            "page_title": "建立帳號",
            "error": None,
            "current_user": None,
        },
    )


@app.post("/account/setup", response_class=HTMLResponse)
def account_setup(
    request: Request,
    name: Annotated[str, Form(min_length=2, max_length=80)],
    email: Annotated[EmailStr, Form(max_length=255)],
    password: Annotated[str, Form(pattern=r"^\d{4}$")],
    password_confirm: Annotated[str, Form(pattern=r"^\d{4}$")],
    database: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    if password != password_confirm:
        return templates.TemplateResponse(
            request=request,
            name="account_setup.html",
            context={
                "page_title": "建立帳號",
                "error": "兩次輸入的密碼不一致。",
                "current_user": None,
            },
            status_code=422,
        )

    member = User(name=name.strip(), email=normalized_email)
    database.add(member)
    try:
        database.flush()
        admin_count = database.scalar(select(func.count(AdminUser.id))) or 0
        if admin_count == 0:
            database.add(AdminUser(user_id=member.id))
        database.add(
            UserCredential(
                user_id=member.id,
                password_hash=hash_password(password),
            )
        )
        database.commit()
    except IntegrityError:
        database.rollback()
        return templates.TemplateResponse(
            request=request,
            name="account_setup.html",
            context={
                "page_title": "建立帳號",
                "error": "這個 Email 已經註冊，請直接登入。",
                "current_user": None,
            },
            status_code=422,
        )

    request.session.clear()
    request.session["user_id"] = member.id
    return RedirectResponse(url="/", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = require_admin_user(request, database)
    total_users = database.scalar(select(func.count(User.id))) or 0
    activities = database.scalars(
        select(Activity).order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all()
    total_activities = len(activities)
    open_count = sum(activity.status == "open" for activity in activities)
    completed_count = sum(activity.status == "completed" for activity in activities)
    cancelled_count = sum(activity.status == "cancelled" for activity in activities)
    cancellation_rate = (
        round(cancelled_count / total_activities * 100)
        if total_activities > 0
        else 0
    )

    registration_rows = database.execute(
        select(Registration.activity_id, func.count(Registration.id))
        .where(Registration.status == "registered")
        .group_by(Registration.activity_id)
    ).all()
    registration_counts = {
        activity_id: count for activity_id, count in registration_rows
    }
    formation_candidates = [
        activity for activity in activities if activity.status != "cancelled"
    ]
    formed_count = sum(
        registration_counts.get(activity.id, 0) >= activity.min_people
        for activity in formation_candidates
    )
    formation_rate = (
        round(formed_count / len(formation_candidates) * 100)
        if formation_candidates
        else 0
    )

    attendance_rows = database.execute(
        select(AttendanceRecord.attendance_status, func.count(AttendanceRecord.id))
        .group_by(AttendanceRecord.attendance_status)
    ).all()
    attendance_counts = {status: count for status, count in attendance_rows}
    attended_count = attendance_counts.get("attended", 0)
    absent_count = attendance_counts.get("absent", 0)
    attendance_total = attended_count + absent_count
    attendance_rate = (
        round(attended_count / attendance_total * 100)
        if attendance_total > 0
        else 0
    )
    average_rating_value = database.scalar(select(func.avg(ActivityFeedback.rating)))
    average_rating = (
        round(float(average_rating_value), 1)
        if average_rating_value is not None
        else None
    )

    type_rows = database.execute(
        select(Activity.activity_type, func.count(Activity.id))
        .group_by(Activity.activity_type)
        .order_by(func.count(Activity.id).desc(), Activity.activity_type.asc())
    ).all()
    type_counts = [(activity_type, count) for activity_type, count in type_rows]
    max_type_count = max((count for _, count in type_counts), default=1)

    recent_activities = activities[:8]
    recent_activity_ids = [activity.id for activity in recent_activities]
    ownerships = database.scalars(
        select(ActivityOwnership).where(
            ActivityOwnership.activity_id.in_(recent_activity_ids or [-1])
        )
    ).all()
    owner_ids = [ownership.user_id for ownership in ownerships]
    owners = {
        user.id: user
        for user in database.scalars(
            select(User).where(User.id.in_(owner_ids or [-1]))
        ).all()
    }
    creator_by_activity_id = {
        ownership.activity_id: owners.get(ownership.user_id)
        for ownership in ownerships
    }

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "page_title": "平台管理",
            "current_user": current_user,
            "total_users": total_users,
            "total_activities": total_activities,
            "open_count": open_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "cancellation_rate": cancellation_rate,
            "formation_rate": formation_rate,
            "attendance_rate": attendance_rate,
            "attended_count": attended_count,
            "absent_count": absent_count,
            "average_rating": average_rating,
            "type_counts": type_counts,
            "max_type_count": max_type_count,
            "recent_activities": recent_activities,
            "creator_by_activity_id": creator_by_activity_id,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"page_title": "登入", "error": None, "current_user": None},
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: Annotated[EmailStr, Form(max_length=255)],
    password: Annotated[str, Form(pattern=r"^\d{4}$")],
    database: Session = Depends(get_db),
):
    normalized_email = str(email).strip().lower()
    client_ip = request.headers.get("cf-connecting-ip")
    if not client_ip:
        client_ip = request.client.host if request.client is not None else "unknown"

    cutoff = datetime.now() - timedelta(minutes=15)
    database.execute(delete(LoginAttempt).where(LoginAttempt.failed_at < cutoff))
    email_failures = database.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.email == normalized_email,
            LoginAttempt.failed_at >= cutoff,
        )
    ) or 0
    ip_failures = database.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.client_ip == client_ip,
            LoginAttempt.failed_at >= cutoff,
        )
    ) or 0
    if email_failures >= 5 or ip_failures >= 5:
        database.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "page_title": "登入",
                "error": "錯誤次數過多，請等待 15 分鐘後再試。",
                "current_user": None,
            },
            status_code=429,
        )

    member = database.scalar(
        select(User).where(User.email == normalized_email)
    )
    credential = None
    if member is not None:
        credential = database.scalar(
            select(UserCredential).where(UserCredential.user_id == member.id)
        )

    if credential is None or not verify_password(password, credential.password_hash):
        database.add(
            LoginAttempt(
                email=normalized_email,
                client_ip=client_ip,
            )
        )
        database.commit()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "page_title": "登入",
                "error": "Email 或密碼錯誤。",
                "current_user": None,
            },
            status_code=401,
        )

    database.execute(
        delete(LoginAttempt).where(LoginAttempt.email == normalized_email)
    )
    database.commit()
    request.session.clear()
    request.session["user_id"] = member.id
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    current_user = get_current_user(request, database)
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    statement = (
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .options(
            selectinload(Notification.user),
            selectinload(Notification.activity),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    notifications = database.scalars(statement).all()
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={
            "page_title": "站內通知",
            "notifications": notifications,
            "current_user": current_user,
        },
    )


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    current_user = require_current_user(request, database)
    notification = database.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="找不到通知")
    if notification.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="不能操作其他成員的通知")
    notification.is_read = True
    database.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/read-all")
def mark_all_notifications_read(
    request: Request,
    database: Session = Depends(get_db),
):
    current_user = require_current_user(request, database)
    notifications = database.scalars(
        select(Notification).where(
            Notification.user_id == current_user.id,
            Notification.is_read.is_(False),
        )
    ).all()
    for notification in notifications:
        notification.is_read = True
    database.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/activities")
def create_activity(
    request: Request,
    title: Annotated[str, Form(min_length=2, max_length=100)],
    activity_type: Annotated[str, Form(min_length=1, max_length=40)],
    starts_at: Annotated[datetime, Form()],
    location: Annotated[str, Form(min_length=2, max_length=150)],
    min_people: Annotated[int, Form(ge=2, le=100)],
    max_people: Annotated[int, Form(ge=2, le=100)],
    fee: Annotated[int, Form(ge=0, le=100000)] = 0,
    description: Annotated[str, Form(max_length=1000)] = "",
    database: Session = Depends(get_db),
):
    current_user = require_current_user(request, database)
    if max_people < min_people:
        return templates.TemplateResponse(
            request=request,
            name="new_activity.html",
            context={
                "page_title": "建立活動",
                "error": "最高人數不能小於最低成團人數。",
                "current_user": current_user,
            },
            status_code=422,
        )

    activity = Activity(
        title=title.strip(),
        activity_type=activity_type,
        starts_at=starts_at,
        location=location.strip(),
        min_people=min_people,
        max_people=max_people,
        fee=fee,
        description=description.strip(),
    )
    database.add(activity)
    database.flush()
    database.add(
        ActivityOwnership(activity_id=activity.id, user_id=current_user.id)
    )
    database.commit()

    return RedirectResponse(url=f"/activities/{activity.id}", status_code=303)


@app.get("/activities/{activity_id}", response_class=HTMLResponse)
def activity_detail(
    activity_id: int,
    request: Request,
    notice: str | None = None,
    database: Session = Depends(get_db),
) -> HTMLResponse:
    activity = database.get(Activity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="找不到活動")

    current_user = get_current_user(request, database)
    ownership = database.scalar(
        select(ActivityOwnership).where(ActivityOwnership.activity_id == activity_id)
    )
    creator = database.get(User, ownership.user_id) if ownership is not None else None
    is_creator = (
        current_user is not None
        and ownership is not None
        and ownership.user_id == current_user.id
    )
    registrations = database.scalars(
        select(Registration)
        .where(
            Registration.activity_id == activity_id,
            Registration.status != "cancelled",
        )
        .options(selectinload(Registration.user))
        .order_by(Registration.registration_time.asc(), Registration.id.asc())
    ).all()
    confirmed = [item for item in registrations if item.status == "registered"]
    waitlisted = [item for item in registrations if item.status == "waitlisted"]
    current_registration = None
    if current_user is not None:
        current_registration = next(
            (item for item in registrations if item.user_id == current_user.id),
            None,
        )
    attendance_records = database.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.registration_id.in_(
                [item.id for item in confirmed] or [-1]
            )
        )
    ).all()
    attendance_by_registration_id = {
        record.registration_id: record for record in attendance_records
    }
    feedbacks = database.scalars(
        select(ActivityFeedback)
        .where(ActivityFeedback.activity_id == activity_id)
        .order_by(ActivityFeedback.updated_at.desc(), ActivityFeedback.id.desc())
    ).all()
    feedback_user_ids = [feedback.user_id for feedback in feedbacks]
    feedback_users = {
        user.id: user
        for user in database.scalars(
            select(User).where(User.id.in_(feedback_user_ids or [-1]))
        ).all()
    }
    average_rating = (
        round(sum(feedback.rating for feedback in feedbacks) / len(feedbacks), 1)
        if feedbacks
        else None
    )
    current_feedback = None
    can_review = False
    if current_user is not None:
        current_feedback = next(
            (feedback for feedback in feedbacks if feedback.user_id == current_user.id),
            None,
        )
        if current_registration is not None:
            attendance = attendance_by_registration_id.get(current_registration.id)
            can_review = (
                activity.status == "completed"
                and attendance is not None
                and attendance.attendance_status == "attended"
            )

    if activity.status == "completed":
        capacity_status = "已完成"
    elif activity.status == "cancelled":
        capacity_status = "已取消"
    elif len(confirmed) >= activity.max_people:
        capacity_status = "額滿"
    elif len(confirmed) >= activity.min_people:
        capacity_status = "已成團"
    else:
        capacity_status = "招募中"

    return templates.TemplateResponse(
        request=request,
        name="activity_detail.html",
        context={
            "page_title": activity.title,
            "activity": activity,
            "current_user": current_user,
            "current_registration": current_registration,
            "creator": creator,
            "is_creator": is_creator,
            "attendance_by_registration_id": attendance_by_registration_id,
            "feedbacks": feedbacks,
            "feedback_users": feedback_users,
            "average_rating": average_rating,
            "current_feedback": current_feedback,
            "can_review": can_review,
            "confirmed": confirmed,
            "waitlisted": waitlisted,
            "capacity_status": capacity_status,
            "notice": NOTICE_MESSAGES.get(notice),
        },
    )


def require_activity_creator(
    activity_id: int,
    request: Request,
    database: Session,
) -> tuple[Activity, User]:
    current_user = require_current_user(request, database)
    activity = database.get(Activity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    ownership = database.scalar(
        select(ActivityOwnership).where(ActivityOwnership.activity_id == activity_id)
    )
    if ownership is None or ownership.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有活動建立者可以執行此操作")
    return activity, current_user


@app.get("/activities/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity_page(
    activity_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    activity, current_user = require_activity_creator(activity_id, request, database)
    if activity.status != "open":
        raise HTTPException(status_code=409, detail="已取消的活動不能編輯")
    return templates.TemplateResponse(
        request=request,
        name="edit_activity.html",
        context={
            "page_title": "編輯活動",
            "activity": activity,
            "error": None,
            "current_user": current_user,
        },
    )


@app.post("/activities/{activity_id}/edit", response_class=HTMLResponse)
def edit_activity(
    activity_id: int,
    request: Request,
    title: Annotated[str, Form(min_length=2, max_length=100)],
    activity_type: Annotated[str, Form(min_length=1, max_length=40)],
    starts_at: Annotated[datetime, Form()],
    location: Annotated[str, Form(min_length=2, max_length=150)],
    min_people: Annotated[int, Form(ge=2, le=100)],
    max_people: Annotated[int, Form(ge=2, le=100)],
    fee: Annotated[int, Form(ge=0, le=100000)] = 0,
    description: Annotated[str, Form(max_length=1000)] = "",
    database: Session = Depends(get_db),
):
    current_user_id = require_session_user_id(request)
    activity = lock_activity_for_write(database, activity_id)
    current_user = database.get(User, current_user_id)
    ownership = database.scalar(
        select(ActivityOwnership).where(ActivityOwnership.activity_id == activity_id)
    )
    if current_user is None or ownership is None or ownership.user_id != current_user_id:
        raise HTTPException(status_code=403, detail="只有活動建立者可以執行此操作")
    if activity.status != "open":
        raise HTTPException(status_code=409, detail="已取消的活動不能編輯")

    confirmed_count = database.scalar(
        select(func.count(Registration.id)).where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
    ) or 0
    error = None
    if max_people < min_people:
        error = "最高人數不能小於最低成團人數。"
    elif max_people < confirmed_count:
        error = f"目前已有 {confirmed_count} 位正式成員，最高人數不能低於此數字。"

    if error is not None:
        return templates.TemplateResponse(
            request=request,
            name="edit_activity.html",
            context={
                "page_title": "編輯活動",
                "activity": activity,
                "error": error,
                "current_user": current_user,
            },
            status_code=422,
        )

    activity.title = title.strip()
    activity.activity_type = activity_type
    activity.starts_at = starts_at
    activity.location = location.strip()
    activity.min_people = min_people
    activity.max_people = max_people
    activity.fee = fee
    activity.description = description.strip()
    database.commit()
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice=updated",
        status_code=303,
    )


@app.post("/activities/{activity_id}/cancel")
def cancel_activity(
    activity_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    current_user_id = require_session_user_id(request)
    activity = lock_activity_for_write(database, activity_id)
    ownership = database.scalar(
        select(ActivityOwnership).where(ActivityOwnership.activity_id == activity_id)
    )
    if ownership is None or ownership.user_id != current_user_id:
        raise HTTPException(status_code=403, detail="只有活動建立者可以執行此操作")
    if activity.status == "cancelled":
        return RedirectResponse(url=f"/activities/{activity_id}", status_code=303)

    active_registrations = database.scalars(
        select(Registration).where(
            Registration.activity_id == activity_id,
            Registration.status != "cancelled",
        )
    ).all()
    activity.status = "cancelled"
    for registration in active_registrations:
        registration.status = "cancelled"
        registration.cancel_time = datetime.now()
        add_notification(
            database,
            user_id=registration.user_id,
            activity_id=activity_id,
            message=f"「{activity.title}」已由建立者取消。",
        )

    database.commit()
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice=activity_cancelled",
        status_code=303,
    )


@app.get("/activities/{activity_id}/attendance", response_class=HTMLResponse)
def attendance_page(
    activity_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    activity, current_user = require_activity_creator(activity_id, request, database)
    if activity.status == "cancelled":
        raise HTTPException(status_code=409, detail="已取消的活動不能記錄出席")
    registrations = database.scalars(
        select(Registration)
        .where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
        .options(selectinload(Registration.user))
        .order_by(Registration.registration_time.asc(), Registration.id.asc())
    ).all()
    records = database.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.registration_id.in_(
                [item.id for item in registrations] or [-1]
            )
        )
    ).all()
    record_map = {record.registration_id: record for record in records}
    return templates.TemplateResponse(
        request=request,
        name="attendance.html",
        context={
            "page_title": "完成活動與出席紀錄",
            "activity": activity,
            "registrations": registrations,
            "record_map": record_map,
            "error": None,
            "current_user": current_user,
        },
    )


@app.post("/activities/{activity_id}/attendance", response_class=HTMLResponse)
async def complete_activity_with_attendance(
    activity_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    current_user_id = require_session_user_id(request)
    form_data = await request.form()
    activity = lock_activity_for_write(database, activity_id)
    current_user = database.get(User, current_user_id)
    ownership = database.scalar(
        select(ActivityOwnership).where(ActivityOwnership.activity_id == activity_id)
    )
    if current_user is None or ownership is None or ownership.user_id != current_user_id:
        raise HTTPException(status_code=403, detail="只有活動建立者可以記錄出席")
    if activity.status == "cancelled":
        raise HTTPException(status_code=409, detail="已取消的活動不能記錄出席")

    registrations = database.scalars(
        select(Registration)
        .where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
        .options(selectinload(Registration.user))
        .order_by(Registration.registration_time.asc(), Registration.id.asc())
    ).all()
    submitted_statuses: dict[int, str] = {}
    for registration in registrations:
        status_value = form_data.get(f"attendance_{registration.id}")
        if status_value not in {"attended", "absent"}:
            existing_records = database.scalars(
                select(AttendanceRecord).where(
                    AttendanceRecord.registration_id.in_(
                        [item.id for item in registrations] or [-1]
                    )
                )
            ).all()
            return templates.TemplateResponse(
                request=request,
                name="attendance.html",
                context={
                    "page_title": "完成活動與出席紀錄",
                    "activity": activity,
                    "registrations": registrations,
                    "record_map": {
                        record.registration_id: record for record in existing_records
                    },
                    "error": "請為每位正式參加者選擇有出席或未出席。",
                    "current_user": current_user,
                },
                status_code=422,
            )
        submitted_statuses[registration.id] = str(status_value)

    for registration in registrations:
        status_value = submitted_statuses[registration.id]
        record = database.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.registration_id == registration.id
            )
        )
        if record is None:
            database.add(
                AttendanceRecord(
                    registration_id=registration.id,
                    attendance_status=status_value,
                    marked_by_user_id=current_user_id,
                )
            )
        else:
            record.attendance_status = status_value
            record.marked_by_user_id = current_user_id
            record.marked_at = datetime.now()

        result_text = "有出席" if status_value == "attended" else "未出席"
        add_notification(
            database,
            user_id=registration.user_id,
            activity_id=activity_id,
            message=f"「{activity.title}」已完成，你的出席紀錄為：{result_text}。",
        )

    waitlisted = database.scalars(
        select(Registration).where(
            Registration.activity_id == activity_id,
            Registration.status == "waitlisted",
        )
    ).all()
    for registration in waitlisted:
        registration.status = "cancelled"
        registration.cancel_time = datetime.now()
        add_notification(
            database,
            user_id=registration.user_id,
            activity_id=activity_id,
            message=f"「{activity.title}」已完成，本次候補未轉為正式名單。",
        )

    activity.status = "completed"
    database.commit()
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice=activity_completed",
        status_code=303,
    )


@app.post("/activities/{activity_id}/feedback")
def save_activity_feedback(
    activity_id: int,
    request: Request,
    rating: Annotated[int, Form(ge=1, le=5)],
    comment: Annotated[str, Form(max_length=500)] = "",
    database: Session = Depends(get_db),
):
    current_user = require_current_user(request, database)
    activity = database.get(Activity, activity_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="找不到活動")
    if activity.status != "completed":
        raise HTTPException(status_code=409, detail="活動完成後才能評價")

    registration = database.scalar(
        select(Registration).where(
            Registration.activity_id == activity_id,
            Registration.user_id == current_user.id,
            Registration.status == "registered",
        )
    )
    if registration is None:
        raise HTTPException(status_code=403, detail="只有正式參加者能評價")
    attendance = database.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.registration_id == registration.id,
            AttendanceRecord.attendance_status == "attended",
        )
    )
    if attendance is None:
        raise HTTPException(status_code=403, detail="只有有出席的成員能評價")

    feedback = database.scalar(
        select(ActivityFeedback).where(
            ActivityFeedback.activity_id == activity_id,
            ActivityFeedback.user_id == current_user.id,
        )
    )
    if feedback is None:
        database.add(
            ActivityFeedback(
                activity_id=activity_id,
                user_id=current_user.id,
                rating=rating,
                comment=comment.strip(),
            )
        )
    else:
        feedback.rating = rating
        feedback.comment = comment.strip()
        feedback.updated_at = datetime.now()
    database.commit()
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice=feedback_saved",
        status_code=303,
    )


@app.post("/activities/{activity_id}/registrations")
def register_member(
    activity_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    user_id = require_session_user_id(request)
    activity = lock_activity_for_write(database, activity_id)
    member = database.get(User, user_id)
    if member is None:
        raise HTTPException(status_code=401, detail="登入狀態已失效")
    if activity.status != "open":
        raise HTTPException(status_code=409, detail="活動目前無法報名")

    existing = database.scalar(
        select(Registration).where(
            Registration.activity_id == activity_id,
            Registration.user_id == user_id,
        )
    )
    if existing is not None and existing.status != "cancelled":
        return RedirectResponse(
            url=f"/activities/{activity_id}?notice=already_joined",
            status_code=303,
        )

    confirmed_count = database.scalar(
        select(func.count(Registration.id)).where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
    ) or 0
    new_status = "registered" if confirmed_count < activity.max_people else "waitlisted"

    if existing is None:
        registration = Registration(
            activity_id=activity_id,
            user_id=user_id,
            status=new_status,
        )
        database.add(registration)
    else:
        existing.status = new_status
        existing.registration_time = datetime.now()
        existing.cancel_time = None

    if new_status == "registered":
        add_notification(
            database,
            user_id=user_id,
            activity_id=activity_id,
            message=f"你已成功報名「{activity.title}」。",
        )
    else:
        add_notification(
            database,
            user_id=user_id,
            activity_id=activity_id,
            message=f"「{activity.title}」目前額滿，你已加入候補名單。",
        )

    if new_status == "registered" and confirmed_count < activity.min_people <= confirmed_count + 1:
        database.flush()
        participant_ids = database.scalars(
            select(Registration.user_id).where(
                Registration.activity_id == activity_id,
                Registration.status == "registered",
            )
        ).all()
        for participant_id in participant_ids:
            add_notification(
                database,
                user_id=participant_id,
                activity_id=activity_id,
                message=f"「{activity.title}」已達最低人數，活動成團！",
            )

    database.commit()
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice={new_status}",
        status_code=303,
    )


@app.post("/activities/{activity_id}/registrations/{registration_id}/cancel")
def cancel_registration(
    activity_id: int,
    registration_id: int,
    request: Request,
    database: Session = Depends(get_db),
):
    current_user_id = require_session_user_id(request)
    activity = lock_activity_for_write(database, activity_id)
    current_user = database.get(User, current_user_id)
    if current_user is None:
        raise HTTPException(status_code=401, detail="登入狀態已失效")
    registration = database.scalar(
        select(Registration).where(
            Registration.id == registration_id,
            Registration.activity_id == activity_id,
        )
    )
    if registration is None:
        raise HTTPException(status_code=404, detail="找不到報名紀錄")
    if registration.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="不能取消其他成員的報名")

    if registration.status == "cancelled":
        return RedirectResponse(url=f"/activities/{activity_id}", status_code=303)

    confirmed_before = database.scalar(
        select(func.count(Registration.id)).where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
    ) or 0
    should_promote = registration.status == "registered"
    registration.status = "cancelled"
    registration.cancel_time = datetime.now()
    add_notification(
        database,
        user_id=registration.user_id,
        activity_id=activity_id,
        message=f"你已取消「{activity.title}」的報名。",
    )

    promoted = None
    if should_promote:
        promoted = database.scalar(
            select(Registration)
            .where(
                Registration.activity_id == activity_id,
                Registration.status == "waitlisted",
            )
            .order_by(Registration.registration_time.asc(), Registration.id.asc())
        )
        if promoted is not None:
            promoted.status = "registered"
            add_notification(
                database,
                user_id=promoted.user_id,
                activity_id=activity_id,
                message=f"「{activity.title}」有名額釋出，你已從候補轉為正式名單。",
            )

    database.flush()
    confirmed_after = database.scalar(
        select(func.count(Registration.id)).where(
            Registration.activity_id == activity_id,
            Registration.status == "registered",
        )
    ) or 0
    if confirmed_before >= activity.min_people and confirmed_after < activity.min_people:
        remaining_user_ids = database.scalars(
            select(Registration.user_id).where(
                Registration.activity_id == activity_id,
                Registration.status == "registered",
            )
        ).all()
        for remaining_user_id in remaining_user_ids:
            add_notification(
                database,
                user_id=remaining_user_id,
                activity_id=activity_id,
                message=f"「{activity.title}」目前低於最低成團人數，等待其他成員加入。",
            )

    database.commit()
    notice = "cancelled_promoted" if promoted is not None else "cancelled"
    return RedirectResponse(
        url=f"/activities/{activity_id}?notice={notice}",
        status_code=303,
    )


@app.get("/api/activities", response_model=list[ActivityRead])
def list_activities(database: Session = Depends(get_db)) -> list[Activity]:
    return list(
        database.scalars(select(Activity).order_by(Activity.starts_at.asc())).all()
    )


@app.get("/api/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "JoinMate"}
