"""Discord 設定（管理員與講師）：登記可 @ 的身分組、預覽即將發送的截止提醒、送測試訊息。"""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from .. import acl, notify, reminders, sysconfig
from ..deps import Ctx, audit, render, require_csrf, staff_ctx
from ..models import Announcement, Assignment, Course, DiscordRole, User
from ..utils import clean_text, utcnow

router = APIRouter(prefix="/staff")

ROLE_ID_RE = re.compile(r"\d{15,21}")


def _discord_ctx(ctx: Ctx = Depends(staff_ctx)) -> Ctx:
    if not (ctx.is_admin or ctx.is_instructor):
        raise HTTPException(403, "Discord 設定僅限講師與管理員")
    return ctx


def roles(db) -> list[DiscordRole]:
    return list(db.scalars(select(DiscordRole).order_by(DiscordRole.label)))


def parse_role_choice(db, raw: str) -> int | None:
    """表單的身分組選項 → DiscordRole.id；空白 = 不提及。"""
    if not raw:
        return None
    try:
        rid = int(raw)
    except ValueError:
        raise HTTPException(400, "身分組選項錯誤")
    if db.get(DiscordRole, rid) is None:
        raise HTTPException(400, "身分組不存在")
    return rid


@router.get("/discord")
def discord_page(request: Request, ctx: Ctx = Depends(_discord_ctx)):
    db = ctx.db
    managed = acl.managed_course_ids(ctx)
    now = utcnow()
    upcoming = []
    for a in db.scalars(select(Assignment).where(Assignment.due_at > now).order_by(Assignment.due_at)):
        if managed is not None and a.course_id not in managed:
            continue
        course = db.get(Course, a.course_id)
        role = reminders.role_of(db, a)
        hours = reminders.effective_hours(a)
        upcoming.append({"a": a, "course": course, "slots": reminders.schedule(db, a, now),
                         "blocked": reminders.reason_not_sent(db, a), "role": role,
                         "preview": reminders.message(course, a, timedelta(hours=min(hours))) if hours else ""})
    creators = {r.created_by: db.get(User, r.created_by) for r in roles(db)}
    return render(request, "staff/discord.html", ctx, roles=roles(db), creators=creators, upcoming=upcoming,
                  staff_ok=notify.configured("staff"), student_ok=notify.configured("student"),
                  default_hours=", ".join(reminders.hours_label(h) for h in reminders.default_hours()),
                  reminder_source=sysconfig.reminder_source(), hours_label=reminders.hours_label)


@router.post("/discord/roles")
def role_add(label: str = Form(""), role_id: str = Form(""), csrf_token: str = Form(""),
             ctx: Ctx = Depends(_discord_ctx)):
    require_csrf(ctx.sess, csrf_token)
    label = clean_text(label, 60).lstrip("@")
    role_id = (role_id or "").strip().removeprefix("<@&").removesuffix(">")
    if not label:
        raise HTTPException(400, "請輸入顯示名稱，例如「學員」")
    if not ROLE_ID_RE.fullmatch(role_id):
        raise HTTPException(400, "身分組 ID 應為 15–21 位數字（在 Discord 對身分組按右鍵 →「複製身分組 ID」）")
    if ctx.db.scalar(select(DiscordRole).where(DiscordRole.role_id == role_id)):
        raise HTTPException(409, "這個身分組 ID 已經登記過了")
    r = DiscordRole(label=label, role_id=role_id, created_by=ctx.user.id)
    ctx.db.add(r)
    ctx.db.flush()
    audit(ctx.db, ctx.user.id, "discord.role_add", "discord_role", r.id, {"label": label, "role_id": role_id}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/staff/discord?msg=created", 303)


@router.post("/discord/roles/{rid}/delete")
def role_delete(rid: int, csrf_token: str = Form(""), ctx: Ctx = Depends(_discord_ctx)):
    require_csrf(ctx.sess, csrf_token)
    r = ctx.db.get(DiscordRole, rid)
    if r is None:
        raise HTTPException(404)
    # 用到這個身分組的作業／公告改為「不提及」
    ctx.db.execute(update(Assignment).where(Assignment.discord_role_id == rid).values(discord_role_id=None))
    ctx.db.execute(update(Announcement).where(Announcement.discord_role_id == rid).values(discord_role_id=None))
    audit(ctx.db, ctx.user.id, "discord.role_delete", "discord_role", rid, {"label": r.label, "role_id": r.role_id}, ctx.ip)
    ctx.db.delete(r)
    ctx.db.commit()
    return RedirectResponse("/staff/discord?msg=deleted", 303)


@router.post("/discord/test")
def discord_test(channel: str = Form("student"), role: str = Form(""), csrf_token: str = Form(""),
                 ctx: Ctx = Depends(_discord_ctx)):
    """立即送出一則測試訊息（可選擇 @ 一個身分組，確認對方真的會收到通知）。"""
    require_csrf(ctx.sess, csrf_token)
    if channel not in notify.CHANNELS or not notify.configured(channel):
        raise HTTPException(400, "這個頻道尚未設定 Webhook（請管理員在「管理後台 › 系統」設定）")
    if channel == "staff" and not ctx.is_admin:
        raise HTTPException(403, "只有管理員可以測試 staff 頻道")
    rid = parse_role_choice(ctx.db, role)
    r = ctx.db.get(DiscordRole, rid) if rid else None
    ok = notify.deliver(channel, f"✅ 作業繳交系統測試訊息（{channel} 頻道），由 {ctx.user.name} 發送。",
                        [r.role_id] if r else None)
    audit(ctx.db, ctx.user.id, "discord.test", "discord", channel, {"ok": ok, "role": r.role_id if r else ""}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/staff/discord?msg=" + ("discord_ok" if ok else "discord_fail"), 303)
