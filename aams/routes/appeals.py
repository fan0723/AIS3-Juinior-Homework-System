"""成績申覆：學員在第一次發布成績後 3 天內提出，一律由講師（或管理員）處理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from .. import acl, notify, services
from ..deps import Ctx, audit, get_ctx, render, require_csrf, staff_ctx
from ..models import Assignment, Course, GradeAppeal, Question, Submission, User
from ..utils import clean_text, utcnow

router = APIRouter()

STATUS_LABELS = {"OPEN": "處理中", "KEPT": "維持原分", "CHANGED": "已改分"}


def appeals_for_submission(db, submission_id: int) -> list[GradeAppeal]:
    return list(db.scalars(select(GradeAppeal).where(GradeAppeal.submission_id == submission_id)
                           .order_by(GradeAppeal.created_at.desc())))


def open_count(ctx: Ctx) -> int:
    managed = acl.managed_course_ids(ctx)
    stmt = select(func.count()).select_from(GradeAppeal).join(Assignment, Assignment.id == GradeAppeal.assignment_id) \
        .where(GradeAppeal.status == "OPEN")
    if managed is not None:
        if not managed:
            return 0
        stmt = stmt.where(Assignment.course_id.in_(managed))
    return ctx.db.scalar(stmt) or 0


@router.post("/assignments/{assignment_id}/appeals")
def appeal_create(assignment_id: int, question_id: str = Form(""), reason: str = Form(""), csrf_token: str = Form(""),
                  ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None or not ctx.is_student or not acl.assignment_visible(ctx, a):
        raise HTTPException(404)
    rel = services.student_release(db, a, ctx.user.id)
    if rel is None or rel[1].visibility == "HIDDEN":
        raise HTTPException(403, "此作業尚未發布你的成績，或成績不對學員公開")
    deadline = services.appeal_deadline(db, a.id)
    if deadline is None or utcnow() > deadline:
        raise HTTPException(409, "已超過成績申覆期限（第一次發布成績後 3 天）")
    rg = rel[1]
    sub = db.get(Submission, rg.submission_id)
    if sub is None or not acl.is_owner(ctx, sub):
        raise HTTPException(403)
    qid = None
    if question_id not in ("", "total"):
        try:
            qid = int(question_id)
        except ValueError:
            raise HTTPException(400, "題目錯誤")
        q = db.get(Question, qid)
        if q is None or q.assignment_id != a.id:
            raise HTTPException(400, "題目不屬於此作業")
    text = clean_text(reason, 2000, multiline=True)
    if len(text) < 5:
        raise HTTPException(400, "請具體說明申覆理由（至少 5 個字）")
    dup = db.scalar(select(GradeAppeal).where(
        GradeAppeal.submission_id == sub.id, GradeAppeal.status == "OPEN",
        GradeAppeal.question_id.is_(None) if qid is None else GradeAppeal.question_id == qid))
    if dup is not None:
        raise HTTPException(409, "同一題已有處理中的申覆，請等待講師回覆")
    ap = GradeAppeal(assignment_id=a.id, submission_id=sub.id, student_id=ctx.user.id, question_id=qid,
                     revision_no=rg.revision_no, reason=text)
    db.add(ap)
    db.flush()
    audit(db, ctx.user.id, "appeal.create", "appeal", ap.id, {"assignment": a.id, "question": qid}, ctx.ip)
    db.commit()
    course = db.get(Course, a.course_id)
    notify.send("staff", f"📝 新的成績申覆：{course.title} / {a.title}（請講師登入「工作台 › 成績申覆」處理）")
    return RedirectResponse(f"/assignments/{a.id}?msg=appeal_created#appeals", 303)


# ---------------------------------------------------------------- staff side
@router.get("/staff/appeals")
def appeals_page(request: Request, ctx: Ctx = Depends(staff_ctx)):
    db = ctx.db
    managed = acl.managed_course_ids(ctx)
    if managed is not None and not managed:
        raise HTTPException(403, "成績申覆由講師處理")
    show = request.query_params.get("show", "open")
    stmt = select(GradeAppeal).join(Assignment, Assignment.id == GradeAppeal.assignment_id)
    if managed is not None:
        stmt = stmt.where(Assignment.course_id.in_(managed))
    if show != "all":
        stmt = stmt.where(GradeAppeal.status == "OPEN")
    rows = []
    for ap in db.scalars(stmt.order_by(GradeAppeal.status != "OPEN", GradeAppeal.created_at)):
        a = db.get(Assignment, ap.assignment_id)
        sub = db.get(Submission, ap.submission_id)
        rows.append({"ap": ap, "a": a, "course": db.get(Course, a.course_id), "student": db.get(User, ap.student_id),
                     "owner": services.owner_label(db, sub),
                     "q": db.get(Question, ap.question_id) if ap.question_id else None,
                     "handler": db.get(User, ap.handler_id) if ap.handler_id else None})
    return render(request, "staff/appeals.html", ctx, rows=rows, show=show, labels=STATUS_LABELS)


@router.post("/staff/appeals/{appeal_id}/resolve")
def appeal_resolve(appeal_id: int, decision: str = Form(""), response: str = Form(""), csrf_token: str = Form(""),
                   ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    ap = db.get(GradeAppeal, appeal_id)
    if ap is None:
        raise HTTPException(404)
    a = db.get(Assignment, ap.assignment_id)
    acl.require_manage_course(ctx, a.course_id)  # 助教不能處理申覆
    if decision not in ("KEPT", "CHANGED"):
        raise HTTPException(400, "請選擇處理結果")
    text = clean_text(response, 2000, multiline=True)
    if not text:
        raise HTTPException(400, "請填寫給學員的回覆")
    old = ap.status
    ap.status, ap.response, ap.handler_id, ap.resolved_at = decision, text, ctx.user.id, utcnow()
    audit(db, ctx.user.id, "appeal.resolve", "appeal", ap.id, {"old": old, "new": decision}, ctx.ip)
    db.commit()
    return RedirectResponse("/staff/appeals?msg=saved", 303)
