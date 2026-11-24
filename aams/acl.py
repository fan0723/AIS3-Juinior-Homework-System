"""Object-level authorization (規格 §3.3, SEC-001~003).

所有判斷都由資料庫關係推導，絕不信任前端送來的 student_id / group_id / course_id。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import Ctx
from .models import (
    Assignment,
    Course,
    CourseInstructor,
    GradingTask,
    GradingTaskQuestion,
    Group,
    GroupMembership,
    GroupStaff,
    Submission,
)


def deny(msg: str = "沒有權限存取此資源") -> HTTPException:
    return HTTPException(403, msg)


def instructs(db: Session, user_id: int, course_id: int) -> bool:
    return db.scalar(
        select(CourseInstructor.id).where(
            CourseInstructor.course_id == course_id, CourseInstructor.instructor_id == user_id
        )
    ) is not None


def can_manage_course(ctx: Ctx, course_id: int) -> bool:
    return ctx.is_admin or (ctx.is_instructor and instructs(ctx.db, ctx.user.id, course_id))


def require_manage_course(ctx: Ctx, course_id: int) -> None:
    if not can_manage_course(ctx, course_id):
        raise deny("只能管理被指派的課程")


def managed_course_ids(ctx: Ctx) -> set[int] | None:
    """None 代表全部（Admin）。"""
    if ctx.is_admin:
        return None
    if not ctx.is_instructor:
        return set()
    return set(ctx.db.scalars(select(CourseInstructor.course_id).where(CourseInstructor.instructor_id == ctx.user.id)))


def active_group_of(db: Session, student_id: int) -> Group | None:
    return db.scalar(
        select(Group)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(GroupMembership.student_id == student_id, GroupMembership.ended_at.is_(None))
    )


def ta_group_ids(db: Session, ta_id: int) -> set[int]:
    return set(db.scalars(select(GroupStaff.group_id).where(GroupStaff.ta_id == ta_id, GroupStaff.ended_at.is_(None))))


def group_member_ids(db: Session, group_id: int) -> list[int]:
    return list(
        db.scalars(
            select(GroupMembership.student_id).where(
                GroupMembership.group_id == group_id, GroupMembership.ended_at.is_(None)
            )
        )
    )


def group_ta_ids(db: Session, group_id: int) -> list[int]:
    return list(db.scalars(select(GroupStaff.ta_id).where(GroupStaff.group_id == group_id, GroupStaff.ended_at.is_(None))))


def active_tasks(db: Session, grader_id: int, assignment_id: int, group_id: int | None = None) -> list[GradingTask]:
    q = select(GradingTask).where(
        GradingTask.grader_id == grader_id,
        GradingTask.assignment_id == assignment_id,
        GradingTask.revoked_at.is_(None),
    )
    if group_id is not None:
        q = q.where(GradingTask.target_group_id == group_id)
    return list(db.scalars(q))


def submission_group_id(db: Session, sub: Submission) -> int | None:
    """團體作業 → 該組；個人作業 → 學生目前所屬組別。"""
    if sub.owner_type == "GROUP":
        return sub.group_id
    g = active_group_of(db, sub.student_id) if sub.student_id else None
    return g.id if g else None


def course_visible_to_students(course: Course) -> bool:
    return course.status == "PUBLISHED"


def assignment_visible(ctx: Ctx, a: Assignment) -> bool:
    if can_manage_course(ctx, a.course_id):
        return True
    course = ctx.db.get(Course, a.course_id)
    if course is None or course.status != "PUBLISHED" or a.status != "PUBLISHED":
        return False
    if ctx.is_student or ctx.is_ta:
        return True
    return False


def is_owner(ctx: Ctx, sub: Submission) -> bool:
    if not ctx.is_student:
        return False
    if sub.owner_type == "INDIVIDUAL":
        return sub.student_id == ctx.user.id
    g = active_group_of(ctx.db, ctx.user.id)
    return g is not None and g.id == sub.group_id


def can_view_submission(ctx: Ctx, sub: Submission, a: Assignment) -> bool:
    if is_owner(ctx, sub):
        return True
    if can_manage_course(ctx, a.course_id):
        return True
    if ctx.is_ta:
        gid = submission_group_id(ctx.db, sub)
        if gid is None:
            return False
        if gid in ta_group_ids(ctx.db, ctx.user.id):
            return True
        if active_tasks(ctx.db, ctx.user.id, a.id, gid):
            return True
    return False


def can_view_grading(ctx: Ctx, sub: Submission, a: Assignment) -> bool:
    """可看未發布分數、評語、內部備註：講師/Admin 或該作業×該組的有效任務。"""
    if can_manage_course(ctx, a.course_id):
        return True
    if ctx.is_ta:
        gid = submission_group_id(ctx.db, sub)
        return gid is not None and bool(active_tasks(ctx.db, ctx.user.id, a.id, gid))
    return False


def gradable_question_ids(ctx: Ctx, sub: Submission, a: Assignment) -> set[int] | str:
    """'ALL' 或被授權題目 id 集合。"""
    if can_manage_course(ctx, a.course_id):
        return "ALL"
    if not ctx.is_ta:
        return set()
    gid = submission_group_id(ctx.db, sub)
    if gid is None:
        return set()
    tasks = active_tasks(ctx.db, ctx.user.id, a.id, gid)
    if not tasks:
        return set()
    return set(
        ctx.db.scalars(
            select(GradingTaskQuestion.question_id).where(GradingTaskQuestion.task_id.in_([t.id for t in tasks]))
        )
    )


def can_grade_total(ctx: Ctx, sub: Submission, a: Assignment) -> bool:
    if can_manage_course(ctx, a.course_id):
        return True
    if not ctx.is_ta:
        return False
    gid = submission_group_id(ctx.db, sub)
    return gid is not None and any(t.can_grade_total for t in active_tasks(ctx.db, ctx.user.id, a.id, gid))


def visible_group_ids_for_assignment(ctx: Ctx, a: Assignment) -> set[int] | None:
    """Staff 在某作業可看到的組別；None = 全部。"""
    if can_manage_course(ctx, a.course_id):
        return None
    ids: set[int] = set()
    if ctx.is_ta:
        ids |= ta_group_ids(ctx.db, ctx.user.id)
        ids |= {t.target_group_id for t in active_tasks(ctx.db, ctx.user.id, a.id)}
    return ids
