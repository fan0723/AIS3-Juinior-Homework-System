"""Business logic: submission finalize, score model, release, statistics."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import acl
from .config import settings
from .deps import Ctx, audit
from .models import (
    Artifact,
    Assignment,
    GradeItem,
    GradeTotal,
    GradingTask,
    Group,
    GroupMemberOverride,
    GroupSnapshot,
    Notification,
    Question,
    ReleaseBatch,
    ReleasedGrade,
    Revision,
    Submission,
    User,
    UserRole,
)
from .storage import purge_revision_files
from .utils import fmt_score, utcnow


# ---------------------------------------------------------------- submissions
def get_or_create_submission(db: Session, a: Assignment, ctx: Ctx) -> Submission:
    if a.submission_mode == "GROUP":
        g = acl.active_group_of(db, ctx.user.id)
        if g is None:
            raise HTTPException(403, "你目前沒有所屬組別，無法提交團體作業")
        sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.group_id == g.id))
        if sub is None:
            sub = Submission(assignment_id=a.id, owner_type="GROUP", group_id=g.id)
    else:
        sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.student_id == ctx.user.id))
        if sub is None:
            sub = Submission(assignment_id=a.id, owner_type="INDIVIDUAL", student_id=ctx.user.id)
    if sub.id is None:
        db.add(sub)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return get_or_create_submission(db, a, ctx)
    return sub


def find_submission(db: Session, a: Assignment, ctx: Ctx) -> Submission | None:
    if a.submission_mode == "GROUP":
        g = acl.active_group_of(db, ctx.user.id)
        if g is None:
            return None
        return db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.group_id == g.id))
    return db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.student_id == ctx.user.id))


@dataclass
class StagedFile:
    key: str
    filename: str
    size: int
    sha256: str
    declared: str


def finalize_revision(ctx: Ctx, a: Assignment, staged: list[StagedFile], idem_key: str) -> Revision:
    """在單一交易內重新檢查所有規則並建立 revision（SEC-022）。"""
    db = ctx.db
    a = db.get(Assignment, a.id)  # reload latest settings
    now = utcnow()
    if not a.accepting_submissions or a.status != "PUBLISHED":
        raise HTTPException(403, "此作業目前不開放收件")
    is_late = now > a.due_at
    if is_late and not a.allow_late:
        raise HTTPException(409, "已超過截止時間，且此作業不接受遲交")
    if now > a.final_deadline_effective:
        raise HTTPException(409, "已超過最終收件期限，系統不再收件")
    if len(staged) > (a.max_files if a.allow_multiple_files else 1):
        raise HTTPException(400, "檔案數超過上限")
    total = sum(s.size for s in staged)
    if total > a.max_total_bytes or total > settings.max_total_bytes_cap:
        raise HTTPException(413, "整次提交總容量超過上限")

    sub = get_or_create_submission(db, a, ctx)
    existing = db.scalar(select(Revision).where(Revision.submission_id == sub.id, Revision.idempotency_key == idem_key))
    if existing is not None:
        return existing  # idempotent replay

    manifest = hashlib.sha256(
        json.dumps(sorted([(s.filename, s.sha256, s.size) for s in staged])).encode()
    ).hexdigest()
    for attempt in range(5):
        next_no = (db.scalar(select(func.max(Revision.revision_no)).where(Revision.submission_id == sub.id)) or 0) + 1
        rev = Revision(
            submission_id=sub.id, revision_no=next_no, uploader_id=ctx.user.id, finalized_at=now,
            due_version=a.due_version, due_at_snapshot=a.due_at, is_late=is_late, scan_state="PENDING_SCAN",
            manifest_hash=manifest, total_bytes=total, idempotency_key=idem_key,
        )
        db.add(rev)
        try:
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            sub = get_or_create_submission(db, a, ctx)
            existing = db.scalar(
                select(Revision).where(Revision.submission_id == sub.id, Revision.idempotency_key == idem_key)
            )
            if existing is not None:
                return existing
    else:
        raise HTTPException(409, "同時提交衝突，請重試")

    for s in staged:
        db.add(Artifact(revision_id=rev.id, private_key=s.key, original_filename=s.filename, size=s.size,
                        sha256=s.sha256, declared_type=s.declared))
    if a.submission_mode == "GROUP":
        members = acl.group_member_ids(db, sub.group_id)
        if ctx.user.id not in members:  # commit 當下仍須是活躍組員
            raise HTTPException(403, "你已不在該組別")
        for sid in members:
            db.add(GroupSnapshot(revision_id=rev.id, student_id=sid))
    sub.active_revision_id = rev.id

    # 通知：本組帶組 TA + 該作業該組有效批改任務者
    gid = acl.submission_group_id(db, sub)
    recipients: set[int] = set()
    if gid is not None:
        recipients |= set(acl.group_ta_ids(db, gid))
        recipients |= set(
            db.scalars(
                select(GradingTask.grader_id).where(
                    GradingTask.assignment_id == a.id, GradingTask.target_group_id == gid,
                    GradingTask.revoked_at.is_(None),
                )
            )
        )
    owner = owner_label(db, sub)
    tag = "（遲交）" if is_late else ""
    for rid in recipients:
        db.add(Notification(recipient_id=rid, submission_id=sub.id, revision_id=rev.id,
                            message=f"{a.title}：{owner} 提交第 {rev.revision_no} 版{tag}，待檔案檢查"))
    audit(db, ctx.user.id, "submission.finalize", "revision", rev.id,
          {"assignment": a.id, "revision_no": rev.revision_no, "late": is_late, "bytes": total}, ctx.ip)
    db.flush()

    # 只保留最近 N 個實體版本
    old = list(
        db.scalars(
            select(Revision).where(Revision.submission_id == sub.id, Revision.purged_at.is_(None))
            .order_by(Revision.revision_no.desc()).offset(settings.retained_versions)
        )
    )
    for r in old:
        purge_revision_files(db, r)
    return rev


def owner_label(db: Session, sub: Submission) -> str:
    if sub.owner_type == "GROUP":
        g = db.get(Group, sub.group_id)
        return g.label if g else "?"
    u = db.get(User, sub.student_id)
    return u.name if u else "?"


# ---------------------------------------------------------------- scores
def questions_of(db: Session, assignment_id: int) -> list[Question]:
    return list(db.scalars(select(Question).where(Question.assignment_id == assignment_id).order_by(Question.ordinal)))


@dataclass
class ScoreView:
    items: dict[int, GradeItem] = field(default_factory=dict)
    total: GradeTotal | None = None
    question_total: Decimal | None = None
    base: Decimal | None = None
    final: Decimal | None = None
    complete: bool = False


def compute_scores(db: Session, a: Assignment, rev_id: int, questions: list[Question] | None = None) -> ScoreView:
    questions = questions if questions is not None else questions_of(db, a.id)
    sv = ScoreView()
    sv.items = {gi.question_id: gi for gi in db.scalars(select(GradeItem).where(GradeItem.revision_id == rev_id))}
    sv.total = db.get(GradeTotal, rev_id)
    scores = [sv.items[q.id].score if q.id in sv.items else None for q in questions]
    if questions and all(s is not None for s in scores):
        sv.question_total = sum(scores, Decimal("0"))
    elif questions and any(s is not None for s in scores):
        sv.question_total = None
    if a.grading_mode == "DIRECT_TOTAL":
        sv.base = sv.total.direct_total if sv.total else None
    else:
        sv.base = sv.question_total
    manual = sv.total.manual_final_score if sv.total else None
    sv.final = manual if manual is not None else sv.base
    sv.complete = sv.final is not None
    return sv


def partial_sum(sv: ScoreView) -> Decimal:
    return sum((gi.score for gi in sv.items.values() if gi.score is not None), Decimal("0"))


# ---------------------------------------------------------------- deadlines
def default_final_deadline(due_at: datetime | None) -> datetime | None:
    """最終收件期限預設值：營隊結束日 + 7 天；未設定營隊結束日時改用「截止時間 + 7 天」。"""
    end = settings.event_end_utc
    if end is not None:
        return end + timedelta(days=settings.final_deadline_days)
    return due_at + timedelta(days=settings.final_deadline_days) if due_at is not None else None


def first_batch(db: Session, assignment_id: int) -> ReleaseBatch | None:
    return db.scalar(
        select(ReleaseBatch).where(ReleaseBatch.assignment_id == assignment_id).order_by(ReleaseBatch.release_no.asc())
    )


def appeal_deadline(db: Session, assignment_id: int) -> datetime | None:
    """成績申覆期限：第一次發布成績後 3 天（重新發布不延長）。"""
    b = first_batch(db, assignment_id)
    return b.created_at + timedelta(days=settings.appeal_days) if b else None


# ---------------------------------------------------------------- release
@dataclass
class Unit:
    label: str
    group_id: int | None
    student_id: int | None
    submission: Submission | None
    latest: Revision | None
    target: Revision | None  # 最新通過檢查的版本（發布對象）
    scores: ScoreView | None
    blockers: list[str]
    members: list[User]


def assignment_units(db: Session, a: Assignment, group_filter: set[int] | None = None) -> list[Unit]:
    questions = questions_of(db, a.id)
    units: list[Unit] = []
    groups = list(db.scalars(select(Group).order_by(Group.ordinal)))
    if a.submission_mode == "GROUP":
        for g in groups:
            if group_filter is not None and g.id not in group_filter:
                continue
            sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.group_id == g.id))
            members = [db.get(User, sid) for sid in acl.group_member_ids(db, g.id)]
            units.append(_unit(db, a, questions, g.label, g.id, None, sub, members))
    else:
        students = list(
            db.scalars(
                select(User).join(UserRole, UserRole.user_id == User.id)
                .where(UserRole.role == "STUDENT", User.status != "DISABLED").order_by(User.name)
            )
        )
        for s in students:
            g = acl.active_group_of(db, s.id)
            if group_filter is not None and (g is None or g.id not in group_filter):
                continue
            sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.student_id == s.id))
            label = f"{s.name}（{g.label if g else '未分組'}）"
            units.append(_unit(db, a, questions, label, g.id if g else None, s.id, sub, [s]))
    return units


def _unit(db, a, questions, label, gid, sid, sub, members) -> Unit:
    latest = target = None
    scores = None
    blockers: list[str] = []
    if sub is not None:
        latest = db.get(Revision, sub.active_revision_id) if sub.active_revision_id else None
        target = db.scalar(
            select(Revision).where(Revision.submission_id == sub.id, Revision.scan_state == "ACCEPTED")
            .order_by(Revision.revision_no.desc())
        )
        if latest is not None and latest.scan_state == "PENDING_SCAN":
            blockers.append(f"第 {latest.revision_no} 版檔案檢查中")
        if target is None:
            if latest is not None and latest.scan_state == "REJECTED":
                blockers.append("沒有通過檔案檢查的版本")
        else:
            scores = compute_scores(db, a, target.id, questions)
            if not scores.complete:
                blockers.append(f"第 {target.revision_no} 版尚未完成評分")
    return Unit(label, gid, sid, sub, latest, target, scores, blockers, members)


def latest_batch(db: Session, assignment_id: int) -> ReleaseBatch | None:
    return db.scalar(
        select(ReleaseBatch).where(ReleaseBatch.assignment_id == assignment_id).order_by(ReleaseBatch.release_no.desc())
    )


def publish(ctx: Ctx, a: Assignment, reason: str) -> ReleaseBatch:
    db = ctx.db
    units = assignment_units(db, a)
    blockers = [(u.label, b) for u in units for b in u.blockers]
    if blockers:
        raise HTTPException(409, "仍有阻擋項目，無法發布")
    prev = latest_batch(db, a.id)
    batch = ReleaseBatch(assignment_id=a.id, release_no=(prev.release_no + 1 if prev else 1),
                         actor_id=ctx.user.id, reason=reason)
    db.add(batch)
    db.flush()
    questions = questions_of(db, a.id)
    count = 0
    for u in units:
        if u.target is None or u.scores is None:
            continue
        sv = u.scores
        snap = {
            "revision_no": u.target.revision_no,
            "is_late": u.target.is_late,
            "questions": [
                {
                    "ordinal": q.ordinal, "title": q.title, "max": str(q.max_points),
                    "score": None if q.id not in sv.items or sv.items[q.id].score is None else str(sv.items[q.id].score),
                    "feedback": sv.items[q.id].feedback_to_student if q.id in sv.items else "",
                }
                for q in questions
            ],
            "base": None if sv.base is None else str(sv.base),
            "final": None if sv.final is None else str(sv.final),
            "summary_feedback": sv.total.summary_feedback if sv.total else "",
            "max_total": str(sum((q.max_points for q in questions), Decimal("0"))),
        }
        overrides = {
            o.student_id: o for o in db.scalars(select(GroupMemberOverride).where(GroupMemberOverride.revision_id == u.target.id))
        }
        for m in u.members:
            score = sv.final
            if m.id in overrides and overrides[m.id].manual_final_score is not None:
                score = overrides[m.id].manual_final_score
            db.add(ReleasedGrade(
                batch_id=batch.id, submission_id=u.submission.id, revision_id=u.target.id,
                revision_no=u.target.revision_no, student_id=m.id, score=score,
                snapshot_json=json.dumps(snap, ensure_ascii=False), visibility=a.student_grade_visibility,
            ))
            count += 1
    audit(db, ctx.user.id, "grades.publish", "assignment", a.id,
          {"release_no": batch.release_no, "rows": count, "reason": reason}, ctx.ip)
    return batch


def student_release(db: Session, a: Assignment, student_id: int) -> tuple[ReleaseBatch, ReleasedGrade] | None:
    row = db.execute(
        select(ReleaseBatch, ReleasedGrade)
        .join(ReleasedGrade, ReleasedGrade.batch_id == ReleaseBatch.id)
        .where(ReleaseBatch.assignment_id == a.id, ReleasedGrade.student_id == student_id)
        .order_by(ReleaseBatch.release_no.desc())
    ).first()
    return (row[0], row[1]) if row else None


# ---------------------------------------------------------------- statistics
MIN_SAMPLE = 5


def public_stats(db: Session, a: Assignment) -> dict | None:
    """學生可見平均：發布後 + 開關 + 排除 0 與 NULL + 小樣本隱藏（SEC-021）。"""
    if not a.show_student_average or a.student_grade_visibility == "HIDDEN":
        return None
    batch = latest_batch(db, a.id)
    if batch is None:
        return None
    rows = list(db.scalars(select(ReleasedGrade).where(ReleasedGrade.batch_id == batch.id)))
    seen: set[int] = set()
    per_q: dict[int, list[Decimal]] = {}
    titles: dict[int, str] = {}
    finals: list[Decimal] = []
    for r in rows:
        if r.revision_id in seen:  # 團體作業每組只算一次
            continue
        seen.add(r.revision_id)
        snap = json.loads(r.snapshot_json)
        for q in snap["questions"]:
            titles[q["ordinal"]] = q["title"] or f"第 {q['ordinal']} 題"
            if q["score"] is not None and Decimal(q["score"]) != 0:
                per_q.setdefault(q["ordinal"], []).append(Decimal(q["score"]))
            per_q.setdefault(q["ordinal"], per_q.get(q["ordinal"], []))
        if snap["final"] is not None and Decimal(snap["final"]) != 0:
            finals.append(Decimal(snap["final"]))

    def avg(vals):
        if len(vals) < MIN_SAMPLE:
            return None, len(vals)
        return fmt_score(sum(vals, Decimal("0")) / len(vals)), len(vals)

    out = {"questions": [], "release_no": batch.release_no}
    for ordn in sorted(per_q):
        v, n = avg(per_q[ordn])
        out["questions"].append({"title": titles[ordn], "avg": v, "n": n})
    v, n = avg(finals)
    out["final"] = {"avg": v, "n": n}
    return out


def submission_rate(db: Session, a: Assignment) -> tuple[int, int]:
    """(已交單位數, 應繳單位數)；已交 = 至少一個通過檢查的版本。"""
    if a.submission_mode == "GROUP":
        total = db.scalar(select(func.count()).select_from(Group)) or 0
    else:
        total = db.scalar(
            select(func.count()).select_from(User).join(UserRole, UserRole.user_id == User.id)
            .where(UserRole.role == "STUDENT", User.status != "DISABLED")
        ) or 0
    done = db.scalar(
        select(func.count(func.distinct(Submission.id))).select_from(Submission)
        .join(Revision, Revision.submission_id == Submission.id)
        .where(Submission.assignment_id == a.id, Revision.scan_state == "ACCEPTED")
    ) or 0
    return done, total
