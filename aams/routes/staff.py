"""工作人員端：工作台、課程／教材／作業設定、題目與 Rubric、批改任務、統計。"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from .. import acl, reminders, services
from ..config import settings
from ..deps import Ctx, audit, render, require_csrf, staff_ctx
from ..inspector import inspect_path
from ..models import (
    Assignment,
    Course,
    CourseMaterial,
    GradeItem,
    GradingTask,
    GradingTaskQuestion,
    Group,
    Notification,
    Question,
    Revision,
    RubricOption,
    Submission,
    User,
    UserRole,
)
from ..storage import MATERIALS, TMP, new_key, stream_to
from ..utils import clean_text, fmt_score, parse_local_input, parse_score, sanitize_filename, utcnow

router = APIRouter(prefix="/staff")

NAME_EN_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _assignment(ctx: Ctx, assignment_id: int) -> Assignment:
    a = ctx.db.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(404)
    return a


def _managed_assignment(ctx: Ctx, assignment_id: int) -> Assignment:
    a = _assignment(ctx, assignment_id)
    acl.require_manage_course(ctx, a.course_id)
    return a


# ------------------------------------------------------------------ dashboard
@router.get("")
def dashboard(request: Request, ctx: Ctx = Depends(staff_ctx)):
    db = ctx.db
    managed = acl.managed_course_ids(ctx)
    courses = [c for c in db.scalars(select(Course).order_by(Course.ordinal, Course.id)) if managed is None or c.id in managed]
    course_rows = [{"c": c, "assignments": list(db.scalars(select(Assignment).where(Assignment.course_id == c.id).order_by(Assignment.due_at)))}
                   for c in courses]
    my_groups = []
    if ctx.is_ta:
        for gid in sorted(acl.ta_group_ids(db, ctx.user.id)):
            g = db.get(Group, gid)
            my_groups.append({"g": g, "members": [db.get(User, i) for i in acl.group_member_ids(db, gid)]})
    tasks = []
    for t in db.scalars(select(GradingTask).where(GradingTask.grader_id == ctx.user.id, GradingTask.revoked_at.is_(None))):
        a = db.get(Assignment, t.assignment_id)
        qs = [db.get(Question, q) for q in db.scalars(select(GradingTaskQuestion.question_id).where(GradingTaskQuestion.task_id == t.id))]
        tasks.append({"t": t, "a": a, "g": db.get(Group, t.target_group_id), "qs": sorted(qs, key=lambda q: q.ordinal)})
    published = []
    if ctx.is_ta:
        published = [a for a in db.scalars(select(Assignment).where(Assignment.status == "PUBLISHED").order_by(Assignment.due_at))]
    notes = list(db.scalars(select(Notification).where(Notification.recipient_id == ctx.user.id, Notification.read_at.is_(None))
                            .order_by(Notification.created_at.desc()).limit(10)))
    from .appeals import open_count

    appeal_open = open_count(ctx) if (ctx.is_admin or ctx.is_instructor) else 0
    return render(request, "staff/dashboard.html", ctx, course_rows=course_rows, my_groups=my_groups, tasks=tasks,
                  published=published, notes=notes, appeal_open=appeal_open)


# ------------------------------------------------------------------ courses / materials
@router.get("/courses/{course_id}")
def course_manage(course_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    acl.require_manage_course(ctx, course_id)
    db = ctx.db
    course = db.get(Course, course_id)
    mats = list(db.scalars(select(CourseMaterial).where(CourseMaterial.course_id == course_id).order_by(CourseMaterial.created_at)))
    assignments = list(db.scalars(select(Assignment).where(Assignment.course_id == course_id).order_by(Assignment.due_at)))
    return render(request, "staff/course.html", ctx, course=course, materials=mats, assignments=assignments)


@router.post("/courses/{course_id}/edit")
def course_edit(course_id: int, description: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    acl.require_manage_course(ctx, course_id)
    c = ctx.db.get(Course, course_id)
    c.description = clean_text(description, 5000, multiline=True)
    audit(ctx.db, ctx.user.id, "course.edit", "course", c.id, {}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/courses/{course_id}?msg=saved", 303)


@router.post("/courses/{course_id}/materials")
def material_upload(course_id: int, title: str = Form(""), file: UploadFile = File(...), csrf_token: str = Form(""),
                    ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    acl.require_manage_course(ctx, course_id)
    name = (file.filename or "").lower()
    ftype = "pdf" if name.endswith(".pdf") else None
    if ftype is None:
        raise HTTPException(400, "教材預設只接受 PDF")
    key = new_key()
    tmp = TMP / key
    try:
        size, sha = stream_to(file.file, tmp, settings.max_material_bytes)
    except OverflowError:
        raise HTTPException(413, "教材檔案過大")
    res = inspect_path(str(tmp), "pdf")
    if not res["ok"]:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "教材未通過 PDF 檢查：" + res["detail"])
    tmp.replace(MATERIALS / key)
    m = CourseMaterial(course_id=course_id, title=clean_text(title, 200) or sanitize_filename(file.filename),
                       storage_key=key, original_filename=sanitize_filename(file.filename), file_type=ftype,
                       size=size, sha256=sha, uploaded_by=ctx.user.id)
    ctx.db.add(m)
    audit(ctx.db, ctx.user.id, "material.upload", "course", course_id, {"sha256": sha, "size": size}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/courses/{course_id}?msg=created", 303)


@router.post("/materials/{material_id}/visibility")
def material_visibility(material_id: int, visibility: str = Form(...), csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    m = ctx.db.get(CourseMaterial, material_id)
    if m is None:
        raise HTTPException(404)
    acl.require_manage_course(ctx, m.course_id)
    if visibility not in ("PUBLISHED", "HIDDEN"):
        raise HTTPException(400)
    m.visibility = visibility
    audit(ctx.db, ctx.user.id, "material.visibility", "material", m.id, {"visibility": visibility}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/courses/{m.course_id}?msg=saved", 303)


# ------------------------------------------------------------------ assignment settings
def _discord_roles(ctx: Ctx):
    from .discord import roles

    return roles(ctx.db)


def _apply_assignment_form(ctx: Ctx, a: Assignment, f: dict, creating: bool) -> list[str]:
    errs: list[str] = []
    before = {k: getattr(a, k, None) for k in ("due_at", "final_deadline", "allow_late", "accepting_submissions",
                                                 "max_total_bytes", "allowed_types", "submission_mode", "status",
                                                 "name_en", "reminder_hours", "discord_role_id")}
    a.title = clean_text(f["title"], 200)
    if not a.title:
        errs.append("請輸入標題")
    a.description = clean_text(f["description"], 10000, multiline=True)
    mode = f["submission_mode"]
    if mode not in ("INDIVIDUAL", "GROUP"):
        errs.append("繳交模式錯誤")
    elif not creating and mode != a.submission_mode:
        has_sub = ctx.db.scalar(select(func.count()).select_from(Submission).where(Submission.assignment_id == a.id))
        if has_sub:
            errs.append("已有學生提交，不可切換個人／團體模式")
        else:
            a.submission_mode = mode
    else:
        a.submission_mode = mode
    try:
        due = parse_local_input(f["due_at"])
        if creating or due != a.due_at:
            if not creating:
                a.due_version = (a.due_version or 1) + 1
            a.due_at = due
    except ValueError:
        errs.append("截止時間格式錯誤")
    name_en = (f["name_en"] or "").strip()
    if name_en and not NAME_EN_RE.fullmatch(name_en):
        errs.append("英文名稱只能使用英文字母、數字、底線與連字號（最多 40 字）")
    else:
        a.name_en = name_en
    if (f["final_deadline"] or "").strip():
        try:
            a.final_deadline = parse_local_input(f["final_deadline"])
        except ValueError:
            errs.append("最終收件期限格式錯誤")
    elif a.due_at is not None:
        a.final_deadline = services.default_final_deadline(a.due_at)
    if a.due_at is not None and a.final_deadline is not None and a.final_deadline < a.due_at:
        errs.append("最終收件期限不可早於截止時間")
    mode = f["reminder_mode"]
    if mode == "off":
        a.reminder_hours = "off"
    elif mode == "custom":
        try:
            hs = reminders.parse_hours(f["reminder_custom"])
            if not hs:
                errs.append("截止前提醒：選了「自訂」就要填提醒時間，例如 2,1")
            else:
                a.reminder_hours = ",".join(reminders.hours_key(h) for h in hs)
        except ValueError as exc:
            errs.append(f"截止前提醒：{exc}")
    else:
        a.reminder_hours = None
    from .discord import parse_role_choice

    a.discord_role_id = parse_role_choice(ctx.db, f["discord_role"])
    a.accepting_submissions = f["accepting_submissions"] == "1"
    a.allow_late = f["allow_late"] == "1"
    types = [t for t in f["allowed_types"] if t in settings.system_allowed_types]
    if not types:
        errs.append("請至少勾選一種允許的檔案類型")
    a.allowed_types = ",".join(sorted(set(types)))
    a.allow_multiple_files = f["allow_multiple_files"] == "1"
    try:
        mf = int(f["max_files"] or 1)
    except ValueError:
        mf = 0
    if a.allow_multiple_files:
        if not 1 <= mf <= settings.max_files_cap:
            errs.append(f"每次檔案數需介於 1–{settings.max_files_cap}")
        a.max_files = max(1, min(mf, settings.max_files_cap))
    else:
        a.max_files = 1
    try:
        mb = int(f["max_total_mb"])
    except ValueError:
        mb = 0
    if not 1 <= mb <= settings.max_total_bytes_cap // 1_000_000:
        errs.append("單次提交總容量需介於 1–40 MB")
    else:
        a.max_total_bytes = mb * 1_000_000
    a.grading_mode = f["grading_mode"] if f["grading_mode"] in ("BY_QUESTION", "DIRECT_TOTAL") else "BY_QUESTION"
    a.show_student_average = f["show_student_average"] == "1"
    a.show_submission_rate = f["show_submission_rate"] == "1"
    a.student_grade_visibility = "HIDDEN" if f["student_grade_visibility"] == "HIDDEN" else "VISIBLE_AFTER_RELEASE"
    a.status = "PUBLISHED" if f["status"] == "PUBLISHED" else "DRAFT"
    a.updated_at = utcnow()
    if not errs:
        after = {k: getattr(a, k) for k in before}
        changed = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
        audit(ctx.db, ctx.user.id, "assignment.create" if creating else "assignment.update", "assignment",
              a.id or "new", {"changed": changed}, ctx.ip)
    return errs


@router.get("/courses/{course_id}/assignments/new")
def assignment_new_form(course_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    acl.require_manage_course(ctx, course_id)
    a = Assignment(course_id=course_id, title="", description="", submission_mode="INDIVIDUAL",
                   due_at=utcnow(), accepting_submissions=True, allow_late=True, allowed_types="pdf",
                   allow_multiple_files=False, max_files=1, max_total_bytes=settings.default_total_bytes,
                   grading_mode="BY_QUESTION", show_student_average=False, show_submission_rate=True,
                   student_grade_visibility="VISIBLE_AFTER_RELEASE", status="DRAFT",
                   final_deadline=services.default_final_deadline(None), name_en="")
    return render(request, "staff/assignment_form.html", ctx, a=a, course=ctx.db.get(Course, course_id), creating=True,
                  errors=[], system_types=settings.system_allowed_types, discord_roles=_discord_roles(ctx), has_subs=False)


def _collect(title, description, submission_mode, due_at, accepting_submissions, allow_late, allowed_types,
             allow_multiple_files, max_files, max_total_mb, grading_mode, show_student_average, show_submission_rate,
             student_grade_visibility, status, final_deadline, name_en, reminder_mode, reminder_custom,
             discord_role) -> dict:
    return locals()


@router.post("/courses/{course_id}/assignments/new")
def assignment_create(
    course_id: int, request: Request, title: str = Form(""), description: str = Form(""),
    submission_mode: str = Form("INDIVIDUAL"), due_at: str = Form(""), accepting_submissions: str = Form(""),
    allow_late: str = Form(""), allowed_types: list[str] = Form([]), allow_multiple_files: str = Form(""),
    max_files: str = Form("1"), max_total_mb: str = Form("20"), grading_mode: str = Form("BY_QUESTION"),
    show_student_average: str = Form(""), show_submission_rate: str = Form(""),
    student_grade_visibility: str = Form("VISIBLE_AFTER_RELEASE"), status: str = Form("DRAFT"),
    final_deadline: str = Form(""), name_en: str = Form(""), reminder_mode: str = Form("default"),
    reminder_custom: str = Form(""), discord_role: str = Form(""),
    csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx),
):
    require_csrf(ctx.sess, csrf_token)
    acl.require_manage_course(ctx, course_id)
    f = _collect(title, description, submission_mode, due_at, accepting_submissions, allow_late, allowed_types,
                 allow_multiple_files, max_files, max_total_mb, grading_mode, show_student_average,
                 show_submission_rate, student_grade_visibility, status, final_deadline, name_en,
                 reminder_mode, reminder_custom, discord_role)
    a = Assignment(course_id=course_id, created_by=ctx.user.id, due_version=1)
    errs = _apply_assignment_form(ctx, a, f, creating=True)
    if errs:
        ctx.db.rollback()
        return render(request, "staff/assignment_form.html", ctx, a=a, course=ctx.db.get(Course, course_id), creating=True,
                      errors=errs, system_types=settings.system_allowed_types, discord_roles=_discord_roles(ctx), has_subs=False, status_code=400)
    ctx.db.add(a)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=created", 303)


@router.get("/assignments/{assignment_id}/edit")
def assignment_edit_form(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _managed_assignment(ctx, assignment_id)
    has_subs = bool(ctx.db.scalar(select(func.count()).select_from(Submission).where(Submission.assignment_id == a.id)))
    return render(request, "staff/assignment_form.html", ctx, a=a, course=ctx.db.get(Course, a.course_id), creating=False,
                  errors=[], system_types=settings.system_allowed_types, discord_roles=_discord_roles(ctx), has_subs=has_subs)


@router.post("/assignments/{assignment_id}/edit")
def assignment_update(
    assignment_id: int, request: Request, title: str = Form(""), description: str = Form(""),
    submission_mode: str = Form("INDIVIDUAL"), due_at: str = Form(""), accepting_submissions: str = Form(""),
    allow_late: str = Form(""), allowed_types: list[str] = Form([]), allow_multiple_files: str = Form(""),
    max_files: str = Form("1"), max_total_mb: str = Form("20"), grading_mode: str = Form("BY_QUESTION"),
    show_student_average: str = Form(""), show_submission_rate: str = Form(""),
    student_grade_visibility: str = Form("VISIBLE_AFTER_RELEASE"), status: str = Form("DRAFT"),
    final_deadline: str = Form(""), name_en: str = Form(""), reminder_mode: str = Form("default"),
    reminder_custom: str = Form(""), discord_role: str = Form(""),
    csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx),
):
    require_csrf(ctx.sess, csrf_token)
    a = _managed_assignment(ctx, assignment_id)
    f = _collect(title, description, submission_mode, due_at, accepting_submissions, allow_late, allowed_types,
                 allow_multiple_files, max_files, max_total_mb, grading_mode, show_student_average,
                 show_submission_rate, student_grade_visibility, status, final_deadline, name_en,
                 reminder_mode, reminder_custom, discord_role)
    errs = _apply_assignment_form(ctx, a, f, creating=False)
    if errs:
        ctx.db.rollback()
        a = _managed_assignment(ctx, assignment_id)
        return render(request, "staff/assignment_form.html", ctx, a=a, course=ctx.db.get(Course, a.course_id),
                      creating=False, errors=errs, system_types=settings.system_allowed_types, discord_roles=_discord_roles(ctx), has_subs=True,
                      status_code=400)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}?msg=saved", 303)


# ------------------------------------------------------------------ questions & rubric
def _question_has_scores(ctx: Ctx, qid: int) -> bool:
    return bool(ctx.db.scalar(select(func.count()).select_from(GradeItem).where(GradeItem.question_id == qid,
                                                                              GradeItem.score.is_not(None))))


@router.get("/assignments/{assignment_id}/questions")
def questions_page(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _managed_assignment(ctx, assignment_id)
    qs = services.questions_of(ctx.db, a.id)
    rubrics = {q.id: list(ctx.db.scalars(select(RubricOption).where(RubricOption.question_id == q.id).order_by(RubricOption.score.desc())))
               for q in qs}
    locked = {q.id: _question_has_scores(ctx, q.id) for q in qs}
    total = sum((q.max_points for q in qs), Decimal("0"))
    return render(request, "staff/questions.html", ctx, a=a, course=ctx.db.get(Course, a.course_id), qs=qs,
                  rubrics=rubrics, locked=locked, total=total)


@router.post("/assignments/{assignment_id}/questions")
def question_add(assignment_id: int, title: str = Form(""), max_points: str = Form(""), csrf_token: str = Form(""),
                 ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    a = _managed_assignment(ctx, assignment_id)
    try:
        mp = parse_score(max_points)
    except ValueError:
        mp = None
    if mp is None or mp <= 0 or mp > 1000:
        raise HTTPException(400, "滿分需為 0–1000 之間的數字")
    n = (ctx.db.scalar(select(func.max(Question.ordinal)).where(Question.assignment_id == a.id)) or 0) + 1
    q = Question(assignment_id=a.id, ordinal=n, title=clean_text(title, 200), max_points=mp)
    ctx.db.add(q)
    audit(ctx.db, ctx.user.id, "question.create", "assignment", a.id, {"ordinal": n, "max": mp}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=created", 303)


@router.post("/questions/{question_id}/update")
def question_update(question_id: int, title: str = Form(""), max_points: str = Form(""), csrf_token: str = Form(""),
                    ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    q = ctx.db.get(Question, question_id)
    if q is None:
        raise HTTPException(404)
    a = _managed_assignment(ctx, q.assignment_id)
    try:
        mp = parse_score(max_points)
    except ValueError:
        mp = None
    if mp is None or mp <= 0 or mp > 1000:
        raise HTTPException(400, "滿分需為 0–1000 之間的數字")
    if mp != q.max_points and _question_has_scores(ctx, q.id):
        raise HTTPException(409, "此題已有分數，不可修改滿分")
    q.title = clean_text(title, 200)
    q.max_points = mp
    audit(ctx.db, ctx.user.id, "question.update", "question", q.id, {"max": mp}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=saved", 303)


@router.post("/questions/{question_id}/delete")
def question_delete(question_id: int, csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    q = ctx.db.get(Question, question_id)
    if q is None:
        raise HTTPException(404)
    a = _managed_assignment(ctx, q.assignment_id)
    used = ctx.db.scalar(select(func.count()).select_from(GradeItem).where(GradeItem.question_id == q.id)) or 0
    used += ctx.db.scalar(select(func.count()).select_from(GradingTaskQuestion).where(GradingTaskQuestion.question_id == q.id)) or 0
    if used:
        raise HTTPException(409, "此題已有評分紀錄或批改任務，不可刪除")
    for r in ctx.db.scalars(select(RubricOption).where(RubricOption.question_id == q.id)):
        ctx.db.delete(r)
    ctx.db.delete(q)
    audit(ctx.db, ctx.user.id, "question.delete", "question", question_id, {}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=deleted", 303)


@router.post("/questions/{question_id}/rubric")
def rubric_add(question_id: int, label: str = Form(""), score: str = Form(""), feedback_template: str = Form(""),
               csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    q = ctx.db.get(Question, question_id)
    if q is None:
        raise HTTPException(404)
    a = _managed_assignment(ctx, q.assignment_id)
    try:
        sc = parse_score(score)
    except ValueError:
        sc = None
    if sc is None or sc < 0 or sc > q.max_points:
        raise HTTPException(400, "Rubric 分數需介於 0 與該題滿分之間")
    ctx.db.add(RubricOption(question_id=q.id, label=clean_text(label, 100) or fmt_score(sc), score=sc,
                            feedback_template=clean_text(feedback_template, 2000, multiline=True)))
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=created", 303)


@router.post("/rubric/{rubric_id}/delete")
def rubric_delete(rubric_id: int, csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    r = ctx.db.get(RubricOption, rubric_id)
    if r is None:
        raise HTTPException(404)
    q = ctx.db.get(Question, r.question_id)
    a = _managed_assignment(ctx, q.assignment_id)
    ctx.db.delete(r)
    ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/questions?msg=deleted", 303)


# ------------------------------------------------------------------ assignment overview (staff)
@router.get("/assignments/{assignment_id}")
def assignment_overview(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _assignment(ctx, assignment_id)
    db = ctx.db
    course = db.get(Course, a.course_id)
    manage = acl.can_manage_course(ctx, a.course_id)
    vis = acl.visible_group_ids_for_assignment(ctx, a)
    if not manage and (a.status != "PUBLISHED" or course.status != "PUBLISHED"):
        raise HTTPException(404)
    units = services.assignment_units(db, a, vis)
    task_groups = {t.target_group_id for t in acl.active_tasks(db, ctx.user.id, a.id)} if ctx.is_ta else set()
    batch = services.latest_batch(db, a.id)
    revcounts = {}
    for u in units:
        if u.submission:
            revcounts[u.submission.id] = db.scalar(select(func.max(Revision.revision_no)).where(Revision.submission_id == u.submission.id))
    slots = reminders.schedule(db, a) if manage else []
    return render(request, "staff/assignment.html", ctx, a=a, course=course, units=units, manage=manage,
                  task_groups=task_groups, batch=batch, revcounts=revcounts, slots=slots,
                  reminder_blocked=reminders.reason_not_sent(db, a), reminder_role=reminders.role_of(db, a),
                  hours_label=reminders.hours_label,
                  questions=services.questions_of(db, a.id))


# ------------------------------------------------------------------ grading tasks
@router.get("/assignments/{assignment_id}/tasks")
def tasks_page(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _managed_assignment(ctx, assignment_id)
    db = ctx.db
    tas = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "TA", User.status != "DISABLED").order_by(User.name)))
    groups = list(db.scalars(select(Group).order_by(Group.ordinal)))
    qs = services.questions_of(db, a.id)
    rows = []
    for t in db.scalars(select(GradingTask).where(GradingTask.assignment_id == a.id).order_by(GradingTask.created_at.desc())):
        qids = set(db.scalars(select(GradingTaskQuestion.question_id).where(GradingTaskQuestion.task_id == t.id)))
        rows.append({"t": t, "grader": db.get(User, t.grader_id), "g": db.get(Group, t.target_group_id),
                     "qs": [q for q in qs if q.id in qids]})
    return render(request, "staff/tasks.html", ctx, a=a, course=db.get(Course, a.course_id), tas=tas, groups=groups,
                  qs=qs, rows=rows)


@router.post("/assignments/{assignment_id}/tasks")
def task_create(assignment_id: int, grader_id: int = Form(...), group_ids: list[int] = Form([]),
                question_ids: list[int] = Form([]), can_grade_total: str = Form(""), csrf_token: str = Form(""),
                ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    a = _managed_assignment(ctx, assignment_id)
    db = ctx.db
    grader = db.get(User, grader_id)
    if grader is None or "TA" not in grader.role_set or grader.status == "DISABLED":
        raise HTTPException(400, "批改者必須是有效的助教帳號")
    valid_q = {q.id for q in services.questions_of(db, a.id)}
    if not set(question_ids) <= valid_q:  # 題目必須屬於同一份作業
        raise HTTPException(400, "題目不屬於此作業")
    valid_g = set(db.scalars(select(Group.id)))
    if not group_ids or not set(group_ids) <= valid_g:
        raise HTTPException(400, "請選擇有效的組別")
    if not question_ids and can_grade_total != "1":
        raise HTTPException(400, "請至少選擇一題，或授予總分權限")
    for gid in sorted(set(group_ids)):
        t = GradingTask(grader_id=grader.id, assignment_id=a.id, target_group_id=gid,
                        can_grade_total=can_grade_total == "1", created_by=ctx.user.id)
        db.add(t)
        db.flush()
        for qid in sorted(set(question_ids)):
            db.add(GradingTaskQuestion(task_id=t.id, question_id=qid))
        audit(db, ctx.user.id, "grading_task.create", "grading_task", t.id,
              {"grader": grader.id, "group": gid, "questions": sorted(set(question_ids)), "total": t.can_grade_total}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/tasks?msg=created", 303)


@router.post("/tasks/{task_id}/revoke")
def task_revoke(task_id: int, csrf_token: str = Form(""), ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    t = ctx.db.get(GradingTask, task_id)
    if t is None:
        raise HTTPException(404)
    a = _managed_assignment(ctx, t.assignment_id)
    if t.revoked_at is None:
        t.revoked_at = utcnow()
        audit(ctx.db, ctx.user.id, "grading_task.revoke", "grading_task", t.id, {}, ctx.ip)
        ctx.db.commit()
    return RedirectResponse(f"/staff/assignments/{a.id}/tasks?msg=revoked", 303)


# ------------------------------------------------------------------ staff statistics
@router.get("/assignments/{assignment_id}/stats")
def stats_page(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _managed_assignment(ctx, assignment_id)
    db = ctx.db
    units = services.assignment_units(db, a)
    qs = services.questions_of(db, a.id)
    submitted = [u for u in units if u.latest is not None]
    on_time = sum(1 for u in submitted if not u.latest.is_late)
    late = len(submitted) - on_time
    rev_counts = Counter()
    for u in submitted:
        n = db.scalar(select(func.max(Revision.revision_no)).where(Revision.submission_id == u.submission.id)) or 0
        rev_counts[n] += 1
    q_stats = []
    for q in qs:
        vals = [u.scores.items[q.id].score for u in units
                if u.scores and q.id in u.scores.items and u.scores.items[q.id].score is not None]
        avg = sum(vals, Decimal("0")) / len(vals) if vals else None
        q_stats.append({"q": q, "n": len(vals), "avg": avg,
                        "rate": (avg / q.max_points * 100) if avg is not None else None,
                        "zeros": sum(1 for v in vals if v == 0)})
    finals = [u.scores.final for u in units if u.scores and u.scores.final is not None]
    max_total = sum((q.max_points for q in qs), Decimal("0")) or Decimal("100")
    buckets = Counter()
    for v in finals:
        pct = float(v / max_total * 100) if max_total else 0
        buckets[min(int(pct // 10) * 10, 100)] += 1
    graders = Counter()
    rev_ids = [u.target.id for u in units if u.target]
    if rev_ids:
        for gi in db.scalars(select(GradeItem).where(GradeItem.revision_id.in_(rev_ids), GradeItem.score.is_not(None))):
            if gi.grader_id:
                graders[gi.grader_id] += 1
    grader_rows = [(db.get(User, uid), n) for uid, n in graders.most_common()]
    return render(request, "staff/stats.html", ctx, a=a, course=db.get(Course, a.course_id), units=units,
                  submitted=len(submitted), on_time=on_time, late=late, rev_counts=sorted(rev_counts.items()),
                  q_stats=q_stats, finals=finals, buckets=[(b, buckets.get(b, 0)) for b in range(0, 101, 10)],
                  grader_rows=grader_rows, max_total=max_total)
