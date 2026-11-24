"""對應規格 §9.3 必要安全測試與 §12 驗收條件。"""
import io
import re
from datetime import timedelta

import pyotp
import pytest
from sqlalchemy import select

from aams.db import SessionLocal
from aams.models import Assignment, GradingTask, Group, Question, Revision, Submission, User, UserRole
from aams.security import hash_password
from aams.storage import inspect_pending_once
from aams.utils import canonical_email, csv_safe, utcnow

PW = "Ais3-Demo-2026"


def make_pdf(extra: bytes = b"", pad: int = 0) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    data = buf.getvalue()
    if pad or extra:
        head, rest = data.split(b"\n", 1)
        data = head + b"\n%" + b"A" * pad + b"\n" + extra + rest
    return data


def assignment(title):
    with SessionLocal() as db:
        return db.scalar(select(Assignment).where(Assignment.title == title))


def set_assignment(title, **kw):
    with SessionLocal() as db:
        a = db.scalar(select(Assignment).where(Assignment.title == title))
        for k, v in kw.items():
            setattr(a, k, v)
        db.commit()


def submit(b, a_id, files, ack=False):
    page = b.get(f"/assignments/{a_id}")
    idem = re.search(r'name="idempotency_key" value="([^"]+)"', page.text)
    data = {"idempotency_key": idem.group(1) if idem else "x" * 20}
    if ack:
        data["late_ack"] = "1"
    return b.post(f"/assignments/{a_id}/submit", data=data, files=[("files", f) for f in files])


A1 = "Lab 1：XSS 解題報告"      # individual, pdf, 20 MB
A2 = "Crackme 小組作業"          # group, jpg/pdf/png, multi 5, 40 MB


# ---------------------------------------------------------------- auth
def test_initial_password_flow(browser):
    with SessionLocal() as db:
        u = User(email="newbie@example.org", canonical_email=canonical_email("newbie@example.org"), name="新生",
                 status="PENDING_PASSWORD_CHANGE", password_hash=hash_password("10007038907"))
        db.add(u)
        db.flush()
        db.add(UserRole(user_id=u.id, role="STUDENT"))
        db.commit()
    b = browser()
    r = b.login("NewBie@Example.org ", "10007038907", follow=False)  # 大小寫／空白等價
    # 首次登入：必須先同意個資告知，才能改密碼
    assert r.status_code == 303 and r.headers["location"] == "/consent"
    assert b.c.get("/change-initial-password", follow_redirects=False).headers["location"] == "/consent"
    # 受限 Session 不能使用一般 API
    r = b.c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/change-initial-password"
    assert b.c.get("/staff", follow_redirects=False).status_code == 303
    page = b.get("/consent")
    # 沒有逐項勾選 → 拒絕
    assert b.post("/consent", {"items": ["read"]}).status_code == 400
    r = b.post("/consent", {"items": ["read", "purpose", "records"]})
    assert r.status_code == 303 and r.headers["location"] == "/change-initial-password"
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.canonical_email == "newbie@example.org")).consented_at is not None
    page = b.get("/change-initial-password")
    # 新密碼不可與初始密碼相同
    r = b.post("/change-initial-password", {"current_password": "10007038907", "new_password": "10007038907",
                                             "confirm_password": "10007038907"})
    assert r.status_code == 400
    r = b.post("/change-initial-password", {"current_password": "10007038907", "new_password": "BrandNew-Pass-1",
                                             "confirm_password": "BrandNew-Pass-1"})
    assert r.status_code == 303
    b2 = browser()
    assert b2.login("newbie@example.org", "10007038907", follow=False).status_code == 401  # 初始密碼失效
    r = b2.login("newbie@example.org", "BrandNew-Pass-1", follow=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_generic_error(browser):
    b = browser()
    r1 = b.login("nobody@example.org", "whatever-123", follow=False)
    r2 = b.login("s011@demo.ais3.local", "wrong-password", follow=False)
    assert r1.status_code == r2.status_code == 401
    assert "帳號或密碼錯誤" in r1.text and "帳號或密碼錯誤" in r2.text


def test_admin_requires_mfa(browser):
    b = browser()
    b.login("staff1@demo.ais3.local")
    r = b.c.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/mfa/setup")
    page = b.get("/mfa/setup?required=1")
    secret = re.search(r'手動金鑰：<span class="mono">([A-Z2-7]+)</span>', page.text).group(1)
    r = b.post("/mfa/setup", {"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 303
    assert b.c.get("/admin").status_code == 200
    assert "0901000001" in b.c.get("/admin/users?q=s011").text  # Admin 可看電話


def test_phone_hidden_from_non_admin(browser):
    for email in ("s011@demo.ais3.local", "ta011@demo.ais3.local", "instructor1@demo.ais3.local"):
        b = browser()
        b.login(email)
        for url in ("/", "/me", "/staff", "/admin/users"):
            r = b.c.get(url)
            assert "0901000001" not in r.text
        assert b.c.get("/admin/users", follow_redirects=False).status_code == 403


# ---------------------------------------------------------------- upload rules
def test_total_size_limit_20mb(browser):
    a = assignment(A1)
    set_assignment(A1, allow_multiple_files=True, max_files=5)
    b = browser()
    b.login("s012@demo.ais3.local")
    # 12 MB + 9 MB = 21 MB → 整次失敗
    r = submit(b, a.id, [("a.pdf", make_pdf(pad=12_000_000), "application/pdf"),
                         ("b.pdf", make_pdf(pad=9_000_000), "application/pdf")])
    assert r.status_code == 413
    # 單檔 21 MB → 拒絕
    r = submit(b, a.id, [("c.pdf", make_pdf(pad=21_000_000), "application/pdf")])
    assert r.status_code == 413
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.canonical_email == "s012@demo.ais3.local"))
        assert db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.student_id == u.id)) is None \
            or not db.scalars(select(Revision).join(Submission).where(Submission.student_id == u.id)).all()
    set_assignment(A1, allow_multiple_files=False, max_files=1)


