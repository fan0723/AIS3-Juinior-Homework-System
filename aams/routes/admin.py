"""管理員：帳號、CSV 匯入（insert-only）、分組、課程、稽核、匯出、進度。全部需 ADMIN + MFA。"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select

from .. import acl, services
from ..config import settings
from ..deps import Ctx, admin_ctx, audit, render, require_csrf, revoke_user_sessions
from ..models import (
    ROLES,
    Artifact,
    Assignment,
    AuditEvent,
    Course,
    CourseInstructor,
    Group,
    GroupMembership,
    GroupStaff,
    MfaFactor,
    Revision,
    Submission,
    User,
    UserRole,
)
from ..security import fmt_wait, hash_password, login_fail_lock, new_token, verify_password, webhook_pw_lock
from ..utils import canonical_email, clean_text, csv_safe, fmt_dt, fmt_score, utcnow, valid_email

router = APIRouter(prefix="/admin")

PHONE_RE = re.compile(r"^[0-9+\-() #x]{0,40}$")


@router.get("")
def admin_home(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    counts = {
        "users": db.scalar(select(func.count()).select_from(User)),
        "pending": db.scalar(select(func.count()).select_from(User).where(User.status == "PENDING_PASSWORD_CHANGE")),
        "groups": db.scalar(select(func.count()).select_from(Group)),
        "courses": db.scalar(select(func.count()).select_from(Course)),
        "assignments": db.scalar(select(func.count()).select_from(Assignment)),
        "revisions": db.scalar(select(func.count()).select_from(Revision)),
        "pending_scan": db.scalar(select(func.count()).select_from(Revision).where(Revision.scan_state == "PENDING_SCAN")),
        "rejected": db.scalar(select(func.count()).select_from(Revision).where(Revision.scan_state == "REJECTED")),
    }
    return render(request, "admin/home.html", ctx, counts=counts)


# ------------------------------------------------------------------ users
@router.get("/users")
def users(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    q = request.query_params.get("q", "").strip()[:100]
    role = request.query_params.get("role", "")
    stmt = select(User).order_by(User.id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(User.canonical_email.like(like), func.lower(User.name).like(like)))
    if role in ROLES:
        stmt = stmt.join(UserRole, UserRole.user_id == User.id).where(UserRole.role == role)
    rows = []
    for u in db.scalars(stmt.limit(500)):
        g = acl.active_group_of(db, u.id)
        rows.append({"u": u, "g": g})
    return render(request, "admin/users.html", ctx, rows=rows, q=q, role=role, groups=list(db.scalars(select(Group).order_by(Group.ordinal))))


def _create_user(ctx: Ctx, email: str, name: str, roles: list[str], school: str, grade: str, phone: str,
                 password: str, group_code: str = "") -> User:
    db = ctx.db
    u = User(email=email.strip(), canonical_email=canonical_email(email), name=name, school=school, grade=grade,
             phone=phone, status="PENDING_PASSWORD_CHANGE", password_hash=hash_password(password))
    db.add(u)
    db.flush()
    for r in roles:
        db.add(UserRole(user_id=u.id, role=r))
    if group_code:
        g = db.scalar(select(Group).where(Group.code == group_code))
        if g is not None:
            if "STUDENT" in roles:
                db.add(GroupMembership(group_id=g.id, student_id=u.id))
            elif "TA" in roles:
                db.add(GroupStaff(group_id=g.id, ta_id=u.id))
    return u


def _validate_user_fields(email, name, school, grade, phone) -> list[str]:
    errs = []
    if not valid_email(email):
        errs.append("Email 格式錯誤")
    if not name or len(name) > 100:
        errs.append("姓名需 1–100 字")
    if len(school) > 120 or len(grade) > 20:
        errs.append("學校／年級過長")
    if not PHONE_RE.match(phone):
        errs.append("電話格式錯誤")
    return errs


@router.post("/users")
def user_create(request: Request, email: str = Form(""), name: str = Form(""), roles: list[str] = Form([]),
                school: str = Form(""), grade: str = Form(""), phone: str = Form(""), group_code: str = Form(""),
                initial_password: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    email, name = clean_text(email, 254), clean_text(name, 100)
    school, grade, phone = clean_text(school, 120), clean_text(grade, 20), clean_text(phone, 40)
    errs = _validate_user_fields(email, name, school, grade, phone)
    roles = [r for r in roles if r in ROLES]
    if not roles:
        errs.append("請至少選擇一個角色")
    if ctx.db.scalar(select(User.id).where(User.canonical_email == canonical_email(email))):
        errs.append("此 Email 已存在（僅新增，不會覆寫既有帳號）")
    generated = False
    if not initial_password:
        initial_password = secrets.token_urlsafe(9)
        generated = True
    elif not 8 <= len(initial_password) <= 128:
        errs.append("初始密碼長度需 8–128")
    if errs:
        raise HTTPException(400, "；".join(errs))
    u = _create_user(ctx, email, name, roles, school, grade, phone, initial_password, clean_text(group_code, 8))
    audit(ctx.db, ctx.user.id, "user.create", "user", u.id, {"roles": roles}, ctx.ip)
    ctx.db.commit()
    return render(request, "admin/secret_once.html", ctx, user=u,
                  secret=initial_password if generated else None, title="已建立帳號")


@router.get("/users/{user_id}")
def user_detail(user_id: int, request: Request, ctx: Ctx = Depends(admin_ctx)):
    u = ctx.db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    factor = ctx.db.scalar(select(MfaFactor).where(MfaFactor.user_id == u.id, MfaFactor.enabled_at.is_not(None)))
    events = list(ctx.db.scalars(select(AuditEvent).where(or_(AuditEvent.actor_id == u.id,
                                                               (AuditEvent.target_type == "user") & (AuditEvent.target_id == str(u.id))))
                                 .order_by(AuditEvent.created_at.desc()).limit(50)))
    return render(request, "admin/user_detail.html", ctx, u=u, mfa=factor is not None, events=events,
                  group=acl.active_group_of(ctx.db, u.id), ta_groups=[ctx.db.get(Group, g) for g in acl.ta_group_ids(ctx.db, u.id)])


@router.post("/users/{user_id}/edit")
def user_edit(user_id: int, email: str = Form(""), name: str = Form(""), roles: list[str] = Form([]),
              school: str = Form(""), grade: str = Form(""), phone: str = Form(""), csrf_token: str = Form(""),
              ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    email, name = clean_text(email, 254), clean_text(name, 100)
    school, grade, phone = clean_text(school, 120), clean_text(grade, 20), clean_text(phone, 40)
    errs = _validate_user_fields(email, name, school, grade, phone)
    other = db.scalar(select(User.id).where(User.canonical_email == canonical_email(email), User.id != u.id))
    if other:
        errs.append("此 Email 已被其他帳號使用")
    roles = {r for r in roles if r in ROLES}
    if not roles:
        errs.append("請至少選擇一個角色")
    if u.id == ctx.user.id and "ADMIN" not in roles:
        errs.append("不可移除自己的管理員角色")
    if errs:
        raise HTTPException(400, "；".join(errs))
    before_roles = u.role_set
    u.email, u.canonical_email, u.name, u.school, u.grade, u.phone = email, canonical_email(email), name, school, grade, phone
    for r in list(u.roles):
        if r.role not in roles:
            db.delete(r)
    for r in roles - before_roles:
        db.add(UserRole(user_id=u.id, role=r))
    if roles != before_roles:  # 角色重大異動 → 撤銷 Session
        u.credential_version += 1
        revoke_user_sessions(db, u.id)
    audit(db, ctx.user.id, "user.edit", "user", u.id, {"roles": [sorted(before_roles), sorted(roles)]}, ctx.ip)
    db.commit()
    return RedirectResponse(f"/admin/users/{u.id}?msg=saved", 303)


@router.post("/users/{user_id}/status")
def user_status(user_id: int, action: str = Form(...), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    u = ctx.db.get(User, user_id)
    if u is None or u.id == ctx.user.id:
        raise HTTPException(400, "不可停用自己")
    if action == "disable":
        u.status = "DISABLED"
        revoke_user_sessions(ctx.db, u.id)
    elif action == "enable" and u.status == "DISABLED":
        u.status = "ACTIVE" if u.activated_at else "PENDING_PASSWORD_CHANGE"
    else:
        raise HTTPException(400)
    audit(ctx.db, ctx.user.id, f"user.{action}", "user", u.id, {}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/admin/users/{u.id}?msg=saved", 303)


@router.post("/users/{user_id}/reset-password")
def user_reset_password(user_id: int, request: Request, verification_note: str = Form(""), csrf_token: str = Form(""),
                        ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    u = ctx.db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    note = clean_text(verification_note, 500)
    if len(note) < 4:
        raise HTTPException(400, "請填寫核身紀錄（例如：已於服務台核對學生證）")
    temp = secrets.token_urlsafe(9)
    u.password_hash = hash_password(temp)
    u.status = "PENDING_PASSWORD_CHANGE"
    u.credential_version += 1
    revoke_user_sessions(ctx.db, u.id)
    audit(ctx.db, ctx.user.id, "user.reset_password", "user", u.id, {"verification": note}, ctx.ip)
    ctx.db.commit()
    return render(request, "admin/secret_once.html", ctx, user=u, secret=temp, title="已重設密碼")


@router.post("/users/{user_id}/reset-mfa")
def user_reset_mfa(user_id: int, verification_note: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    u = ctx.db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    if u.id == ctx.user.id:
        raise HTTPException(400, "不可重設自己的 MFA，請由另一位管理員操作")
    note = clean_text(verification_note, 500)
    if len(note) < 4:
        raise HTTPException(400, "請填寫核身紀錄")
    f = ctx.db.scalar(select(MfaFactor).where(MfaFactor.user_id == u.id))
    if f is not None:
        ctx.db.delete(f)
    u.credential_version += 1
    revoke_user_sessions(ctx.db, u.id)
    audit(ctx.db, ctx.user.id, "user.reset_mfa", "user", u.id, {"verification": note}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse(f"/admin/users/{u.id}?msg=saved", 303)


# ------------------------------------------------------------------ CSV import (insert-only)
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_LOCK = threading.Lock()
CSV_FIELDS = ["email", "name", "role", "school", "grade", "group_code", "initial_password", "phone"]


def _gc_previews():
    now = time.time()
    with _PREVIEW_LOCK:
        for k in [k for k, v in _PREVIEWS.items() if v["exp"] < now]:
            _PREVIEWS.pop(k, None)


@router.get("/import")
def import_form(request: Request, ctx: Ctx = Depends(admin_ctx)):
    return render(request, "admin/import.html", ctx, preview=None)


@router.post("/import")
def import_preview(request: Request, file: UploadFile = File(...), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    raw = file.file.read(settings.max_csv_bytes + 1)
    if len(raw) > settings.max_csv_bytes:
        raise HTTPException(413, "CSV 檔案過大")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV 必須是 UTF-8 編碼")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or [f.strip() for f in reader.fieldnames] != CSV_FIELDS:
        raise HTTPException(400, "CSV 欄位必須為：" + ",".join(CSV_FIELDS))
    db = ctx.db
    existing = set(db.scalars(select(User.canonical_email)))
    group_codes = set(db.scalars(select(Group.code)))
    rows, seen = [], set()
    for i, r in enumerate(reader, start=2):
        if i - 1 > settings.max_csv_rows:
            raise HTTPException(400, f"CSV 超過 {settings.max_csv_rows} 列")
        r = {k.strip(): (v or "") for k, v in r.items() if k}
        email = clean_text(r.get("email"), 254)
        canon = canonical_email(email)
        item = {
            "line": i, "email": email, "name": clean_text(r.get("name"), 100), "role": clean_text(r.get("role"), 16).upper(),
            "school": clean_text(r.get("school"), 120), "grade": clean_text(r.get("grade"), 20),
            "group_code": clean_text(r.get("group_code"), 8).upper(), "phone": clean_text(r.get("phone"), 40),
            "status": "NEW", "problems": [],
        }
        pw = r.get("initial_password") or ""
        p = item["problems"]
        if len(r.get("name") or "") > 100 or len(r.get("school") or "") > 120:
            p.append("欄位過長")
        p += _validate_user_fields(email, item["name"], item["school"], item["grade"], item["phone"])
        if item["role"] not in ROLES:
            p.append("非法角色")
        if not 8 <= len(pw) <= 128:
            p.append("初始密碼長度需 8–128（請以文字格式保存，避免前導零遺失）")
        if item["group_code"] and item["group_code"] not in group_codes:
            p.append("組別不存在")
        if canon in seen:
            p.append("檔案內 Email 重複")
        if p:
            item["status"] = "ERROR"
        elif canon in existing:
            item["status"] = "DUPLICATE"
        seen.add(canon)
        item["_pw"] = pw
        rows.append(item)
    _gc_previews()
    token = new_token(16)
    with _PREVIEW_LOCK:  # 明文密碼只存在記憶體，10 分鐘後失效，不落地
        _PREVIEWS[token] = {"admin": ctx.user.id, "rows": rows, "exp": time.time() + 600}
    summary = {
        "total": len(rows),
        "new": sum(1 for r in rows if r["status"] == "NEW"),
        "dup": sum(1 for r in rows if r["status"] == "DUPLICATE"),
        "err": sum(1 for r in rows if r["status"] == "ERROR"),
    }
    audit(db, ctx.user.id, "import.preview", "csv", "", summary, ctx.ip)
    db.commit()
    return render(request, "admin/import.html", ctx, preview={"token": token, "rows": rows, "summary": summary})


@router.post("/import/commit")
def import_commit(token: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    with _PREVIEW_LOCK:
        data = _PREVIEWS.pop(token, None)  # 一次性：防重送
    if data is None or data["admin"] != ctx.user.id or data["exp"] < time.time():
        raise HTTPException(409, "預覽已失效或已匯入，請重新上傳 CSV")
    rows = data["rows"]
    if any(r["status"] == "ERROR" for r in rows):
        raise HTTPException(400, "CSV 仍有錯誤列，請修正後重新預覽")
    db = ctx.db
    created = 0
    try:
        for r in rows:
            if r["status"] != "NEW":
                continue
            if db.scalar(select(User.id).where(User.canonical_email == canonical_email(r["email"]))):
                continue  # 再次確認 insert-only
            _create_user(ctx, r["email"], r["name"], [r["role"]], r["school"], r["grade"], r["phone"], r["_pw"],
                         r["group_code"])
            created += 1
        audit(db, ctx.user.id, "import.commit", "csv", "", {"created": created}, ctx.ip)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        for r in rows:
            r["_pw"] = ""
    return RedirectResponse("/admin/users?msg=imported", 303)


# ------------------------------------------------------------------ groups
@router.get("/groups")
def groups_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    rows = []
    for g in db.scalars(select(Group).order_by(Group.ordinal)):
        rows.append({"g": g, "members": [db.get(User, i) for i in acl.group_member_ids(db, g.id)],
                     "tas": [db.get(User, i) for i in acl.group_ta_ids(db, g.id)]})
    students = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "STUDENT").order_by(User.name)))
    tas = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "TA").order_by(User.name)))
    return render(request, "admin/groups.html", ctx, rows=rows, students=students, tas=tas)


@router.post("/groups/create")
def groups_create(count: int = Form(12), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    if not 1 <= count <= 99:
        raise HTTPException(400)
    db = ctx.db
    existing = set(db.scalars(select(Group.ordinal)))
    for n in range(1, count + 1):
        if n not in existing:
            db.add(Group(code=f"G{n:02d}", ordinal=n))
    audit(db, ctx.user.id, "group.create", "group", "", {"count": count}, ctx.ip)
    db.commit()
    return RedirectResponse("/admin/groups?msg=created", 303)


@router.post("/groups/assign-student")
def group_assign_student(student_id: int = Form(...), group_id: int = Form(0), csrf_token: str = Form(""),
                         ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    u = db.get(User, student_id)
    if u is None or "STUDENT" not in u.role_set:
        raise HTTPException(400, "必須是學員帳號")
    now = utcnow()
    old = acl.active_group_of(db, u.id)
    for m in db.scalars(select(GroupMembership).where(GroupMembership.student_id == u.id, GroupMembership.ended_at.is_(None))):
        m.ended_at = now
    if group_id:
        if db.get(Group, group_id) is None:
            raise HTTPException(400)
        db.add(GroupMembership(group_id=group_id, student_id=u.id, joined_at=now))
    audit(db, ctx.user.id, "group.member_change", "user", u.id, {"from": old.code if old else None, "to": group_id}, ctx.ip)
    db.commit()
    return RedirectResponse("/admin/groups?msg=saved", 303)


@router.post("/groups/{group_id}/ta")
def group_ta(group_id: int, ta_id: int = Form(...), action: str = Form("add"), csrf_token: str = Form(""),
             ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    u = db.get(User, ta_id)
    if u is None or "TA" not in u.role_set or db.get(Group, group_id) is None:
        raise HTTPException(400, "必須是助教帳號")
    if action == "add":
        if ta_id not in acl.group_ta_ids(db, group_id):
            db.add(GroupStaff(group_id=group_id, ta_id=ta_id))
    else:
        for s in db.scalars(select(GroupStaff).where(GroupStaff.group_id == group_id, GroupStaff.ta_id == ta_id, GroupStaff.ended_at.is_(None))):
            s.ended_at = utcnow()
    audit(db, ctx.user.id, f"group.ta_{action}", "group", group_id, {"ta": ta_id}, ctx.ip)
    db.commit()
    return RedirectResponse("/admin/groups?msg=saved", 303)


# ------------------------------------------------------------------ courses
@router.get("/courses")
def courses_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    rows = []
    for c in db.scalars(select(Course).order_by(Course.ordinal, Course.id)):
        ins = [db.get(User, i) for i in db.scalars(select(CourseInstructor.instructor_id).where(CourseInstructor.course_id == c.id))]
        rows.append({"c": c, "instructors": ins})
    instructors = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "INSTRUCTOR").order_by(User.name)))
    return render(request, "admin/courses.html", ctx, rows=rows, instructors=instructors)


def _name_en(value: str) -> str:
    from .staff import NAME_EN_RE

    value = (value or "").strip()
    if value and not NAME_EN_RE.fullmatch(value):
        raise HTTPException(400, "英文名稱只能使用英文字母、數字、底線與連字號（最多 40 字）")
    return value


@router.post("/courses")
def course_create(title: str = Form(""), description: str = Form(""), ordinal: int = Form(0), name_en: str = Form(""),
                  csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    title = clean_text(title, 200)
    if not title:
        raise HTTPException(400, "請輸入課程名稱")
    c = Course(title=title, description=clean_text(description, 5000, multiline=True), ordinal=ordinal,
               name_en=_name_en(name_en))
    ctx.db.add(c)
    ctx.db.flush()
    audit(ctx.db, ctx.user.id, "course.create", "course", c.id, {}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/admin/courses?msg=created", 303)


@router.post("/courses/{course_id}/update")
def course_update(course_id: int, title: str = Form(""), status: str = Form("DRAFT"), ordinal: int = Form(0),
                  name_en: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    c = ctx.db.get(Course, course_id)
    if c is None:
        raise HTTPException(404)
    c.title = clean_text(title, 200) or c.title
    c.status = "PUBLISHED" if status == "PUBLISHED" else "DRAFT"
    c.ordinal = ordinal
    c.name_en = _name_en(name_en)
    audit(ctx.db, ctx.user.id, "course.update", "course", c.id, {"status": c.status, "name_en": c.name_en}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/admin/courses?msg=saved", 303)


@router.post("/courses/{course_id}/instructors")
def course_instructor(course_id: int, instructor_id: int = Form(...), action: str = Form("add"), csrf_token: str = Form(""),
                      ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    u = db.get(User, instructor_id)
    if u is None or "INSTRUCTOR" not in u.role_set or db.get(Course, course_id) is None:
        raise HTTPException(400, "必須是講師帳號")
    row = db.scalar(select(CourseInstructor).where(CourseInstructor.course_id == course_id, CourseInstructor.instructor_id == u.id))
    if action == "add" and row is None:
        db.add(CourseInstructor(course_id=course_id, instructor_id=u.id))
    elif action == "remove" and row is not None:
        db.delete(row)
    audit(db, ctx.user.id, f"course.instructor_{action}", "course", course_id, {"instructor": u.id}, ctx.ip)
    db.commit()
    return RedirectResponse("/admin/courses?msg=saved", 303)


# ------------------------------------------------------------------ audit / progress
@router.get("/audit")
def audit_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    action = request.query_params.get("action", "").strip()[:64]
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(500)
    if action:
        stmt = stmt.where(AuditEvent.action.like(f"{action}%"))
    events = list(ctx.db.scalars(stmt))
    actors = {e.actor_id: ctx.db.get(User, e.actor_id) for e in events if e.actor_id}
    return render(request, "admin/audit.html", ctx, events=events, actors=actors, action=action)


@router.get("/progress")
def progress_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    assignments = list(db.scalars(select(Assignment).where(Assignment.status == "PUBLISHED").order_by(Assignment.course_id, Assignment.due_at)))
    students = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "STUDENT").order_by(User.name)))
    status: dict[tuple[int, int], str] = {}
    for a in assignments:
        for sub in db.scalars(select(Submission).where(Submission.assignment_id == a.id)):
            rev = db.get(Revision, sub.active_revision_id) if sub.active_revision_id else None
            if rev is None:
                continue
            s = "LATE" if rev.is_late else "OK"
            if rev.scan_state != "ACCEPTED":
                s += "?"
            ids = [sub.student_id] if sub.owner_type == "INDIVIDUAL" else acl.group_member_ids(db, sub.group_id)
            for sid in ids:
                status[(sid, a.id)] = s
    groups = {s.id: acl.active_group_of(db, s.id) for s in students}
    return render(request, "admin/progress.html", ctx, assignments=assignments, students=students, status=status, groups=groups)


# ------------------------------------------------------------------ exports
@router.get("/exports")
def exports_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    db = ctx.db
    assignments = list(db.scalars(select(Assignment).order_by(Assignment.course_id, Assignment.due_at)))
    courses = {c.id: c for c in db.scalars(select(Course))}
    students = list(db.scalars(select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role == "STUDENT").order_by(User.name)))
    groups = list(db.scalars(select(Group).order_by(Group.ordinal)))
    return render(request, "admin/exports.html", ctx, assignments=assignments, courses=courses, students=students, groups=groups)


def _csv_response(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@router.post("/exports/grades")
def export_grades(assignment_id: int = Form(...), group_ids: list[int] = Form([]), csrf_token: str = Form(""),
                  ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    a = db.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(404)
    qs = services.questions_of(db, a.id)
    units = services.assignment_units(db, a, set(group_ids) if group_ids else None)
    batch = services.latest_batch(db, a.id)
    released: dict[int, str] = {}
    if batch:
        from ..models import ReleasedGrade

        for rg in db.scalars(select(ReleasedGrade).where(ReleasedGrade.batch_id == batch.id)):
            released[rg.student_id] = fmt_score(rg.score)
    header = ["course", "assignment", "group", "email", "name", "revision_no", "is_late", "scan_state"]
    header += [f"Q{q.ordinal}" for q in qs] + ["base_score", "manual_final_score", "final_score", "member_override",
                                               f"released_score(release#{batch.release_no if batch else '-'})", "visibility"]
    course = db.get(Course, a.course_id)
    out = [header]
    from ..models import GroupMemberOverride

    for u in units:
        ovs = {}
        if u.target:
            ovs = {o.student_id: o for o in db.scalars(select(GroupMemberOverride).where(GroupMemberOverride.revision_id == u.target.id))}
        for m in u.members:
            g = acl.active_group_of(db, m.id)
            row = [course.title, a.title, g.code if g else "", m.email, m.name,
                   u.target.revision_no if u.target else "", (u.target.is_late if u.target else ""),
                   (u.latest.scan_state if u.latest else "NO_SUBMISSION")]
            for q in qs:
                gi = u.scores.items.get(q.id) if u.scores else None
                row.append(fmt_score(gi.score) if gi and gi.score is not None else "")
            sv = u.scores
            row += [fmt_score(sv.base) if sv and sv.base is not None else "",
                    fmt_score(sv.total.manual_final_score) if sv and sv.total and sv.total.manual_final_score is not None else "",
                    fmt_score(sv.final) if sv and sv.final is not None else "",
                    fmt_score(ovs[m.id].manual_final_score) if m.id in ovs else "",
                    released.get(m.id, ""), a.student_grade_visibility]
            out.append(row)
    audit(db, ctx.user.id, "export.grades", "assignment", a.id, {"rows": len(out) - 1, "groups": group_ids}, ctx.ip)
    db.commit()
    return _csv_response(out, f"grades-a{a.id}-{_stamp()}.csv")


@router.post("/exports/submissions")
def export_submissions(csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    out = [["course", "assignment", "mode", "owner", "revision_no", "uploader_email", "finalized_at(Asia/Taipei)",
            "is_late", "late_reclassified_reason", "scan_state", "total_bytes", "files", "sha256_list", "purged"]]
    for a in db.scalars(select(Assignment).order_by(Assignment.course_id, Assignment.id)):
        course = db.get(Course, a.course_id)
        for sub in db.scalars(select(Submission).where(Submission.assignment_id == a.id)):
            owner = services.owner_label(db, sub)
            for r in db.scalars(select(Revision).where(Revision.submission_id == sub.id).order_by(Revision.revision_no)):
                arts = list(db.scalars(select(Artifact).where(Artifact.revision_id == r.id)))
                up = db.get(User, r.uploader_id)
                out.append([course.title, a.title, a.submission_mode, owner, r.revision_no, up.email if up else "",
                            fmt_dt(r.finalized_at), r.is_late, r.late_reclassified_reason, r.scan_state, r.total_bytes,
                            " | ".join(x.original_filename for x in arts), " | ".join(x.sha256 for x in arts),
                            bool(r.purged_at)])
    audit(db, ctx.user.id, "export.submissions", "all", "", {"rows": len(out) - 1}, ctx.ip)
    db.commit()
    return _csv_response(out, f"submissions-{_stamp()}.csv")


@router.post("/exports/users")
def export_users(include_phone: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    phone = include_phone == "1"
    header = ["email", "name", "roles", "school", "grade", "group", "status"] + (["phone"] if phone else [])
    out = [header]
    for u in db.scalars(select(User).order_by(User.id)):
        g = acl.active_group_of(db, u.id)
        row = [u.email, u.name, " ".join(sorted(u.role_set)), u.school, u.grade, g.code if g else "", u.status]
        if phone:
            row.append(u.phone)
        out.append(row)
    audit(db, ctx.user.id, "export.users", "all", "", {"rows": len(out) - 1, "include_phone": phone}, ctx.ip)
    db.commit()
    return _csv_response(out, f"users-{_stamp()}.csv")


@router.get("/system")
def system_page(request: Request, ctx: Ctx = Depends(admin_ctx)):
    info = {
        "database": "SQLite" if settings.is_sqlite else "PostgreSQL",
        "data_dir": str(settings.data_dir),
        "inspector_mode": settings.inspector_mode,
        "cookie_secure": settings.cookie_secure,
        "system_types": ", ".join(settings.system_allowed_types),
        "event_end_at": settings.event_end_at or "（未設定）",
    }
    from .. import monitor, notify, reminders, sysconfig

    current = monitor.check(ctx.db)
    channels = []
    for ch, label, use in (("student", "學員公告頻道", "截止提醒、公告、成績發布"), ("staff", "staff 頻道", "監控警示、成績申覆")):
        row = sysconfig.updated_info(ctx.db, ch)
        url = notify.webhook_url(ch)
        channels.append({
            "ch": ch, "label": label, "use": use, "source": sysconfig.webhook_source(ch), "ok": bool(url),
            "webhook_id": sysconfig.webhook_id(url),  # Webhook ID 不是機密；Token 永遠不顯示
            "undecryptable": sysconfig.web_undecryptable(ch),
            "updated_at": row.updated_at if row else None,
            "updated_by": ctx.db.get(User, row.updated_by) if row and row.updated_by else None,
        })
    discord = {"staff": notify.configured("staff"), "student": notify.configured("student"),
               "invalid": notify.invalid_config(), "status": dict(notify.status), "channels": channels}
    rrow = sysconfig.updated_info(ctx.db, sysconfig.REMINDER_KEY)
    reminder = {"raw": sysconfig.reminder_hours_raw(), "source": sysconfig.reminder_source(),
                "label": "、".join(reminders.hours_label(h) for h in reminders.default_hours()),
                "updated_at": rrow.updated_at if rrow else None,
                "updated_by": ctx.db.get(User, rrow.updated_by) if rrow and rrow.updated_by else None}
    return render(request, "admin/system.html", ctx, info=info, cfg=json.dumps(info, ensure_ascii=False),
                  current=current, recent=list(monitor.recent), discord=discord, reminder=reminder,
                  locks=_lock_rows(ctx.db), fmt_wait=fmt_wait,
                  event_end_ok=settings.event_end_utc is not None)


def _lock_rows(db) -> dict:
    """目前被鎖住的登入（帳號＋IP）與 webhook 密碼確認（管理員）。"""
    login = []
    for key, secs, tier in sorted(login_fail_lock.locked(), key=lambda x: -x[1]):
        email, _, ip = key.rpartition("|")
        u = db.scalar(select(User).where(User.canonical_email == email))
        login.append({"key": key, "email": email, "ip": ip, "user": u, "secs": secs, "tier": tier})
    webhook = []
    for key, secs, tier in webhook_pw_lock.locked():
        u = db.get(User, int(key)) if key.isdigit() else None
        webhook.append({"key": key, "user": u, "secs": secs, "tier": tier})
    return {"login": login, "webhook": webhook}


@router.post("/system/unlock")
def system_unlock(kind: str = Form(""), key: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    """解除密碼錯誤鎖定。webhook 密碼確認的鎖定只能由「另一位」管理員解除。"""
    require_csrf(ctx.sess, csrf_token)
    if kind == "login":
        if not any(k == key for k, _, _ in login_fail_lock.locked()):
            raise HTTPException(404, "這筆鎖定已經解除或過期了")
        login_fail_lock.reset(key)
        email, _, ip = key.rpartition("|")
        audit(ctx.db, ctx.user.id, "auth.unlock", "email", email[:64], {"ip": ip}, ctx.ip)
    elif kind == "webhook":
        if key == str(ctx.user.id):
            raise HTTPException(403, "不能解除自己的鎖定，請由另一位管理員解除。")
        if not any(k == key for k, _, _ in webhook_pw_lock.locked()):
            raise HTTPException(404, "這筆鎖定已經解除或過期了")
        webhook_pw_lock.reset(key)
        audit(ctx.db, ctx.user.id, "system.webhook_unlock", "user", key, {}, ctx.ip)
    else:
        raise HTTPException(400, "參數錯誤")
    ctx.db.commit()
    return RedirectResponse("/admin/system?msg=unlocked#locks", 303)


_CH_LABEL = {"staff": "staff 頻道", "student": "學員公告頻道"}


@router.post("/system/discord-webhook")
def system_set_webhook(channel: str = Form(""), action: str = Form("set"), url: str = Form(""),
                       password: str = Form(""), confirm: str = Form(""), csrf_token: str = Form(""),
                       ctx: Ctx = Depends(admin_ctx)):
    """設定／更換／停用 Discord webhook（僅管理員）。網址加密存放，之後不再顯示。"""
    require_csrf(ctx.sess, csrf_token)
    from .. import notify, sysconfig

    db = ctx.db
    if channel not in _CH_LABEL or action not in ("set", "clear"):
        raise HTTPException(400, "參數錯誤")
    if sysconfig.env_locked(channel):
        raise HTTPException(409, "這個頻道由伺服器 .env 設定，網頁上不能修改。要改請編輯 .env 後重新啟動。")
    url = (url or "").strip()
    if action == "set" and not sysconfig.valid_webhook(url):
        raise HTTPException(400, "Webhook 網址格式不正確：必須是 https://discord.com/api/webhooks/數字/Token"
                                 "（在 Discord 頻道設定 › 整合 › Webhook ›「複製 Webhook 網址」）")
    old_url = notify.webhook_url(channel)
    had_value = sysconfig.webhook_source(channel) == "web"
    if action == "clear" and not had_value:
        raise HTTPException(400, "這個頻道目前沒有網頁設定")
    if had_value and confirm != "yes":
        raise HTTPException(400, "這個頻道已經設定過：請勾選「我了解更換／停用的影響」再送出。")
    key = str(ctx.user.id)
    wait = webhook_pw_lock.remaining(key)
    if wait:
        raise HTTPException(429, f"密碼錯誤次數過多，請 {fmt_wait(wait)}後再試，或請另一位管理員在「系統」頁解除。")
    if not verify_password(ctx.user.password_hash, password):
        locked_for = webhook_pw_lock.fail(key)
        audit(db, ctx.user.id, "system.webhook_bad_password", "system", channel, {"locked_seconds": locked_for}, ctx.ip)
        db.commit()
        if locked_for:
            from .. import notify

            notify.send("staff", f"🔒 管理員 {ctx.user.name} 修改 Discord 設定時密碼連續錯誤，已暫停 {fmt_wait(locked_for)}。"
                                 "若不是本人操作，請立即檢查這個帳號。")
        raise HTTPException(403, "密碼錯誤，設定沒有變更。")
    webhook_pw_lock.reset(key)
    if action == "set" and url == old_url:
        return RedirectResponse("/admin/system?msg=saved#discord", 303)

    # 先通知「舊的」頻道：就算是帳號被盜用的人改的，原本的頻道也會知道
    if old_url:
        verb = "更換為新的 Webhook" if action == "set" else "停用"
        notify.deliver_url(old_url, f"⚠️ 本頻道的作業繳交系統通知已被 {ctx.user.name} {verb}"
                                    f"（{fmt_dt(utcnow())}）。之後的通知不會再發到這裡；若不是你們預期的變更，請立即聯絡管理員。",
                           label=channel)
    sysconfig.set_webhook(db, channel, url if action == "set" else None, ctx.user.id)
    audit(db, ctx.user.id, "system.webhook_" + ("set" if not had_value else "change" if action == "set" else "clear"),
          "system", channel, {"webhook_id": sysconfig.webhook_id(url), "old_webhook_id": sysconfig.webhook_id(old_url)}, ctx.ip)
    db.commit()
    sysconfig.invalidate()
    if action == "clear":
        return RedirectResponse("/admin/system?msg=webhook_cleared#discord", 303)
    ok = notify.deliver(channel, f"✅ 作業繳交系統已連上這個頻道（{_CH_LABEL[channel]}），由 {ctx.user.name} 設定。")
    return RedirectResponse("/admin/system?msg=" + ("webhook_saved" if ok else "webhook_saved_unverified") + "#discord", 303)


@router.post("/system/reminder-hours")
def system_set_reminder_hours(hours: str = Form(""), action: str = Form("set"), csrf_token: str = Form(""),
                              ctx: Ctx = Depends(admin_ctx)):
    """截止提醒系統預設（不是機密，不需要密碼；畫面上有變更警示）。"""
    require_csrf(ctx.sess, csrf_token)
    from .. import reminders, sysconfig

    if sysconfig.reminder_locked():
        raise HTTPException(409, "截止提醒預設由伺服器 .env（AAMS_REMINDER_HOURS）設定，網頁上不能修改。")
    before = sysconfig.reminder_hours_raw()
    after = sysconfig.DEFAULT_REMINDER_HOURS
    if action == "reset":
        sysconfig.set_reminder_hours(ctx.db, None, ctx.user.id)
    else:
        try:
            parsed = reminders.parse_hours(hours)
        except ValueError as e:
            raise HTTPException(400, f"提醒時間格式錯誤：{e}")
        if not parsed:
            raise HTTPException(400, "請至少填一個時間；要整體停用請到各作業設定選「不提醒」。")
        after = ",".join(reminders.hours_key(h) for h in parsed)
        sysconfig.set_reminder_hours(ctx.db, after, ctx.user.id)
    audit(ctx.db, ctx.user.id, "system.reminder_hours", "system", "reminder_hours",
          {"from": before, "to": after}, ctx.ip)
    ctx.db.commit()
    sysconfig.invalidate()
    return RedirectResponse("/admin/system?msg=reminder_saved#reminder", 303)


@router.post("/system/test-discord")
def system_test_discord(channel: str = Form("staff"), csrf_token: str = Form(""), ctx: Ctx = Depends(admin_ctx)):
    require_csrf(ctx.sess, csrf_token)
    from .. import notify

    if channel not in notify.CHANNELS or not notify.configured(channel):
        raise HTTPException(400, "這個頻道尚未設定 Webhook")
    ok = notify.deliver(channel, f"✅ 作業繳交系統測試訊息（{channel} 頻道），由 {ctx.user.name} 從系統頁發送。")
    audit(ctx.db, ctx.user.id, "system.test_discord", "system", channel, {"ok": ok}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/admin/system?msg=" + ("discord_ok" if ok else "discord_fail"), 303)
