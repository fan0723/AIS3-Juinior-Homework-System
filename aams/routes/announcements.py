"""公告欄：全營公告（管理員）與課程公告（該課講師／管理員）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import acl, notify
from ..deps import Ctx, audit, get_ctx, render, require_csrf
from ..models import Announcement, Course, DiscordRole, User
from ..utils import clean_text, utcnow

router = APIRouter()


# ---------------------------------------------------------------- helpers
def _published_course_ids(db: Session) -> set[int]:
    return set(db.scalars(select(Course.id).where(Course.status == "PUBLISHED")))


def visible_announcements(ctx: Ctx, course_id: int | None = None, only_global: bool = False,
                          include_hidden: bool = False) -> list[Announcement]:
    """依使用者權限過濾；置頂優先、新的在前。"""
    db = ctx.db
    stmt = select(Announcement)
    if only_global:
        stmt = stmt.where(Announcement.course_id.is_(None))
    elif course_id is not None:
        stmt = stmt.where(Announcement.course_id == course_id)
    rows = list(db.scalars(stmt.order_by(Announcement.pinned.desc(), Announcement.created_at.desc())))
    published = _published_course_ids(db)
    managed = acl.managed_course_ids(ctx)
    out = []
    for a in rows:
        manage = can_manage(ctx, a, managed)
        if a.course_id is not None and a.course_id not in published and not manage:
            continue
        if a.status != "PUBLISHED" and not (include_hidden and manage):
            continue
        out.append(a)
    return out


def can_manage(ctx: Ctx, ann: Announcement, managed: set[int] | None = ...) -> bool:
    if ctx.is_admin:
        return True
    if ann.course_id is None:
        return False
    managed = acl.managed_course_ids(ctx) if managed is ... else managed
    return managed is None or ann.course_id in managed


def postable_courses(ctx: Ctx) -> list[Course]:
    managed = acl.managed_course_ids(ctx)
    courses = list(ctx.db.scalars(select(Course).order_by(Course.ordinal, Course.id)))
    return [c for c in courses if managed is None or c.id in managed]


def can_post(ctx: Ctx) -> bool:
    return ctx.is_admin or (ctx.is_instructor and bool(postable_courses(ctx)))


def unread_count(db: Session, user: User) -> int:
    published = _published_course_ids(db)
    stmt = select(func.count()).select_from(Announcement).where(
        Announcement.status == "PUBLISHED",
        or_(Announcement.course_id.is_(None), Announcement.course_id.in_(published or {-1})),
    )
    if user.announcements_seen_at is not None:
        stmt = stmt.where(Announcement.created_at > user.announcements_seen_at)
    return db.scalar(stmt) or 0


def _push(db: Session, ann: Announcement) -> None:
    """公開的公告第一次發布時推送到學員 Discord 頻道（只推一次）。"""
    if ann.status != "PUBLISHED" or ann.pushed_at is not None:
        return
    course = db.get(Course, ann.course_id) if ann.course_id else None
    if course is not None and course.status != "PUBLISHED":
        return
    where = f"【{course.title}】" if course else "【全營公告】"
    body = ann.body if len(ann.body) <= 400 else ann.body[:400] + "…"
    role = db.get(DiscordRole, ann.discord_role_id) if ann.discord_role_id else None
    if notify.send("student", f"📢 {where}{ann.title}\n{body}\n（詳見作業繳交系統公告欄）",
                   [role.role_id] if role else None):
        ann.pushed_at = utcnow()


def _load(ctx: Ctx, ann_id: int) -> Announcement:
    ann = ctx.db.get(Announcement, ann_id)
    if ann is None or not can_manage(ctx, ann):
        raise HTTPException(404)
    return ann


def _parse_form(ctx: Ctx, course_id: str, title: str, body: str) -> tuple[int | None, str, str]:
    title = clean_text(title, 200)
    body = clean_text(body, 10000, multiline=True)
    if not title:
        raise HTTPException(400, "請輸入公告標題")
    if course_id in ("", "0", "global"):
        if not ctx.is_admin:
            raise HTTPException(403, "只有管理員可以發布全營公告")
        return None, title, body
    try:
        cid = int(course_id)
    except ValueError:
        raise HTTPException(400, "課程錯誤")
    if ctx.db.get(Course, cid) is None or not acl.can_manage_course(ctx, cid):
        raise HTTPException(403, "只能在自己負責的課程發布公告")
    return cid, title, body


def _roles(ctx: Ctx):
    from .discord import roles

    return roles(ctx.db)


def parse_role_choice(db, raw: str):
    from .discord import parse_role_choice as _p

    return _p(db, raw)


# ---------------------------------------------------------------- pages
@router.get("/announcements")
def announcements_page(request: Request, ctx: Ctx = Depends(get_ctx)):
    items = visible_announcements(ctx, include_hidden=True)
    courses = {c.id: c for c in ctx.db.scalars(select(Course))}
    seen = ctx.user.announcements_seen_at
    ctx.user.announcements_seen_at = utcnow()
    ctx.db.commit()
    return render(request, "announcements.html", ctx, items=items, courses=courses, seen=seen,
                  can_post=can_post(ctx), manageable={a.id for a in items if can_manage(ctx, a)})


@router.get("/announcements/new")
def announcement_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    if not can_post(ctx):
        raise HTTPException(403, "只有講師與管理員可以發布公告")
    pre = request.query_params.get("course_id", "")
    return render(request, "announcement_form.html", ctx, ann=None, courses=postable_courses(ctx), preset=pre,
                  discord_roles=_roles(ctx))


@router.post("/announcements/new")
def announcement_create(course_id: str = Form(""), title: str = Form(""), body: str = Form(""), pinned: str = Form(""),
                        status: str = Form("PUBLISHED"), discord_role: str = Form(""), csrf_token: str = Form(""),
                        ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    if not can_post(ctx):
        raise HTTPException(403, "只有講師與管理員可以發布公告")
    cid, title, body = _parse_form(ctx, course_id, title, body)
    ann = Announcement(course_id=cid, title=title, body=body, pinned=pinned == "1",
                       status="HIDDEN" if status == "HIDDEN" else "PUBLISHED", author_id=ctx.user.id,
                       discord_role_id=parse_role_choice(ctx.db, discord_role))
    ctx.db.add(ann)
    ctx.db.flush()
    _push(ctx.db, ann)
    audit(ctx.db, ctx.user.id, "announcement.create", "announcement", ann.id,
          {"course": cid, "status": ann.status, "pinned": ann.pinned}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/announcements?msg=created", 303)


@router.get("/announcements/{ann_id}/edit")
def announcement_edit_form(ann_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    ann = _load(ctx, ann_id)
    return render(request, "announcement_form.html", ctx, ann=ann, courses=postable_courses(ctx),
                  preset=str(ann.course_id or "global"), discord_roles=_roles(ctx))


@router.post("/announcements/{ann_id}/edit")
def announcement_update(ann_id: int, title: str = Form(""), body: str = Form(""), pinned: str = Form(""),
                        status: str = Form("PUBLISHED"), discord_role: str = Form(""), csrf_token: str = Form(""),
                        ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    ann = _load(ctx, ann_id)
    _cid, ann.title, ann.body = _parse_form(ctx, str(ann.course_id or "global"), title, body)
    ann.pinned = pinned == "1"
    ann.status = "HIDDEN" if status == "HIDDEN" else "PUBLISHED"
    ann.discord_role_id = parse_role_choice(ctx.db, discord_role)
    ann.updated_at = utcnow()
    _push(ctx.db, ann)
    audit(ctx.db, ctx.user.id, "announcement.update", "announcement", ann.id,
          {"status": ann.status, "pinned": ann.pinned}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/announcements?msg=saved", 303)


@router.post("/announcements/{ann_id}/delete")
def announcement_delete(ann_id: int, csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    ann = _load(ctx, ann_id)
    audit(ctx.db, ctx.user.id, "announcement.delete", "announcement", ann.id, {"title": ann.title}, ctx.ip)
    ctx.db.delete(ann)
    ctx.db.commit()
    return RedirectResponse("/announcements?msg=deleted", 303)