def test_total_size_limit_40mb_group(browser):
    a = assignment(A2)
    b = browser()
    b.login("s021@demo.ais3.local")
    r = submit(b, a.id, [("a.pdf", make_pdf(pad=18_000_000), "application/pdf"),
                         ("b.pdf", make_pdf(pad=17_000_000), "application/pdf")])
    assert r.status_code == 303, r.text[:300]
    r = submit(b, a.id, [("a.pdf", make_pdf(pad=21_000_000), "application/pdf"),
                         ("b.pdf", make_pdf(pad=20_000_000), "application/pdf")])
    assert r.status_code == 413


def test_forged_content_length_is_not_trusted(browser):
    a = assignment(A1)
    b = browser()
    b.login("s013@demo.ais3.local")
    page = b.get(f"/assignments/{a.id}")
    body = b"--X\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.pdf\"\r\n\r\n" + b"%PDF-1.4\n" + b"A" * 43_000_000 + b"\r\n--X--\r\n"
    r = b.c.post(f"/assignments/{a.id}/submit", content=body,
                 headers={"Content-Type": "multipart/form-data; boundary=X", "Content-Length": "100"})
    assert r.status_code in (400, 413)


@pytest.mark.parametrize("name,content", [
    ("shell.php4", b"<?php system($_GET[1]); ?>"),
    ("report.pdf.php", b"%PDF-1.4 <?php ?>"),
    ("report.PHP", b"<?php ?>"),
    ("evil.php.pdf", b"%PDF-1.4\n"),
    ("fake.pdf", b"<?php echo 1; ?>"),         # 偽造副檔名 / MIME
    ("page.html", b"<script>alert(1)</script>"),
])
def test_dangerous_uploads_rejected(browser, name, content):
    a = assignment(A1)
    b = browser()
    b.login("s014@demo.ais3.local")
    r = submit(b, a.id, [(name, content, "application/pdf")])
    assert r.status_code == 400


def test_pdf_with_javascript_rejected_by_worker(browser):
    a = assignment(A1)
    b = browser()
    b.login("s015@demo.ais3.local")
    evil = make_pdf(extra=b"1 0 obj << /Type /Action /S /Java#53cript /JS (app.alert(1)) >> endobj\n")
    r = submit(b, a.id, [("evil.pdf", evil, "application/pdf")])
    assert r.status_code == 303
    inspect_pending_once()
    with SessionLocal() as db:
        rev = db.get(Revision, int(r.headers["location"].split("/")[2]))
        assert rev.scan_state == "REJECTED"


