"""批次下載：所有角色都能用，但只能勾選到自己有權限看的提交。

學員：自己（或自己小組）的提交 · 助教：帶的組＋被指派批改的組 · 講師：負責課程 · 管理員：全部
"""
from __future__ import annotations

import threading
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from .. import acl, services
from ..config import settings
from ..deps import Ctx, audit, get_ctx, render, require_csrf
from ..models import Assignment, Course
from ..zipbuild import ExportTooLarge, build_zip

router = APIRouter()

_slots = threading.BoundedSemaphore(max(1, settings.download_max_concurrent))


def unit_key(u: services.Unit) -> str:
    return f"g{u.group_id}" if u.student_id is None else f"s{u.student_id}"


def allowed_units(ctx: Ctx, a: Assignment) -> list[services.Unit]:
    """依角色決定可下載的繳交單位（伺服器端判斷，絕不信任表單）。"""
    db = ctx.db
    if ctx.is_staff:
        units = services.assignment_units(db, a, acl.visible_group_ids_for_assignment(ctx, a))
        return [u for u in units if u.submission is None or acl.can_view_submission(ctx, u.submission, a)]
    if not ctx.is_student:
        return []
    if a.submission_mode == "GROUP":
        g = acl.active_group_of(db, ctx.user.id)
        if g is None:
            return []
        return services.assignment_units(db, a, {g.id})
    return [u for u in services.assignment_units(db, a, None) if u.student_id == ctx.user.id]


def downloadable_assignments(ctx: Ctx) -> list[tuple[Assignment, Course]]:
    db = ctx.db
    out = []
    courses = {c.id: c for c in db.scalars(select(Course))}
    for a in db.scalars(select(Assignment).order_by(Assignment.course_id, Assignment.due_at)):
        if not acl.assignment_visible(ctx, a):
            continue
        out.append((a, courses[a.course_id]))
    return out


@router.get("/downloads")
def downloads_page(request: Request, ctx: Ctx = Depends(get_ctx)):
    assignments = downloadable_assignments(ctx)
    sel = None
    units = []
    raw = request.query_params.get("assignment_id", "")
    if raw.isdigit():
        a = ctx.db.get(Assignment, int(raw))
        if a is None or not acl.assignment_visible(ctx, a):
            raise HTTPException(404)
        sel = a
        units = allowed_units(ctx, a)
    return render(request, "downloads.html", ctx, assignments=assignments, sel=sel, units=units, unit_key=unit_key,
                  course=ctx.db.get(Course, sel.course_id) if sel else None)


@router.post("/downloads/zip")
def downloads_zip(assignment_id: int = Form(...), units: list[str] = Form([]), versions: str = Form("latest"),
                  force_naming: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None or not acl.assignment_visible(ctx, a):
        raise HTTPException(404)
    wanted = set(units)
    if not wanted:
        raise HTTPException(400, "請至少勾選一個組別或學員")
    allowed = {unit_key(u): u for u in allowed_units(ctx, a)}
    if not wanted <= set(allowed):  # 表單送來沒有權限的單位 → 整次拒絕
        raise HTTPException(403, "包含沒有權限下載的組別或學員")
    selected = [u for k, u in allowed.items() if k in wanted]
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "目前有多人同時在產生壓縮檔，請稍等一下再試")
    try:
        try:
            path, stats = build_zip(db, a, selected, all_versions=versions == "all", force_naming=force_naming == "1",
                                    include_internal=ctx.is_staff, downloader=ctx.user)
        except ExportTooLarge:
            raise HTTPException(413, "下載總量超過上限，請減少勾選的人數或只下載最新版")
    finally:
        _slots.release()
    audit(db, ctx.user.id, "download.zip", "assignment", a.id,
          {"units": sorted(wanted), "versions": versions, "force_naming": force_naming == "1", **stats}, ctx.ip)
    db.commit()
    course = db.get(Course, a.course_id)
    name = f"{course.file_name}_{a.file_name}_{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))
