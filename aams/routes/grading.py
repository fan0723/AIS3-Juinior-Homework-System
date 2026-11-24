"""批改：提交檢視、逐題分數（optimistic locking）、總分／人工最終分、組員個別調分、內部備註、統一發布。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from .. import acl, notify, services
from ..deps import Ctx, audit, render, require_csrf, staff_ctx
from ..models import (
    Artifact,
    Assignment,
    Course,
    GradeHistory,
    GradeItem,
    GradeTotal,
    GroupMemberOverride,
    InternalNote,
    Question,
    ReleasedGrade,
    ReleaseBatch,
    Revision,
    RubricOption,
    Submission,
    User,
)
from ..utils import clean_text, fmt_score, parse_score, utcnow

router = APIRouter(prefix="/staff")

from .appeals import STATUS_LABELS as APPEAL_LABELS  # noqa: E402
from .appeals import appeals_for_submission  # noqa: E402


def _load_rev(ctx: Ctx, revision_id: int) -> tuple[Revision, Submission, Assignment]:
    rev = ctx.db.get(Revision, revision_id)
    if rev is None:
        raise HTTPException(404)
    sub = ctx.db.get(Submission, rev.submission_id)
    a = ctx.db.get(Assignment, sub.assignment_id)
    return rev, sub, a


@router.get("/submissions/{submission_id}")
def submission_view(submission_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    db = ctx.db
    sub = db.get(Submission, submission_id)
    if sub is None:
        raise HTTPException(404)
    a = db.get(Assignment, sub.assignment_id)
    if not acl.can_view_submission(ctx, sub, a):
        raise HTTPException(404)
    revs = list(db.scalars(select(Revision).where(Revision.submission_id == sub.id).order_by(Revision.revision_no.desc())))
    arts = {r.id: list(db.scalars(select(Artifact).where(Artifact.revision_id == r.id))) for r in revs}
    uploaders = {r.id: db.get(User, r.uploader_id) for r in revs}
    latest = revs[0] if revs else None
    accepted = [r for r in revs if r.scan_state == "ACCEPTED"]
    sel_id = request.query_params.get("rev")
    selected = None
    if sel_id and sel_id.isdigit():
        selected = next((r for r in revs if r.id == int(sel_id)), None)
    if selected is None:
        selected = accepted[0] if accepted else latest

    grading_visible = acl.can_view_grading(ctx, sub, a)
    gradable = acl.gradable_question_ids(ctx, sub, a)
    total_ok = acl.can_grade_total(ctx, sub, a)
    manage = acl.can_manage_course(ctx, a.course_id)
    qs = services.questions_of(db, a.id)
    rubrics = {q.id: list(db.scalars(select(RubricOption).where(RubricOption.question_id == q.id).order_by(RubricOption.score.desc()))) for q in qs}
    sv = services.compute_scores(db, a, selected.id, qs) if (selected and grading_visible) else None
    notes = []
    history = []
    overrides = {}
    members = []
    if grading_visible:
        notes = [(n, db.get(User, n.author_id)) for n in db.scalars(
            select(InternalNote).where(InternalNote.submission_id == sub.id).order_by(InternalNote.created_at.desc()))]
        if selected:
            history = [(h, db.get(User, h.actor_id)) for h in db.scalars(
                select(GradeHistory).where(GradeHistory.revision_id == selected.id).order_by(GradeHistory.created_at.desc()).limit(50))]
            overrides = {o.student_id: o for o in db.scalars(select(GroupMemberOverride).where(GroupMemberOverride.revision_id == selected.id))}
    if sub.owner_type == "GROUP":
        members = [db.get(User, i) for i in acl.group_member_ids(db, sub.group_id)]
    released = services.latest_batch(db, a.id)
    released_rev = None
    if released:
        rg = db.scalar(select(ReleasedGrade).where(ReleasedGrade.batch_id == released.id, ReleasedGrade.submission_id == sub.id))
        released_rev = rg.revision_no if rg else None
    return render(
        request, "staff/submission.html", ctx, a=a, course=db.get(Course, a.course_id), sub=sub,
        owner=services.owner_label(db, sub), revs=revs, arts=arts, uploaders=uploaders, latest=latest, selected=selected,
        grading_visible=grading_visible, gradable=gradable, total_ok=total_ok, manage=manage, qs=qs, rubrics=rubrics,
        sv=sv, notes=notes, history=history, overrides=overrides, members=members, released=released,
        released_rev=released_rev, partial=services.partial_sum(sv) if sv else None,
        appeals=appeals_for_submission(db, sub.id) if manage else [], appeal_labels=APPEAL_LABELS,
        max_total=sum((q.max_points for q in qs), Decimal("0")),
    )


def _check_score_range(score: Decimal | None, max_points: Decimal, ctx: Ctx) -> None:
    if score is None:
        return
    if score < 0 or (score > max_points and not ctx.is_admin):
        raise HTTPException(400, f"分數需介於 0 與 {fmt_score(max_points)} 之間（超過滿分需管理員例外處理）")


@router.post("/revisions/{revision_id}/grade-item")
def save_grade_item(revision_id: int, question_id: int = Form(...), score: str = Form(""), feedback: str = Form(""),
                    expected_version: int = Form(...), reason: str = Form(""), csrf_token: str = Form(""),
                    ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    rev, sub, a = _load_rev(ctx, revision_id)
    q = db.get(Question, question_id)
    # 題目必須屬於該 revision 的作業（防止替換 ID 跨作業寫入）
    if q is None or q.assignment_id != a.id:
        raise HTTPException(400, "題目不屬於此作業")
    # 在同一交易內重新驗證授權
    allowed = acl.gradable_question_ids(ctx, sub, a)
    if allowed != "ALL" and q.id not in allowed:
        raise HTTPException(403, "你沒有此題的批改任務")
    if rev.scan_state != "ACCEPTED":
        raise HTTPException(409, "此版本尚未通過檔案檢查，不能評分")
    try:
        sc = parse_score(score)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _check_score_range(sc, q.max_points, ctx)
    fb = clean_text(feedback, 5000, multiline=True)

    item = db.scalar(select(GradeItem).where(GradeItem.revision_id == rev.id, GradeItem.question_id == q.id))
    if item is None:
        if expected_version != 0:
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
        item = GradeItem(revision_id=rev.id, question_id=q.id, score=sc, feedback_to_student=fb,
                         grader_id=ctx.user.id, row_version=1, updated_at=utcnow())
        db.add(item)
        try:
            db.flush()
        except Exception:
            db.rollback()
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
        old = None
    else:
        old = item.score
        # compare-and-swap：只有 row_version 相符才寫入，否則 409
        res = db.execute(
            update(GradeItem).where(GradeItem.id == item.id, GradeItem.row_version == expected_version)
            .values(score=sc, feedback_to_student=fb, grader_id=ctx.user.id, row_version=expected_version + 1,
                    updated_at=utcnow())
        )
        if res.rowcount != 1:
            db.rollback()
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
    if old != sc:
        db.add(GradeHistory(revision_id=rev.id, field=f"question:{q.ordinal}", old_value=fmt_score(old),
                            new_value=fmt_score(sc), actor_id=ctx.user.id, reason=clean_text(reason, 500)))
        audit(db, ctx.user.id, "grade.item", "revision", rev.id, {"q": q.ordinal, "old": old, "new": sc}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=saved#q{q.id}", 303)


@router.post("/revisions/{revision_id}/total")
def save_total(revision_id: int, direct_total: str = Form(""), manual_final_score: str = Form(""),
               override_reason: str = Form(""), summary_feedback: str = Form(""), expected_version: int = Form(...),
               csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    rev, sub, a = _load_rev(ctx, revision_id)
    if not acl.can_view_grading(ctx, sub, a):
        raise HTTPException(403)
    total_ok = acl.can_grade_total(ctx, sub, a)
    if rev.scan_state != "ACCEPTED":
        raise HTTPException(409, "此版本尚未通過檔案檢查，不能評分")
    qs = services.questions_of(db, a.id)
    max_total = sum((q.max_points for q in qs), Decimal("0")) or Decimal("100")
    try:
        dt = parse_score(direct_total)
        mf = parse_score(manual_final_score)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _check_score_range(dt, max_total, ctx)
    _check_score_range(mf, max_total, ctx)
    gt = db.get(GradeTotal, rev.id)
    cur = gt or GradeTotal(revision_id=rev.id, row_version=0)
    changes = {}
    if a.grading_mode == "DIRECT_TOTAL" and dt != cur.direct_total:
        if not total_ok:
            raise HTTPException(403, "沒有直接設定總分的權限")
        changes["direct_total"] = (cur.direct_total, dt)
    if mf != cur.manual_final_score:
        if not acl.can_manage_course(ctx, a.course_id) and not total_ok:
            raise HTTPException(403, "只有講師／管理員或特別授權者可以覆寫最終分數")
        if mf is not None and not clean_text(override_reason, 500):
            raise HTTPException(400, "覆寫最終分數必須填寫原因")
        changes["manual_final"] = (cur.manual_final_score, mf)
    fb = clean_text(summary_feedback, 5000, multiline=True)
    values = dict(direct_total=dt if "direct_total" in changes else cur.direct_total,
                  manual_final_score=mf if "manual_final" in changes else cur.manual_final_score,
                  override_reason=clean_text(override_reason, 500) if "manual_final" in changes else cur.override_reason,
                  summary_feedback=fb, updated_by=ctx.user.id, updated_at=utcnow())
    if gt is None:
        if expected_version != 0:
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
        db.add(GradeTotal(revision_id=rev.id, row_version=1, **values))
        try:
            db.flush()
        except Exception:
            db.rollback()
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
    else:
        res = db.execute(update(GradeTotal).where(GradeTotal.revision_id == rev.id, GradeTotal.row_version == expected_version)
                         .values(row_version=expected_version + 1, **values))
        if res.rowcount != 1:
            db.rollback()
            return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=conflict", 303)
    for field, (old, new) in changes.items():
        db.add(GradeHistory(revision_id=rev.id, field=field, old_value=fmt_score(old), new_value=fmt_score(new),
                            actor_id=ctx.user.id, reason=clean_text(override_reason, 500)))
    audit(db, ctx.user.id, "grade.total", "revision", rev.id, {k: [str(o), str(n)] for k, (o, n) in changes.items()}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=saved", 303)


@router.post("/revisions/{revision_id}/member-override")
def member_override(revision_id: int, student_id: int = Form(...), score: str = Form(""), reason: str = Form(""),
                    csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    rev, sub, a = _load_rev(ctx, revision_id)
    acl.require_manage_course(ctx, a.course_id)
    if sub.owner_type != "GROUP" or student_id not in acl.group_member_ids(db, sub.group_id):
        raise HTTPException(400, "只能調整該組組員")
    try:
        sc = parse_score(score)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    max_total = sum((q.max_points for q in services.questions_of(db, a.id)), Decimal("0")) or Decimal("100")
    _check_score_range(sc, max_total, ctx)
    reason = clean_text(reason, 500)
    if not reason:
        raise HTTPException(400, "個別調分必須填寫原因")
    o = db.scalar(select(GroupMemberOverride).where(GroupMemberOverride.revision_id == rev.id,
                                                     GroupMemberOverride.student_id == student_id))
    old = o.manual_final_score if o else None
    if o is None:
        db.add(GroupMemberOverride(revision_id=rev.id, student_id=student_id, manual_final_score=sc, reason=reason,
                                   actor_id=ctx.user.id))
    else:
        o.manual_final_score, o.reason, o.actor_id, o.created_at = sc, reason, ctx.user.id, utcnow()
    db.add(GradeHistory(revision_id=rev.id, field=f"member:{student_id}", old_value=fmt_score(old),
                        new_value=fmt_score(sc), actor_id=ctx.user.id, reason=reason))
    audit(db, ctx.user.id, "grade.member_override", "revision", rev.id, {"student": student_id, "new": sc}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=saved", 303)


@router.post("/submissions/{submission_id}/notes")
def add_note(submission_id: int, content: str = Form(""), revision_id: int = Form(0), question_id: int = Form(0),
             csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    sub = db.get(Submission, submission_id)
    if sub is None:
        raise HTTPException(404)
    a = db.get(Assignment, sub.assignment_id)
    if not acl.can_view_grading(ctx, sub, a):
        raise HTTPException(403, "需要有效批改任務才能新增內部備註")
    text = clean_text(content, 5000, multiline=True)
    if not text:
        raise HTTPException(400, "內容不可為空")
    rev = db.get(Revision, revision_id) if revision_id else None
    if rev is not None and rev.submission_id != sub.id:
        raise HTTPException(400)
    q = db.get(Question, question_id) if question_id else None
    if q is not None and q.assignment_id != a.id:
        raise HTTPException(400)
    db.add(InternalNote(submission_id=sub.id, revision_id=rev.id if rev else None, question_id=q.id if q else None,
                        author_id=ctx.user.id, content=text))
    audit(db, ctx.user.id, "note.create", "submission", sub.id, {}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={revision_id or ''}&msg=saved#notes", 303)


@router.post("/revisions/{revision_id}/reclassify")
def reclassify_late(revision_id: int, is_late: str = Form(...), reason: str = Form(""), csrf_token: str = Form(""),
                    ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    if not ctx.is_admin or not ctx.sess.mfa_verified:
        raise HTTPException(403, "僅限已通過 MFA 的管理員")
    rev, sub, a = _load_rev(ctx, revision_id)
    reason = clean_text(reason, 500)
    if not reason:
        raise HTTPException(400, "重新分類必須填寫原因")
    old = rev.is_late
    rev.is_late = is_late == "1"
    rev.late_reclassified_reason = reason
    audit(ctx.db, ctx.user.id, "revision.reclassify_late", "revision", rev.id, {"old": old, "new": rev.is_late, "reason": reason}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=saved", 303)


@router.post("/revisions/{revision_id}/late-note")
def late_note(revision_id: int, note: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    """遲交核准註記：LATE 標記不變，只記錄「已核准」與原因（講師／管理員）。留空 = 清除註記。"""
    require_csrf(ctx.sess, csrf_token)
    rev, sub, a = _load_rev(ctx, revision_id)
    acl.require_manage_course(ctx, a.course_id)
    if not rev.is_late:
        raise HTTPException(400, "只有遲交（LATE）的版本可以加註核准")
    text = clean_text(note, 500)
    old = rev.late_note
    rev.late_note = text
    rev.late_note_by = ctx.user.id if text else None
    rev.late_note_at = utcnow() if text else None
    audit(ctx.db, ctx.user.id, "revision.late_note", "revision", rev.id, {"old": old, "new": text}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/submissions/{sub.id}?rev={rev.id}&msg=saved", 303)


# ------------------------------------------------------------------ release
@router.get("/assignments/{assignment_id}/release")
def release_page(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(404)
    acl.require_manage_course(ctx, a.course_id)
    units = services.assignment_units(db, a)
    blockers = [(u, b) for u in units for b in u.blockers]
    no_sub = [u for u in units if u.submission is None]
    batches = list(db.scalars(select(ReleaseBatch).where(ReleaseBatch.assignment_id == a.id).order_by(ReleaseBatch.release_no.desc())))
    batch_actors = {b.id: db.get(User, b.actor_id) for b in batches}
    return render(request, "staff/release.html", ctx, a=a, course=db.get(Course, a.course_id), units=units,
                  blockers=blockers, no_sub=no_sub, batches=batches, batch_actors=batch_actors)


@router.post("/assignments/{assignment_id}/release")
def release(assignment_id: int, confirm: str = Form(""), reason: str = Form(""), csrf_token: str = Form(""),
            ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    a = ctx.db.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(404)
    acl.require_manage_course(ctx, a.course_id)
    if confirm != "1":
        raise HTTPException(400, "請勾選確認後再發布")
    prev = services.latest_batch(ctx.db, a.id)
    reason = clean_text(reason, 500)
    if prev is not None and not reason:
        raise HTTPException(400, "重新發布（republish）必須填寫原因")
    batch = services.publish(ctx, a, reason)
    ctx.db.commit()
    if a.student_grade_visibility != "HIDDEN":
        course = ctx.db.get(Course, a.course_id)
        what = "成績已發布" if batch.release_no == 1 else f"成績已更新（第 {batch.release_no} 次發布）"
        notify.send("student", f"📊 【{course.title}】{a.title} {what}，請登入作業繳交系統查看。")
    return RedirectResponse(f"/staff/assignments/{a.id}/release?msg=released", 303)
