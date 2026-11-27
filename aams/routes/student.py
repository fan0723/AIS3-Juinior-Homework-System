"""學員端：課程、教材、作業、提交、收據、通知。"""
from __future__ import annotations

import json
from datetime import timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import acl, services
from ..config import settings
from ..deps import Ctx, get_ctx, render, require_csrf
from ..models import (
    Artifact,
    Assignment,
    Course,
    CourseMaterial,
    Notification,
    Revision,
    Submission,
    User,
)
from ..security import new_token
from ..storage import QUARANTINE, declared_type_of, new_key, stream_to
from ..utils import sanitize_filename, utcnow

router = APIRouter()

from .appeals import STATUS_LABELS as APPEAL_LABELS  # noqa: E402
from .appeals import appeals_for_submission  # noqa: E402


def visible_courses(ctx: Ctx) -> list[Course]:
    courses = list(ctx.db.scalars(select(Course).order_by(Course.ordinal, Course.id)))
    managed = acl.managed_course_ids(ctx)
    return [c for c in courses if c.status == "PUBLISHED" or managed is None or c.id in managed]


@router.get("/")
def home(request: Request, ctx: Ctx = Depends(get_ctx)):
    courses = visible_courses(ctx)
    db = ctx.db
    cards = []
    for c in courses:
        assignments = [a for a in db.scalars(select(Assignment).where(Assignment.course_id == c.id)) if acl.assignment_visible(ctx, a)]
        cards.append({"course": c, "n_assignments": len(assignments)})
    upcoming = []
    if ctx.is_student:
        now = utcnow()
        for c in courses:
            for a in db.scalars(select(Assignment).where(Assignment.course_id == c.id, Assignment.status == "PUBLISHED")):
                if c.status != "PUBLISHED":
                    continue
                sub = services.find_submission(db, a, ctx)
                upcoming.append({"a": a, "course": c, "sub": sub, "past_due": now > a.due_at,
                                 "latest": db.get(Revision, sub.active_revision_id) if sub and sub.active_revision_id else None})
        upcoming.sort(key=lambda x: x["a"].due_at)
    group = acl.active_group_of(db, ctx.user.id) if ctx.is_student else None
    from .announcements import visible_announcements

    anns = visible_announcements(ctx, only_global=True)[:5]
    return render(request, "home.html", ctx, cards=cards, upcoming=upcoming, group=group, anns=anns, now=utcnow())


@router.get("/courses/{course_id}")
def course_page(course_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404)
    manage = acl.can_manage_course(ctx, course.id)
    if course.status != "PUBLISHED" and not manage:
        raise HTTPException(404)
    mats = list(db.scalars(select(CourseMaterial).where(CourseMaterial.course_id == course.id).order_by(CourseMaterial.created_at)))
    if not manage:
        mats = [m for m in mats if m.visibility == "PUBLISHED"]
    assignments = [a for a in db.scalars(select(Assignment).where(Assignment.course_id == course.id).order_by(Assignment.due_at))
                   if acl.assignment_visible(ctx, a)]
    now = utcnow()
    rows = []
    for a in assignments:
        sub = services.find_submission(db, a, ctx) if ctx.is_student else None
        latest = db.get(Revision, sub.active_revision_id) if sub and sub.active_revision_id else None
        released = services.latest_batch(db, a.id) is not None
        rate = services.submission_rate(db, a) if (a.show_submission_rate and now > a.due_at) else None
        rows.append({"a": a, "sub": sub, "latest": latest, "released": released, "past_due": now > a.due_at, "rate": rate})
    from .announcements import visible_announcements

    anns = visible_announcements(ctx, course_id=course.id, include_hidden=manage)
    return render(request, "course.html", ctx, course=course, materials=mats, rows=rows, manage=manage, anns=anns)


