"""資料表（對應規格 §7）。時間一律以 naive UTC 儲存。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .utils import utcnow

SCORE = Numeric(8, 2, asdecimal=True)

ROLES = ("STUDENT", "TA", "INSTRUCTOR", "ADMIN")
ROLE_LABELS = {"STUDENT": "學員", "TA": "助教", "INSTRUCTOR": "講師", "ADMIN": "管理員"}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254))
    canonical_email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    school: Mapped[str] = mapped_column(String(120), default="")
    grade: Mapped[str] = mapped_column(String(20), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    discord_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="PENDING_PASSWORD_CHANGE")
    password_hash: Mapped[str] = mapped_column(String(255))
    credential_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 個資告知同意（所有角色首次登入必須同意）
    consented_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    consent_ip: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 公告未讀計算：最後一次打開公告頁的時間
    announcements_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    roles: Mapped[list["UserRole"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status in ('PENDING_PASSWORD_CHANGE','ACTIVE','DISABLED')", name="ck_user_status"),
    )

    @property
    def role_set(self) -> set[str]:
        return {r.role for r in self.roles}


class UserRole(Base):
    __tablename__ = "user_roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))
    user: Mapped[User] = relationship(back_populates="roles")
    __table_args__ = (
        UniqueConstraint("user_id", "role"),
        CheckConstraint("role in ('STUDENT','TA','INSTRUCTOR','ADMIN')", name="ck_role"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))  # RESTRICTED | MFA_PENDING | FULL
    csrf_token: Mapped[str] = mapped_column(String(64))
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_failures: Mapped[int] = mapped_column(Integer, default=0)
    credential_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")


class MfaFactor(Base):
    __tablename__ = "mfa_factors"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    type: Mapped[str] = mapped_column(String(16), default="TOTP")
    encrypted_secret: Mapped[str] = mapped_column(Text)
    enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_step: Mapped[int] = mapped_column(Integer, default=0)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT | PUBLISHED
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 英文名稱（下載檔名用；只允許英數字、底線、連字號）
    name_en: Mapped[str] = mapped_column(String(40), default="", server_default="")

    @property
    def file_name(self) -> str:
        return self.name_en or f"Course{self.id}"


class CourseInstructor(Base):
    __tablename__ = "course_instructors"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    instructor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    __table_args__ = (UniqueConstraint("course_id", "instructor_id"),)


class CourseMaterial(Base):
    __tablename__ = "course_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    storage_key: Mapped[str] = mapped_column(String(80))
    original_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    visibility: Mapped[str] = mapped_column(String(16), default="PUBLISHED")  # PUBLISHED | HIDDEN
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)
    ordinal: Mapped[int] = mapped_column(Integer, unique=True)

    @property
    def label(self) -> str:
        return f"第 {self.ordinal} 組"


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GroupStaff(Base):
    __tablename__ = "group_staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    ta_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT | PUBLISHED
    submission_mode: Mapped[str] = mapped_column(String(16), default="INDIVIDUAL")
    due_at: Mapped[datetime] = mapped_column(DateTime)
    due_version: Mapped[int] = mapped_column(Integer, default=1)
    accepting_submissions: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_late: Mapped[bool] = mapped_column(Boolean, default=True)
    allowed_types: Mapped[str] = mapped_column(String(100), default="pdf")
    allow_multiple_files: Mapped[bool] = mapped_column(Boolean, default=False)
    max_files: Mapped[int] = mapped_column(Integer, default=1)
    max_total_bytes: Mapped[int] = mapped_column(Integer, default=20_000_000)
    grading_mode: Mapped[str] = mapped_column(String(16), default="BY_QUESTION")
    show_student_average: Mapped[bool] = mapped_column(Boolean, default=False)
    show_submission_rate: Mapped[bool] = mapped_column(Boolean, default=True)
    student_grade_visibility: Mapped[str] = mapped_column(String(32), default="VISIBLE_AFTER_RELEASE")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 最終收件期限：超過後一律拒收（即使允許遲交）。NULL（舊資料）視為截止時間 + 7 天
    final_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    name_en: Mapped[str] = mapped_column(String(40), default="", server_default="")
    # 截止前 Discord 提醒：NULL = 用系統預設；"off" = 不提醒；"2,1" = 自訂（小時）
    reminder_hours: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    # 提醒時要 @ 的 Discord 身分組（DiscordRole.id）；NULL = 不提及
    discord_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("submission_mode in ('INDIVIDUAL','GROUP')", name="ck_sub_mode"),
        CheckConstraint("grading_mode in ('BY_QUESTION','DIRECT_TOTAL')", name="ck_grading_mode"),
        CheckConstraint("max_total_bytes > 0 AND max_total_bytes <= 40000000", name="ck_total_bytes"),
        CheckConstraint("max_files >= 1 AND max_files <= 10", name="ck_max_files"),
        CheckConstraint(
            "student_grade_visibility in ('VISIBLE_AFTER_RELEASE','HIDDEN')", name="ck_visibility"
        ),
    )

    @property
    def allowed_type_list(self) -> list[str]:
        return [t for t in self.allowed_types.split(",") if t]

    @property
    def final_deadline_effective(self) -> datetime:
        """實際的最後收件時間：不接受遲交 → 截止時間；否則最終收件期限。"""
        from datetime import timedelta

        if not self.allow_late:
            return self.due_at
        return self.final_deadline or (self.due_at + timedelta(days=7))

    @property
    def file_name(self) -> str:
        return self.name_en or f"Assignment{self.id}"


class Question(Base):
    __tablename__ = "assignment_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200), default="")
    max_points: Mapped[Decimal] = mapped_column(SCORE)
    __table_args__ = (
        UniqueConstraint("assignment_id", "ordinal"),
        CheckConstraint("max_points > 0", name="ck_q_max"),
    )


class RubricOption(Base):
    __tablename__ = "rubric_options"
    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("assignment_questions.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    score: Mapped[Decimal] = mapped_column(SCORE)
    feedback_template: Mapped[str] = mapped_column(Text, default="")


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    owner_type: Mapped[str] = mapped_column(String(16))  # INDIVIDUAL | GROUP
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"), nullable=True)
    active_revision_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint("assignment_id", "student_id"),
        UniqueConstraint("assignment_id", "group_id"),
        CheckConstraint(
            "(owner_type='INDIVIDUAL' AND student_id IS NOT NULL AND group_id IS NULL) OR "
            "(owner_type='GROUP' AND group_id IS NOT NULL AND student_id IS NULL)",
            name="ck_sub_owner",
        ),
    )


class Revision(Base):
    __tablename__ = "submission_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    finalized_at: Mapped[datetime] = mapped_column(DateTime)
    due_version: Mapped[int] = mapped_column(Integer)
    due_at_snapshot: Mapped[datetime] = mapped_column(DateTime)
    is_late: Mapped[bool] = mapped_column(Boolean)
    late_reclassified_reason: Mapped[str] = mapped_column(Text, default="")
    # 遲交核准註記：LATE 標記不變，只是記錄「已核准」與原因
    late_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    late_note_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    late_note_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scan_state: Mapped[str] = mapped_column(String(16), default="PENDING_SCAN")
    manifest_hash: Mapped[str] = mapped_column(String(64))
    total_bytes: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (
        UniqueConstraint("submission_id", "revision_no"),
        UniqueConstraint("submission_id", "idempotency_key"),
        CheckConstraint("scan_state in ('PENDING_SCAN','ACCEPTED','REJECTED')", name="ck_scan_state"),
        CheckConstraint("total_bytes <= 40000000", name="ck_rev_total"),
    )


class Artifact(Base):
    __tablename__ = "submission_artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), index=True)
    private_key: Mapped[str] = mapped_column(String(80), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    declared_type: Mapped[str] = mapped_column(String(10))
    validated_type: Mapped[str] = mapped_column(String(10), default="")
    inspection_state: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|ACCEPTED|REJECTED
    inspection_detail: Mapped[str] = mapped_column(String(255), default="")
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GroupSnapshot(Base):
    __tablename__ = "submission_group_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class GradingTask(Base):
    __tablename__ = "grading_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    grader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    target_group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    can_grade_total: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GradingTaskQuestion(Base):
    __tablename__ = "grading_task_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("grading_tasks.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("assignment_questions.id"))
    __table_args__ = (UniqueConstraint("task_id", "question_id"),)


class GradeItem(Base):
    __tablename__ = "grade_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), index=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("assignment_questions.id"))
    score: Mapped[Optional[Decimal]] = mapped_column(SCORE, nullable=True)  # NULL = 尚未評分
    feedback_to_student: Mapped[str] = mapped_column(Text, default="")
    grader_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("revision_id", "question_id"),)


class GradeHistory(Base):
    __tablename__ = "grade_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), index=True)
    field: Mapped[str] = mapped_column(String(64))  # question:<id> | direct_total | manual_final | member:<id>
    old_value: Mapped[str] = mapped_column(String(32), default="")
    new_value: Mapped[str] = mapped_column(String(32), default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GradeTotal(Base):
    __tablename__ = "grade_totals"
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), primary_key=True)
    direct_total: Mapped[Optional[Decimal]] = mapped_column(SCORE, nullable=True)
    manual_final_score: Mapped[Optional[Decimal]] = mapped_column(SCORE, nullable=True)
    override_reason: Mapped[str] = mapped_column(Text, default="")
    summary_feedback: Mapped[str] = mapped_column(Text, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GroupMemberOverride(Base):
    __tablename__ = "group_member_grade_overrides"
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    manual_final_score: Mapped[Optional[Decimal]] = mapped_column(SCORE, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("revision_id", "student_id"),)


class InternalNote(Base):
    __tablename__ = "staff_internal_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    revision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("submission_revisions.id"), nullable=True)
    question_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assignment_questions.id"), nullable=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReleaseBatch(Base):
    __tablename__ = "grade_release_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    release_no: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("assignment_id", "release_no"),)


class ReleasedGrade(Base):
    __tablename__ = "released_grades"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("grade_release_batches.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[Optional[Decimal]] = mapped_column(SCORE, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(32))


class Notification(Base):
    __tablename__ = "submission_notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("submission_revisions.id"))
    message: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Announcement(Base):
    """公告：course_id 為 NULL = 全營公告。"""
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[Optional[int]] = mapped_column(ForeignKey("courses.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="PUBLISHED")  # PUBLISHED | HIDDEN
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 已推送到 Discord
    discord_role_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 推送時要 @ 的身分組
    __table_args__ = (CheckConstraint("status in ('PUBLISHED','HIDDEN')", name="ck_ann_status"),)


class DiscordRole(Base):
    """可以在 Discord 訊息中 @ 的身分組（由管理員／講師在介面登記）。"""
    __tablename__ = "discord_roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(60))          # 顯示名稱，例如「學員」
    role_id: Mapped[str] = mapped_column(String(25), unique=True)  # Discord 身分組 ID（17–20 位數字）
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReminderLog(Base):
    """截止提醒發送紀錄（避免重複發送；截止時間修改後 due_version 改變會重新計算）。"""
    __tablename__ = "reminder_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    due_version: Mapped[int] = mapped_column(Integer)
    hours: Mapped[str] = mapped_column(String(10))
    sent: Mapped[bool] = mapped_column(Boolean, default=True)  # False = 伺服器停機錯過、補發時略過
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("assignment_id", "due_version", "hours"),)


class GradeAppeal(Base):
    """成績申覆：一律由講師（或管理員）處理。question_id 為 NULL = 針對總分。"""
    __tablename__ = "grade_appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    question_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assignment_questions.id"), nullable=True)
    revision_no: Mapped[int] = mapped_column(Integer)  # 申覆當下已發布的版本
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN | KEPT | CHANGED
    response: Mapped[str] = mapped_column(Text, default="")
    handler_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    __table_args__ = (CheckConstraint("status in ('OPEN','KEPT','CHANGED')", name="ck_appeal_status"),)


class SimilarityRun(Base):
    """PDF 相似度比對（只給工作人員看；系統只列出可疑組合，不判定抄襲）。"""
    __tablename__ = "similarity_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING | RUNNING | DONE | FAILED
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    template_name: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")


Index("ix_membership_active", GroupMembership.student_id, GroupMembership.ended_at)


# ---- Append-only / immutable 保護（應用層）----
def _immutable(mapper, connection, target):
    raise RuntimeError(f"{type(target).__name__} is immutable")


for _cls in (ReleaseBatch, ReleasedGrade, AuditEvent, GradeHistory):
    event.listen(_cls, "before_update", _immutable)
    event.listen(_cls, "before_delete", _immutable)


class SystemSetting(Base):
    """網頁上可修改的系統設定（key/value）。機密值（Discord webhook）以 AAMS_SECRET_KEY 加密後存放。"""
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
