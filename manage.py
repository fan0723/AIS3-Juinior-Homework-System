"""AAMS 管理指令。

    python manage.py init [--lan]          產生 .env（隨機 SECRET）
    python manage.py initdb                建立資料表
    python manage.py create-admin          建立第一位管理員（互動輸入）
    python manage.py seed-demo [--active]  建立示範資料（102 帳號、12 組、課程、作業）
    python manage.py seed-demo --active --renew
                                           示範資料已存在時：補齊缺少的並重設示範帳號密碼
    python manage.py totp-code EMAIL       （僅測試）印出該帳號目前的 TOTP 驗證碼
    python manage.py backup                備份資料庫與檔案到 data/backups/
    python manage.py reset-instance        期末重置（需兩位管理員確認）
    python manage.py reset-demo [--yes] [--force]
                                           測試環境完整重置：清空資料庫與上傳檔案，保留 .env，重建示範資料
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"


def cmd_init(args) -> None:
    if ENV.exists() and not args.force:
        print(f".env 已存在：{ENV}（如要覆寫請加 --force；覆寫 SECRET 會使所有 Session 與 MFA 失效）")
        return
    replaced = ENV.exists()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    ENV.write_text(
        "\n".join([
            "# AAMS 設定（請勿提交到版控）",
            f"AAMS_SECRET_KEY={secrets.token_urlsafe(48)}",
            f"AAMS_HOST={host}",
            "AAMS_PORT=8000",
            "AAMS_PREVIEW_PORT=8001",
            "# 留空 = 依使用者連線的主機名稱自動推導 http(s)://<host>:8001",
            "AAMS_PREVIEW_URL=",
            "# 正式環境（HTTPS / Caddy）必須設為 true",
            "AAMS_COOKIE_SECURE=false",
            "# AAMS_DATABASE_URL=postgresql+psycopg://aams:password@127.0.0.1:5432/aams",
            "AAMS_EVENT_NAME=AIS3 Junior 2026",
            "# 營隊結束日（台北時間，例 2026-07-10）。作業「最終收件期限」預設 = 此日 + 7 天",
            "AAMS_EVENT_END_AT=",
            "AAMS_SYSTEM_ALLOWED_TYPES=pdf,png,jpg,txt",
            "# Discord Webhook 與截止提醒預設：建議留空，由管理員在網頁「管理後台 › 系統」設定（加密存放）。",
            "# 這裡填了就以 .env 為準，網頁上會鎖住不能改。",
            "AAMS_DISCORD_STAFF_WEBHOOK=",
            "AAMS_DISCORD_STUDENT_WEBHOOK=",
            "AAMS_REMINDER_HOURS=",
            "",
        ]),
        encoding="utf-8",
    )
    print(f"已建立 {ENV}（AAMS_HOST={host}）")
    if replaced:
        print("注意：SECRET 已更換，所有登入 Session 與已綁定的 TOTP 都已失效（無法復原）。"
              "測試環境請接著執行：python manage.py reset-demo")


def _db():
    from aams.db import SessionLocal, init_db

    init_db()
    return SessionLocal()


def cmd_initdb(args) -> None:
    _db().close()
    print("資料表已建立／確認完成。")


def cmd_create_admin(args) -> None:
    from sqlalchemy import select

    from aams.models import User, UserRole
    from aams.security import hash_password, password_problems
    from aams.utils import canonical_email, valid_email

    email = args.email or input("管理員 Email：").strip()
    if not valid_email(email):
        sys.exit("Email 格式錯誤")
    name = args.name or input("姓名：").strip() or "Admin"
    pw = getpass.getpass("密碼（至少 10 字元，不會顯示）：")
    if pw != getpass.getpass("再次輸入："):
        sys.exit("兩次密碼不一致")
    probs = password_problems(pw, email)
    if probs:
        sys.exit("；".join(probs))
    with _db() as db:
        if db.scalar(select(User).where(User.canonical_email == canonical_email(email))):
            sys.exit("此 Email 已存在")
        u = User(email=email, canonical_email=canonical_email(email), name=name, status="ACTIVE",
                 password_hash=hash_password(pw), activated_at=__import__("aams.utils", fromlist=["utcnow"]).utcnow())
        db.add(u)
        db.flush()
        db.add(UserRole(user_id=u.id, role="ADMIN"))
        db.commit()
    print(f"已建立管理員 {email}。首次登入後系統會要求綁定 TOTP 兩步驟驗證。")


DEMO_INITIAL = "10007038907"
DEMO_ACTIVE = "Ais3-Demo-2026"


def cmd_seed_demo(args) -> None:
    from decimal import Decimal

    from sqlalchemy import func, select

    from aams.models import (
        Assignment,
        Course,
        CourseInstructor,
        Group,
        GroupMembership,
        GroupStaff,
        Question,
        RubricOption,
        User,
        UserRole,
    )
    from aams.security import hash_password
    from aams.utils import canonical_email, utcnow

    renew = getattr(args, "renew", False)
    stats = {"users_new": 0, "users_reset": 0, "courses_new": 0, "assignments_new": 0, "memberships_fixed": 0}

    with _db() as db:
        exists = db.scalar(select(func.count()).select_from(User).where(User.email.like("%@demo.ais3.local")))
        if exists and not renew:
            sys.exit("示範資料已存在（@demo.ais3.local）。\n"
                     "  ‧ 想補齊缺少的示範帳號／課程並重設示範帳號密碼：python manage.py seed-demo --active --renew\n"
                     "  ‧ 想整個重來：先關閉 run.py，刪除 data 資料夾後再執行 seed-demo")
        status = "ACTIVE" if args.active else "PENDING_PASSWORD_CHANGE"
        pw_hash = hash_password(DEMO_ACTIVE if args.active else DEMO_INITIAL)
        now = utcnow()

        groups = []
        for n in range(1, 13):
            g = db.scalar(select(Group).where(Group.ordinal == n))
            if g is None:
                g = Group(code=f"G{n:02d}", ordinal=n)
                db.add(g)
                db.flush()
            groups.append(g)

        def mk(email, name, role, **kw):
            """建立示範帳號；--renew 時若已存在則重設密碼、狀態與角色（只動 @demo.ais3.local 帳號）。"""
            u = db.scalar(select(User).where(User.canonical_email == canonical_email(email)))
            if u is None:
                u = User(email=email, canonical_email=canonical_email(email), name=name, status=status,
                         password_hash=pw_hash, activated_at=now if args.active else None, **kw)
                db.add(u)
                db.flush()
                db.add(UserRole(user_id=u.id, role=role))
                stats["users_new"] += 1
                return u
            u.password_hash = pw_hash
            u.status = status
            u.activated_at = now if args.active else None
            u.credential_version = (u.credential_version or 1) + 1  # 讓舊的登入狀態全部失效
            for r in list(u.roles):
                if r.role != role:
                    u.roles.remove(r)
            if not any(r.role == role for r in u.roles):
                u.roles.append(UserRole(role=role))
            db.flush()
            stats["users_reset"] += 1
            return u

        def ensure_member(g, s):
            active = list(db.scalars(select(GroupMembership).where(
                GroupMembership.student_id == s.id, GroupMembership.ended_at.is_(None))))
            if any(m.group_id == g.id for m in active):
                return
            for m in active:  # 被移到別組的示範學員 → 回到原本設計的組別
                m.ended_at = now
            db.flush()
            db.add(GroupMembership(group_id=g.id, student_id=s.id))
            stats["memberships_fixed"] += 0 if not active else 1

        def ensure_ta(g, t):
            if db.scalar(select(GroupStaff).where(GroupStaff.group_id == g.id, GroupStaff.ta_id == t.id,
                                                  GroupStaff.ended_at.is_(None))) is None:
                db.add(GroupStaff(group_id=g.id, ta_id=t.id))

        for gi, g in enumerate(groups, start=1):
            for k in range(1, 7):
                s = mk(f"s{gi:02d}{k}@demo.ais3.local", f"學員{gi:02d}-{k}", "STUDENT", school="示範高中",
                       grade=str(10 + k % 3), phone=f"09{gi:02d}00{k:04d}")
                ensure_member(g, s)
            for k in (1, 2):
                t = mk(f"ta{gi:02d}{k}@demo.ais3.local", f"助教{gi:02d}-{k}", "TA", school="示範大學")
                ensure_ta(g, t)
        instructors = [mk(f"instructor{i}@demo.ais3.local", f"講師{i}", "INSTRUCTOR", school="示範大學") for i in (1, 2, 3)]
        for i in (1, 2, 3):
            mk(f"staff{i}@demo.ais3.local", f"工作人員{i}", "ADMIN", school="主辦單位")

        def ensure_course(title, description, ordinal, instructor, name_en=""):
            c = db.scalar(select(Course).where(Course.title == title))
            if c is None:
                c = Course(title=title, description=description, status="PUBLISHED", ordinal=ordinal, name_en=name_en)
                db.add(c)
                db.flush()
                stats["courses_new"] += 1
            if db.scalar(select(CourseInstructor).where(CourseInstructor.course_id == c.id,
                                                        CourseInstructor.instructor_id == instructor.id)) is None:
                db.add(CourseInstructor(course_id=c.id, instructor_id=instructor.id))
            return c

        c1 = ensure_course("Web Security 入門", "HTTP 基礎、XSS、SQL Injection。\n投影片請見下方教材區。", 1, instructors[0],
                           "WebSecurity")
        c2 = ensure_course("Reverse Engineering", "組語基礎與靜態分析。", 2, instructors[1], "Reverse")
        c3 = ensure_course("最終專題", "分組專題報告。", 3, instructors[2], "FinalProject")

        def ensure_assignment(course, title, items, **kw):
            """已存在的作業不覆蓋（可能已有提交與分數），只補建缺少的。"""
            if db.scalar(select(Assignment).where(Assignment.course_id == course.id, Assignment.title == title)):
                return
            from aams.services import default_final_deadline

            kw.setdefault("final_deadline", default_final_deadline(kw["due_at"]))
            a = Assignment(course_id=course.id, title=title, status="PUBLISHED", **kw)
            db.add(a)
            db.flush()
            for i, (t, m) in enumerate(items, start=1):
                q = Question(assignment_id=a.id, ordinal=i, title=t, max_points=Decimal(m))
                db.add(q)
                db.flush()
                for label, frac, fb in (("完全正確", 1, "很好！"), ("部分正確", Decimal("0.6"), "方向正確，細節待補。"),
                                        ("嘗試", Decimal("0.2"), "請再研究看看。")):
                    db.add(RubricOption(question_id=q.id, label=label, score=Decimal(m) * frac, feedback_template=fb))
            stats["assignments_new"] += 1

        ensure_assignment(c1, "Lab 1：XSS 解題報告", [("Reflected XSS", 30), ("Stored XSS", 30), ("CSP 分析", 40)],
                          description="請上傳 PDF 解題報告（含截圖）。", submission_mode="INDIVIDUAL",
                          due_at=now + timedelta(days=2), allowed_types="pdf", allow_multiple_files=False, max_files=1,
                          max_total_bytes=20_000_000, grading_mode="BY_QUESTION", show_student_average=True,
                          created_by=instructors[0].id, name_en="Lab1XSS")
        ensure_assignment(c2, "Crackme 小組作業", [("找出密碼", 50), ("分析報告", 50)],
                          description="每組提交一份報告，可附截圖。", submission_mode="GROUP",
                          due_at=now + timedelta(days=3), allowed_types="jpg,pdf,png", allow_multiple_files=True,
                          max_files=5, max_total_bytes=40_000_000, grading_mode="BY_QUESTION",
                          created_by=instructors[1].id, name_en="Crackme")
        ensure_assignment(c3, "最終專題報告", [("專題總分", 100)],
                          description="分組專題（成績不對學員公開）。", submission_mode="GROUP",
                          due_at=now + timedelta(days=4), allowed_types="pdf", allow_multiple_files=True, max_files=3,
                          max_total_bytes=40_000_000, grading_mode="DIRECT_TOTAL", student_grade_visibility="HIDDEN",
                          created_by=instructors[2].id, name_en="FinalReport")
        db.commit()
    pw = DEMO_ACTIVE if args.active else DEMO_INITIAL
    if renew and exists:
        print("示範資料已更新（--renew）：")
        print(f"  新增帳號 {stats['users_new']} 個；重設既有示範帳號 {stats['users_reset']} 個（密碼、狀態、角色，舊登入全部登出）")
        print(f"  新增課程 {stats['courses_new']} 門；新增作業 {stats['assignments_new']} 份（既有作業、提交、分數不變）")
        print(f"  修正分組 {stats['memberships_fixed']} 人（被移到別組的示範學員回到原組）")
        print("  未變動：非 @demo.ais3.local 的帳號、已存在的作業／提交／分數、管理員的兩步驟驗證綁定")
    else:
        print("示範資料已建立：72 學員（12 組 × 6）、24 助教、3 講師、3 工作人員、3 門課程與作業。")
    print(f"所有示範帳號密碼：{pw}（狀態 {status}）")
    print("  學員  s{組2碼}{1-6}@demo.ais3.local   例：s011@demo.ais3.local（第 1 組）")
    print("  助教  ta{組2碼}{1-2}@demo.ais3.local  例：ta011@demo.ais3.local（帶第 1 組）")
    print("  講師  instructor1..3@demo.ais3.local （各負責一門課）")
    print("  管理  staff1..3@demo.ais3.local      （首次登入需綁定 TOTP；可用 manage.py totp-code 取得驗證碼）")
    print("【注意】這些是公開的測試帳號，正式上線前務必刪除（正式環境請用全新資料庫）。")


def cmd_totp_code(args) -> None:
    import pyotp
    from sqlalchemy import select

    from aams.models import MfaFactor, User
    from aams.security import decrypt_secret
    from aams.utils import canonical_email

    print("【注意】僅供內網測試使用：任何能執行此指令的人都已擁有伺服器權限。")
    with _db() as db:
        u = db.scalar(select(User).where(User.canonical_email == canonical_email(args.email)))
        f = db.scalar(select(MfaFactor).where(MfaFactor.user_id == u.id)) if u else None
        if f is None:
            sys.exit("此帳號尚未開始設定 TOTP（請先登入並打開 /mfa/setup 頁面）")
        secret = decrypt_secret(f.encrypted_secret)
        if secret is None:
            sys.exit("無法解開此帳號的 TOTP 金鑰：.env 的 AAMS_SECRET_KEY 已更換（例如執行過 init --force）。\n"
                     "  測試環境：python manage.py reset-demo\n"
                     "  正式環境：請另一位管理員在「管理後台 › 使用者」重設此帳號的兩步驟驗證")
        print(pyotp.TOTP(secret).now())


def cmd_backup(args) -> None:
    from aams.config import settings

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = settings.data_dir / "backups" / f"aams-{stamp}.tar.gz"
    tmpdb = settings.data_dir / "tmp" / f"db-{stamp}.sqlite"
    with tarfile.open(out, "w:gz") as tar:
        if settings.is_sqlite:
            src = sqlite3.connect(str(settings.data_dir / "aams.db"))
            dst = sqlite3.connect(str(tmpdb))
            src.backup(dst)  # 線上一致性備份
            dst.close()
            src.close()
            tar.add(tmpdb, arcname="aams.db")
            tmpdb.unlink()
        else:
            print("PostgreSQL：請另外執行 pg_dump（見部署手冊），此處只備份檔案。")
        for sub in ("accepted", "materials"):
            tar.add(settings.data_dir / sub, arcname=sub)
    print(f"備份完成：{out}")
    print("請將備份複製到另一台機器或離線媒體（同一顆硬碟上的備份無法防硬碟損壞）。")


def cmd_reset_instance(args) -> None:
    from sqlalchemy import select

    from aams.config import settings
    from aams.db import Base, engine
    from aams.models import User
    from aams.security import verify_password
    from aams.utils import canonical_email

    print("=== 期末重置：將刪除本 instance 的所有資料庫資料與上傳檔案 ===")
    print("請先確認：成績 CSV、繳交紀錄 CSV、必要的 PDF ZIP 都已匯出並核對 SHA-256 manifest，且已備份。")
    confirmed: set[int] = set()
    with _db() as db:
        for i in (1, 2):
            email = input(f"第 {i} 位管理員 Email：").strip()
            pw = getpass.getpass("密碼：")
            u = db.scalar(select(User).where(User.canonical_email == canonical_email(email)))
            if u is None or "ADMIN" not in u.role_set or not verify_password(u.password_hash, pw):
                sys.exit("驗證失敗，中止。")
            confirmed.add(u.id)
    if len(confirmed) < 2:
        sys.exit("需要兩位不同的管理員確認，中止。")
    phrase = f"RESET {settings.event_name}"
    if input(f"請輸入「{phrase}」確認：").strip() != phrase:
        sys.exit("確認字串不符，中止。")
    _wipe_instance()
    print("已重置資料庫與上傳檔案。注意：data/backups/ 與外部備份不會自動刪除，請依保存政策另行銷毀。")


def _wipe_instance() -> None:
    """刪除所有資料表與上傳檔案（data/backups/ 與 .env 不動）。"""
    from aams import models  # noqa: F401  確保所有資料表都已註冊
    from aams.config import settings
    from aams.db import Base, engine

    Base.metadata.drop_all(engine)
    for sub in ("quarantine", "accepted", "materials", "tmp", "spool"):
        shutil.rmtree(settings.data_dir / sub, ignore_errors=True)
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


DEMO_DOMAIN = "%@demo.ais3.local"


def cmd_reset_demo(args) -> None:
    from sqlalchemy import select

    from aams.models import User

    with _db() as db:
        others = list(db.scalars(select(User.email).where(User.canonical_email.not_like(DEMO_DOMAIN))))
    if others and not args.force:
        sys.exit(f"資料庫裡有 {len(others)} 個非示範帳號（例如 {others[0]}），看起來不是測試環境，中止。\n"
                 "  確定要全部刪除（例如紅隊內測匯入的帳號）請加 --force。")
    print("=== 測試環境完整重置：刪除所有資料表與上傳檔案，保留 .env（SECRET 不變），再重建示範資料 ===")
    print("請先關閉 run.py（Ctrl+C），否則檔案與資料庫可能被鎖住。")
    if not args.yes and input("請輸入「RESET DEMO」確認：").strip() != "RESET DEMO":
        sys.exit("確認字串不符，中止。")
    _wipe_instance()
    cmd_seed_demo(argparse.Namespace(active=True, renew=False))
    print("管理員需要重新綁定 TOTP：手機上舊的「AIS3 Junior 作業繳交系統」項目請刪除後再掃新 QR，")
    print("  或直接用：python manage.py totp-code staff1@demo.ais3.local")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # 繁中 Windows 主控台（cp950）遇到無法編碼的字元時不要整個崩潰
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="AIS3 Junior 作業繳交系統 管理指令")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("init")
    s.add_argument("--lan", action="store_true", help="監聽 0.0.0.0（內網測試）")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sp.add_parser("initdb").set_defaults(fn=cmd_initdb)
    s = sp.add_parser("create-admin")
    s.add_argument("--email")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_create_admin)
    s = sp.add_parser("seed-demo")
    s.add_argument("--active", action="store_true", help=f"直接啟用帳號，密碼 {DEMO_ACTIVE}")
    s.add_argument("--renew", action="store_true",
                   help="示範資料已存在時：補齊缺少的示範帳號／課程／作業，並重設既有示範帳號的密碼、狀態與角色")
    s.set_defaults(fn=cmd_seed_demo)
    s = sp.add_parser("totp-code")
    s.add_argument("email")
    s.set_defaults(fn=cmd_totp_code)
    sp.add_parser("backup").set_defaults(fn=cmd_backup)
    sp.add_parser("reset-instance").set_defaults(fn=cmd_reset_instance)
    s = sp.add_parser("reset-demo", help="測試環境完整重置（保留 .env）")
    s.add_argument("--yes", action="store_true", help="不詢問確認字串（腳本用）")
    s.add_argument("--force", action="store_true", help="資料庫有非示範帳號時仍然重置")
    s.set_defaults(fn=cmd_reset_demo)
    args = p.parse_args()
    if args.cmd != "init" and not ENV.exists():
        import os

        if not os.environ.get("AAMS_SECRET_KEY"):
            sys.exit("找不到 .env，請先執行：python manage.py init")
    args.fn(args)


if __name__ == "__main__":
    main()