@router.get("/assignments/{assignment_id}")
def assignment_page(assignment_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None or not acl.assignment_visible(ctx, a):
        raise HTTPException(404)
    if not ctx.is_student:
        return RedirectResponse(f"/staff/assignments/{a.id}", 303)
    course = db.get(Course, a.course_id)
    now = utcnow()
    group = acl.active_group_of(db, ctx.user.id)
    members = [db.get(User, i) for i in acl.group_member_ids(db, group.id)] if group else []
    sub = services.find_submission(db, a, ctx)
    revisions = []
    if sub is not None:
        for r in db.scalars(select(Revision).where(Revision.submission_id == sub.id).order_by(Revision.revision_no.desc())):
            arts = list(db.scalars(select(Artifact).where(Artifact.revision_id == r.id)))
            revisions.append({"r": r, "arts": arts, "uploader": db.get(User, r.uploader_id)})
    rel = services.student_release(db, a, ctx.user.id)
    release = None
    if rel:
        batch, rg = rel
        release = {"batch": batch, "rg": rg, "snap": json.loads(rg.snapshot_json)}
    latest_no = revisions[0]["r"].revision_no if revisions else 0
    stats = services.public_stats(db, a) if rel else None
    rate = services.submission_rate(db, a) if (a.show_submission_rate and now > a.due_at) else None
    is_late_now = now > a.due_at
    final_at = a.final_deadline_effective
    past_final = now > final_at
    can_submit = a.accepting_submissions and (not is_late_now or a.allow_late) and not past_final and \
        (a.submission_mode == "INDIVIDUAL" or group is not None)
    return render(
        request, "assignment.html", ctx, a=a, course=course, group=group, members=members, sub=sub,
        revisions=revisions, release=release, latest_no=latest_no, stats=stats, rate=rate,
        is_late_now=is_late_now, can_submit=can_submit, idem=new_token(16),
        final_at=final_at, past_final=past_final, server_epoch=int(now.replace(tzinfo=timezone.utc).timestamp() * 1000),
        due_in=(a.due_at - now).total_seconds(), final_in=(final_at - now).total_seconds(),
        any_released=services.latest_batch(db, a.id) is not None,
        appeals=appeals_for_submission(db, sub.id) if sub else [], appeal_deadline=services.appeal_deadline(db, a.id),
        questions=services.questions_of(db, a.id), qmap={q.id: q for q in services.questions_of(db, a.id)},
        appeal_labels=APPEAL_LABELS,
        now=now, accept=",".join("." + t for t in a.allowed_type_list) + (",.jpeg" if "jpg" in a.allowed_type_list else ""),
    )


@router.post("/assignments/{assignment_id}/submit")
def submit(assignment_id: int, request: Request, files: list[UploadFile] = File(default=[]),
           idempotency_key: str = Form(""), late_ack: str = Form(""), csrf_token: str = Form(""),
           ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None or not ctx.is_student or not acl.assignment_visible(ctx, a):
        raise HTTPException(404)
    if not idempotency_key or len(idempotency_key) > 64:
        raise HTTPException(400, "缺少提交識別碼，請重新整理頁面")
    if not a.accepting_submissions:
        raise HTTPException(403, "此作業目前不開放收件")
    late = utcnow() > a.due_at
    if late and not a.allow_late:
        raise HTTPException(409, "已超過截止時間，且此作業不接受遲交")
    if utcnow() > a.final_deadline_effective:
        raise HTTPException(409, "已超過最終收件期限，系統不再收件")
    if (late or services.latest_batch(db, a.id) is not None) and late_ack != "1":
        raise HTTPException(400, "這次提交屬於補交／發布後重交，請勾選確認後再送出")

    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(400, "請選擇至少一個檔案")
    limit_files = a.max_files if a.allow_multiple_files else 1
    if len(files) > limit_files:
        raise HTTPException(400, f"此作業每次最多提交 {limit_files} 個檔案")

    staged: list[services.StagedFile] = []
    budget = min(a.max_total_bytes, settings.max_total_bytes_cap)
    allowed_types = a.allowed_type_list
    total_mb = a.max_total_bytes / 1_000_000
    db.rollback()  # 寫入大檔期間釋放 DB 連線；finalize 時會在新交易中重新驗證所有規則
    try:
        used = 0
        for f in files:
            declared = declared_type_of(f.filename, allowed_types)
            if declared is None:
                raise HTTPException(400, f"不允許的檔案類型：{sanitize_filename(f.filename)}（允許：{', '.join(allowed_types)}）")
            key = new_key()
            dest = QUARANTINE / key
            try:
                size, sha = stream_to(f.file, dest, budget - used)
            except OverflowError:
                raise HTTPException(413, f"整次提交的所有檔案合計超過 {total_mb:g} MB，整次提交已取消")
            staged.append(services.StagedFile(key, sanitize_filename(f.filename), size, sha, declared))
            used += size
            if size == 0:
                raise HTTPException(400, "不可上傳空白檔案")
            with open(dest, "rb") as fh:
                head = fh.read(16)
            from ..inspector import quick_magic_ok

            if not quick_magic_ok(head, declared):
                raise HTTPException(400, f"檔案內容與副檔名不符：{sanitize_filename(f.filename)}")
        rev, purged = services.finalize_revision(ctx, a, staged, idempotency_key)
        db.commit()
    except BaseException:
        db.rollback()
        for s in staged:
            (QUARANTINE / s.key).unlink(missing_ok=True)
        raise
    for p in purged:  # commit 成功後才刪舊版檔案
        p.unlink(missing_ok=True)
    return RedirectResponse(f"/revisions/{rev.id}/receipt?msg=submitted", 303)


@router.get("/revisions/{revision_id}/receipt")
def receipt(revision_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    rev = db.get(Revision, revision_id)
    if rev is None:
        raise HTTPException(404)
    sub = db.get(Submission, rev.submission_id)
    a = db.get(Assignment, sub.assignment_id)
    if not acl.can_view_submission(ctx, sub, a):
        raise HTTPException(404)
    arts = list(db.scalars(select(Artifact).where(Artifact.revision_id == rev.id)))
    return render(request, "receipt.html", ctx, rev=rev, sub=sub, a=a, arts=arts, uploader=db.get(User, rev.uploader_id),
                  owner=services.owner_label(db, sub))


@router.get("/notifications")
def notifications(request: Request, ctx: Ctx = Depends(get_ctx)):
    items = list(ctx.db.scalars(select(Notification).where(Notification.recipient_id == ctx.user.id)
                                .order_by(Notification.created_at.desc()).limit(200)))
    states = {n.id: ctx.db.get(Revision, n.revision_id) for n in items}
    return render(request, "notifications.html", ctx, items=items, states=states)


@router.post("/notifications/read")
def notifications_read(request: Request, csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    now = utcnow()
    for n in ctx.db.scalars(select(Notification).where(Notification.recipient_id == ctx.user.id, Notification.read_at.is_(None))):
        n.read_at = now
    ctx.db.commit()
    return RedirectResponse("/notifications", 303)