def test_retain_five_versions_and_late_flag(browser):
    a = assignment(A1)
    b = browser()
    b.login("s016@demo.ais3.local")
    for i in range(6):
        assert submit(b, a.id, [(f"v{i}.pdf", make_pdf(), "application/pdf")]).status_code == 303
    inspect_pending_once()
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.canonical_email == "s016@demo.ais3.local"))
        sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.student_id == u.id))
        revs = list(db.scalars(select(Revision).where(Revision.submission_id == sub.id).order_by(Revision.revision_no)))
        assert len(revs) == 6
        assert revs[0].purged_at is not None and all(r.purged_at is None for r in revs[1:])
        assert all(not r.is_late for r in revs)
    # 截止後提交 → LATE；需勾選確認
    set_assignment(A1, due_at=utcnow() - timedelta(seconds=1))
    assert submit(b, a.id, [("late.pdf", make_pdf(), "application/pdf")]).status_code == 400
    r = submit(b, a.id, [("late.pdf", make_pdf(), "application/pdf")], ack=True)
    assert r.status_code == 303
    with SessionLocal() as db:
        rev = db.get(Revision, int(r.headers["location"].split("/")[2]))
        assert rev.is_late
    set_assignment(A1, allow_late=False)
    assert submit(b, a.id, [("late2.pdf", make_pdf(), "application/pdf")], ack=True).status_code == 409
    set_assignment(A1, allow_late=True, due_at=utcnow() + timedelta(days=2))


# ---------------------------------------------------------------- ACL
def test_student_cannot_read_other_group(browser):
    a = assignment(A2)
    b = browser()
    b.login("s022@demo.ais3.local")  # 第 2 組，看得到 s021 交的
    page = b.get(f"/assignments/{a.id}")
    m = re.search(r"/artifacts/(\d+)/download", page.text)
    assert m, "group member should see group files"
    art_id = m.group(1)
    other = browser()
    other.login("s031@demo.ais3.local")  # 第 3 組
    assert other.c.get(f"/artifacts/{art_id}/download").status_code in (404, 409)
    assert other.c.get(f"/artifacts/{art_id}/preview").status_code in (404, 409)


def test_ta_grading_requires_task(browser):
    a = assignment(A2)
    inspect_pending_once()
    with SessionLocal() as db:
        g2 = db.scalar(select(Group).where(Group.code == "G02"))
        sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.group_id == g2.id))
        rev = db.scalar(select(Revision).where(Revision.submission_id == sub.id, Revision.scan_state == "ACCEPTED"))
        assert rev is not None
        q1, q2 = db.scalars(select(Question).where(Question.assignment_id == a.id).order_by(Question.ordinal)).all()
        ta = db.scalar(select(User).where(User.canonical_email == "ta021@demo.ais3.local"))
        other_ta = db.scalar(select(User).where(User.canonical_email == "ta081@demo.ais3.local"))
    # 本組 TA 未指派：可讀，不可改
    b = browser()
    b.login("ta021@demo.ais3.local")
    assert b.get(f"/staff/submissions/{sub.id}").status_code == 200
    r = b.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q1.id, "score": "10", "expected_version": 0})
    assert r.status_code == 403
    # 他組 TA 未指派：不可讀
    o = browser()
    o.login("ta081@demo.ais3.local")
    assert o.get(f"/staff/submissions/{sub.id}").status_code == 404
    # 講師指派 ta081 → G02 / Q2
    ins = browser()
    ins.login("instructor2@demo.ais3.local")
    ins.get(f"/staff/assignments/{a.id}/tasks")
    r = ins.post(f"/staff/assignments/{a.id}/tasks", {"grader_id": other_ta.id, "group_ids": [g2.id], "question_ids": [q2.id]})
    assert r.status_code == 303
    o.get(f"/staff/submissions/{sub.id}")
    assert o.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q2.id, "score": "40", "expected_version": 0}).status_code == 303
    assert o.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q1.id, "score": "40", "expected_version": 0}).status_code == 403
    # 其他講師（非本課）不可管理
    ins1 = browser()
    ins1.login("instructor1@demo.ais3.local")
    assert ins1.c.get(f"/staff/assignments/{a.id}/tasks").status_code == 403
    # 撤銷後立即失效
    with SessionLocal() as db:
        t = db.scalar(select(GradingTask).where(GradingTask.grader_id == other_ta.id, GradingTask.revoked_at.is_(None)))
    ins.get(f"/staff/assignments/{a.id}/tasks")
    assert ins.post(f"/staff/tasks/{t.id}/revoke").status_code == 303
    assert o.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q2.id, "score": "41", "expected_version": 1}).status_code == 403
    assert o.c.get(f"/staff/submissions/{sub.id}").status_code == 404


