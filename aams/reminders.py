"""截止前 Discord 提醒（發到學員公告頻道）。

- 預設提醒時間：管理員在「管理後台 › 系統」設定（未設定 = "2,1"，截止前 2 小時、1 小時）；
  .env 填了 AAMS_REMINDER_HOURS 則以 .env 為準、網頁鎖住。
- 每份作業可自訂（例如 "3,1,0.5"）或關閉（"off"）；講師／管理員在作業設定修改。
- 只提醒「截止時間」，不提醒最終收件期限。
- 不發的情況：草稿作業、課程未發布、收件已關閉、已過截止時間。
- 發送紀錄存在資料庫（reminder_logs），伺服器重啟不會重發；截止時間修改後依新時間重新計算。
- 伺服器停機錯過時，重新開機後只補發「最近的那一次」，不會一次跳出好幾則。
- 不列出誰還沒交（避免公開點名）。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import notify
from .db import SessionLocal
from .models import Assignment, Course, DiscordRole, ReminderLog
from .utils import fmt_dt, utcnow

log = logging.getLogger("aams.reminders")

MAX_ITEMS = 5
MAX_HOURS = 168  # 一週

_last_run = 0.0


def parse_hours(raw: str) -> list[float]:
    """'2, 1, 0.5' → [2.0, 1.0, 0.5]（由大到小、去重）。格式錯誤時丟出 ValueError。"""
    out: set[float] = set()
    for part in (raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = float(part)
        except ValueError:
            raise ValueError(f"「{part}」不是數字") from None
        if not (0 < h <= MAX_HOURS) or math.isnan(h):
            raise ValueError(f"提醒時間需介於 0–{MAX_HOURS} 小時")
        out.add(round(h, 2))
    if len(out) > MAX_ITEMS:
        raise ValueError(f"最多設定 {MAX_ITEMS} 個提醒時間")
    return sorted(out, reverse=True)


def hours_key(h: float) -> str:
    return f"{h:g}"


def hours_label(h: float) -> str:
    minutes = round(h * 60)
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時"
    if minutes < 60:
        return f"{minutes} 分鐘"
    return f"{minutes // 60} 小時 {minutes % 60} 分"


def default_hours() -> list[float]:
    from . import sysconfig

    try:
        return parse_hours(sysconfig.reminder_hours_raw())
    except ValueError:
        log.warning("截止提醒系統預設格式錯誤，改用 2,1")
        return [2.0, 1.0]


def effective_hours(a: Assignment) -> list[float]:
    if a.reminder_hours is None:
        return default_hours()
    if a.reminder_hours == "off":
        return []
    try:
        return parse_hours(a.reminder_hours)
    except ValueError:
        return []


def role_of(db, a_or_ann) -> DiscordRole | None:
    rid = getattr(a_or_ann, "discord_role_id", None)
    return db.get(DiscordRole, rid) if rid else None


def message(course: Course, a: Assignment, remaining: timedelta) -> str:
    minutes = max(1, math.ceil(remaining.total_seconds() / 60))
    left = hours_label(minutes / 60)
    return (f"⏰ 【{course.title}】{a.title} 將在 {left}後截止（{fmt_dt(a.due_at, False)}，台北時間）。"
            f"還沒交的同學請盡快到作業繳交系統上傳！")


@dataclass
class Slot:
    hours: float
    at: datetime
    state: str  # PENDING | SENT | SKIPPED | PAST


def reason_not_sent(db, a: Assignment) -> str:
    """作業不會發提醒的原因（空字串 = 會發）。"""
    course = db.get(Course, a.course_id)
    if a.status != "PUBLISHED":
        return "作業是草稿"
    if course is None or course.status != "PUBLISHED":
        return "課程未發布"
    if not a.accepting_submissions:
        return "收件已關閉"
    return ""


def schedule(db, a: Assignment, now: datetime | None = None) -> list[Slot]:
    """這份作業目前截止時間下的提醒排程（給頁面預覽用）。"""
    now = now or utcnow()
    logs = {r.hours: r for r in db.scalars(select(ReminderLog).where(
        ReminderLog.assignment_id == a.id, ReminderLog.due_version == a.due_version))}
    out = []
    for h in effective_hours(a):
        at = a.due_at - timedelta(hours=h)
        r = logs.get(hours_key(h))
        if r is not None:
            state = "SENT" if r.sent else "SKIPPED"
        elif now >= a.due_at:
            state = "PAST"
        else:
            state = "PENDING"
        out.append(Slot(h, at, state))
    return out


def run_once(db=None, now: datetime | None = None) -> list[str]:
    """檢查所有作業，發送到期的提醒；回傳這次發出的訊息。"""
    own = db is None
    db = db or SessionLocal()
    now = now or utcnow()
    sent: list[str] = []
    try:
        for a in db.scalars(select(Assignment).where(Assignment.status == "PUBLISHED", Assignment.due_at > now)):
            if reason_not_sent(db, a):
                continue
            hours = effective_hours(a)
            if not hours:
                continue
            done = set(db.scalars(select(ReminderLog.hours).where(
                ReminderLog.assignment_id == a.id, ReminderLog.due_version == a.due_version)))
            reached = [h for h in hours if now >= a.due_at - timedelta(hours=h) and hours_key(h) not in done]
            if not reached:
                continue
            closest = min(reached)  # 錯過多次時只補發最近的一次
            for h in reached:
                db.add(ReminderLog(assignment_id=a.id, due_version=a.due_version, hours=hours_key(h),
                                   sent=(h == closest)))
            try:
                db.commit()  # 先記錄再發送：多個 worker 同時跑時只有一個會成功寫入
            except IntegrityError:
                db.rollback()
                continue
            course = db.get(Course, a.course_id)
            role = role_of(db, a)
            text = message(course, a, a.due_at - now)
            notify.send("student", text, [role.role_id] if role else None)
            sent.append(text)
    finally:
        if own:
            db.close()
    return sent


def maybe_run(interval: int = 30) -> None:
    global _last_run
    if time.time() - _last_run < interval:
        return
    _last_run = time.time()
    try:
        run_once()
    except Exception:  # noqa: BLE001
        log.exception("reminder check failed")