def test_grade_conflict_and_release(browser):
    a = assignment(A2)
    with SessionLocal() as db:
        g2 = db.scalar(select(Group).where(Group.code == "G02"))
        sub = db.scalar(select(Submission).where(Submission.assignment_id == a.id, Submission.group_id == g2.id))
        rev = db.scalar(select(Revision).where(Revision.submission_id == sub.id, Revision.scan_state == "ACCEPTED"))
        q1, q2 = db.scalars(select(Question).where(Question.assignment_id == a.id).order_by(Question.ordinal)).all()
    i1 = browser()
    i1.login("instructor2@demo.ais3.local")
    i1.get(f"/staff/submissions/{sub.id}")
    assert i1.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q1.id, "score": "0", "expected_version": 0}).headers["location"].endswith(f"msg=saved#q{q1.id}")
    # 第二位用過期版本 → conflict
    r = i1.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q1.id, "score": "5", "expected_version": 0})
    assert "msg=conflict" in r.headers["location"]
    # 超過滿分拒絕
    assert i1.post(f"/staff/revisions/{rev.id}/grade-item", {"question_id": q1.id, "score": "999", "expected_version": 1}).status_code == 400
    # 發布前學生看不到分數
    s = browser()
    s.login("s023@demo.ais3.local")
    page = s.get(f"/assignments/{a.id}")
    assert "第 1 次發布" not in page.text
    # G02 已有 Q1=0（真實零分）與 Q2=40 → 完成，可統一發布
    i1.get(f"/staff/assignments/{a.id}/release")
    r = i1.post(f"/staff/assignments/{a.id}/release", {"confirm": "1"})
    assert r.status_code == 303
    page = s.get(f"/assignments/{a.id}")
    assert "第 1 次發布" in page.text and '<p class="kpi">40' in page.text
    # 發布後補交：舊快照不變，學生看到「最新交件待處理」；republish 需原因
    r = submit(s, a.id, [("v2.pdf", make_pdf(), "application/pdf")], ack=True)
    assert r.status_code == 303
    page = s.get(f"/assignments/{a.id}")
    assert '<p class="kpi">40' in page.text and "待助教處理" in page.text
    i1.get(f"/staff/assignments/{a.id}/release")
    assert i1.post(f"/staff/assignments/{a.id}/release", {"confirm": "1", "reason": "regrade"}).status_code == 409  # 新版檢查中 → 阻擋


def test_csv_formula_neutralization():
    assert csv_safe("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert csv_safe("+1+1") == "'+1+1"
    assert csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert csv_safe("-5") == "-5"
    assert csv_safe("正常文字") == "正常文字"


def test_xss_in_discord_and_filename_escaped(browser):
    b = browser()
    b.login("s111@demo.ais3.local")
    b.get("/me")
    r = b.post("/me/discord", {"discord_id": "<script>alert(1)</script>"})
    assert r.status_code == 400
    a = assignment(A1)
    r = submit(b, a.id, [("<img src=x onerror=alert(1)>.pdf", make_pdf(), "application/pdf")])
    assert r.status_code == 303
    page = b.get(f"/assignments/{a.id}")
    assert "<img src=x onerror" not in page.text


def test_preview_token_required(preview_client):
    assert preview_client.get("/p/forged.token").status_code == 403
