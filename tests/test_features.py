"""新功能測試：個資同意、公告、倒數／最終收件期限、遲交核准、Discord、監控、申覆、批次下載、相似度、欄位升級。"""
import io
import random
import re
import time
import zipfile
from datetime import timedelta
from decimal import Decimal

import pyotp
import pytest
from sqlalchemy import select, text

from aams import monitor, notify
from aams.config import settings
from aams.db import SessionLocal, add_missing_columns, engine
from aams.models import (
    Announcement,
    Assignment,
    Course,
    GradeAppeal,
    GradeItem,
    Question,
    Revision,
    SimilarityRun,
    Submission,
    User,
)
from aams.storage import inspect_pending_once
from aams.utils import utcnow

from test_security import make_pdf, submit  # noqa: E402


# ---------------------------------------------------------------- helpers
def make_text_pdf(lines: list[str]) -> bytes:
    """產生含真正文字（Helvetica）的最小 PDF，供文字抽取／相似度測試。"""
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream = "BT /F1 10 Tf 40 800 Td 12 TL " + " ".join(f"({ln}) Tj T*" for ln in esc) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def words(seed: int, n: int) -> list[str]:
    rnd = random.Random(seed)
    vocab = ["buffer", "overflow", "payload", "stack", "canary", "heap", "pointer", "return", "address", "shell",
             "exploit", "gadget", "libc", "offset", "padding", "register", "syscall", "kernel", "format", "string",
             "injection", "cookie", "session", "token", "header", "cipher", "padding", "oracle", "nonce", "hash"]
    lines = []
    for _ in range(n):
        lines.append(" ".join(rnd.choice(vocab) + str(rnd.randint(0, 99)) for _ in range(9)))
    return lines


def uid(email):
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.canonical_email == email)).id


@pytest.fixture(scope="module")
def admin():
    from conftest import Browser

    b = Browser()
    b.login("staff3@demo.ais3.local")
    page = b.get("/mfa/setup?required=1")
    secret = re.search(r'手動金鑰：<span class="mono">([A-Z2-7]+)</span>', page.text).group(1)
    assert b.post("/mfa/setup", {"code": pyotp.TOTP(secret).now()}).status_code == 303
    return b


class _Sent(list):
    """攔截到的 Discord 訊息 [(channel, text)]；.roles 是每則訊息要 @ 的身分組。"""

    def __init__(self, box, roles):
        super().__init__()
        self._box, self.roles = box, roles

    def __iter__(self):
        return iter(self._box)

    def __len__(self):
        return len(self._box)

    def clear(self):
        self._box.clear()
        self.roles.clear()


@pytest.fixture
def sent(monkeypatch):
    """攔截 Discord 傳送（不連網路）。"""
    box = []

    def fake(ch, txt, role_ids=None):
        box.append((ch, txt))
        fake.roles.append(role_ids)
        return True

    fake.roles = []
    monkeypatch.setattr(notify, "send", fake)
    return _Sent(box, fake.roles)


@pytest.fixture(scope="module")
def lab():
    """建立一份獨立的個人作業（第一門課，instructor1 負責），一題 10 分。"""
    with SessionLocal() as db:
        course = db.scalar(select(Course).where(Course.title == "Web Security 入門"))
        ins = db.scalar(select(User).where(User.canonical_email == "instructor1@demo.ais3.local"))
        a = Assignment(course_id=course.id, title="Feature Lab", status="PUBLISHED", submission_mode="INDIVIDUAL",
                       due_at=utcnow() + timedelta(days=1), final_deadline=utcnow() + timedelta(days=3),
                       allowed_types="pdf", allow_multiple_files=True, max_files=3, max_total_bytes=20_000_000,
                       created_by=ins.id, name_en="FeatLab", show_submission_rate=False)
        db.add(a)
        db.flush()
        q = Question(assignment_id=a.id, ordinal=1, title="Q", max_points=Decimal(10))
        db.add(q)
        db.commit()
        return {"id": a.id, "q": q.id}


def set_lab(lab, **kw):
    with SessionLocal() as db:
        a = db.get(Assignment, lab["id"])
        for k, v in kw.items():
            setattr(a, k, v)
        db.commit()


# ---------------------------------------------------------------- 1. consent
def test_privacy_page_public_and_me_dialog(browser):
    b = browser()
    assert b.c.get("/privacy").status_code == 200  # 未登入也能看
    b.login("s041@demo.ais3.local")
    page = b.get("/me")
    assert 'id="privacy-dialog"' in page.text and "已同意" in page.text


def test_consent_required_for_staff_roles(browser):
    b = browser()
    r = b.login("ta031@demo.ais3.local", follow=False)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.canonical_email == "ta031@demo.ais3.local"))
        consented = u.consented_at
    if consented is None:
        assert b.c.get("/staff", follow_redirects=False).headers["location"].startswith("/consent")
    b.consent()
    assert b.c.get("/staff").status_code == 200


# ---------------------------------------------------------------- 2. announcements
def test_announcements(browser, admin, sent):
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    ins.get("/announcements/new")
    # 講師不能發全營公告
    assert ins.post("/announcements/new", {"course_id": "global", "title": "x", "body": "y"}).status_code == 403
    with SessionLocal() as db:
        c1 = db.scalar(select(Course).where(Course.title == "Web Security 入門"))
        c2 = db.scalar(select(Course).where(Course.title == "Reverse Engineering"))
    # 也不能發到別人的課
    assert ins.post("/announcements/new", {"course_id": str(c2.id), "title": "x", "body": "y"}).status_code == 403
    assert ins.post("/announcements/new", {"course_id": str(c1.id), "title": "Lab 教室改到 B201",
                                           "body": "<script>alert(1)</script>", "pinned": "1"}).status_code == 303
    assert ins.post("/announcements/new", {"course_id": str(c1.id), "title": "隱藏草稿", "body": "z",
                                           "status": "HIDDEN"}).status_code == 303
    # 助教不能發公告
    ta = browser()
    ta.login("ta011@demo.ais3.local")
    assert ta.c.get("/announcements/new").status_code == 403
    # 管理員發全營公告 → Discord 學員頻道（只推一次）
    admin.get("/announcements/new")
    assert admin.post("/announcements/new", {"course_id": "global", "title": "午餐改 12:30", "body": "請準時"}).status_code == 303
    student_msgs = [t for ch, t in sent if ch == "student"]
    assert any("午餐改 12:30" in t for t in student_msgs) and any("B201" in t for t in student_msgs)
    assert not any("隱藏草稿" in t for t in student_msgs)

    s = browser()
    s.login("s042@demo.ais3.local")
    home = s.get("/")
    assert "午餐改 12:30" in home.text and 'badge-count' in home.text
    course = s.get(f"/courses/{c1.id}")
    assert "B201" in course.text and "隱藏草稿" not in course.text
    assert "<script>alert(1)</script>" not in course.text  # 跳脫
    s.get("/announcements")  # 打開公告頁 → 已讀
    assert "公告<span class=\"badge-count\">" not in s.get("/").text
    # 只有作者課程的講師／管理員能編輯
    with SessionLocal() as db:
        ann = db.scalar(select(Announcement).where(Announcement.title == "Lab 教室改到 B201"))
    other = browser()
    other.login("instructor2@demo.ais3.local")
    assert other.c.get(f"/announcements/{ann.id}/edit").status_code == 404


# ---------------------------------------------------------------- 4/5. countdown, final deadline, late note
def test_final_deadline_default_and_validation(browser, monkeypatch):
    monkeypatch.setattr(settings, "event_end_at", "2026-07-10")
    from aams.services import default_final_deadline

    # 2026-07-10 23:59 台北 = 15:59 UTC，+7 天
    assert default_final_deadline(None).strftime("%Y-%m-%d %H:%M") == "2026-07-17 15:59"
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    with SessionLocal() as db:
        c1 = db.scalar(select(Course).where(Course.title == "Web Security 入門"))
    page = ins.get(f"/staff/courses/{c1.id}/assignments/new")
    assert 'name="final_deadline" value="2026-07-17T23:59"' in page.text
    base = {"title": "期限測試", "submission_mode": "INDIVIDUAL", "due_at": "2026-07-09T18:00",
            "allowed_types": ["pdf"], "max_total_mb": "20", "status": "DRAFT", "name_en": "Bad Name!"}
    r = ins.post(f"/staff/courses/{c1.id}/assignments/new", {**base, "final_deadline": "2026-07-08T18:00"})
    assert r.status_code == 400 and "不可早於截止時間" in r.text and "英文名稱" in r.text
    r = ins.post(f"/staff/courses/{c1.id}/assignments/new", {**base, "name_en": "DeadlineTest", "final_deadline": ""})
    assert r.status_code == 303
    with SessionLocal() as db:
        a = db.scalar(select(Assignment).where(Assignment.title == "期限測試"))
        assert a.final_deadline.strftime("%Y-%m-%d %H:%M") == "2026-07-17 15:59" and a.name_en == "DeadlineTest"
    # 助教不能改作業設定（含最終收件期限）
    ta = browser()
    ta.login("ta011@demo.ais3.local")
    assert ta.c.get(f"/staff/assignments/{a.id}/edit").status_code == 403


def test_submission_flow_late_and_final_deadline(browser, lab):
    for email in ("s041@demo.ais3.local", "s043@demo.ais3.local"):
        b = browser()
        b.login(email)
        page = b.get(f"/assignments/{lab['id']}")
        assert "data-countdown=" in page.text and "data-server-epoch=" in page.text and "最終收件期限" in page.text
        r = submit(b, lab["id"], [("report.pdf", make_text_pdf(words(41 if "41" in email else 43, 25)), "application/pdf")])
        assert r.status_code == 303, r.text[:300]
    b43 = browser()
    b43.login("s043@demo.ais3.local")
    assert submit(b43, lab["id"], [("report-v2.pdf", make_text_pdf(words(43, 25)), "application/pdf"),
                                   ("appendix.pdf", make_pdf(), "application/pdf")]).status_code == 303
    # 截止後 → LATE（仍在最終收件期限內）
    set_lab(lab, due_at=utcnow() - timedelta(hours=2, minutes=5))
    b42 = browser()
    b42.login("s042@demo.ais3.local")
    r = submit(b42, lab["id"], [("late.pdf", make_text_pdf(words(41, 25)[:20] + words(99, 5)), "application/pdf")], ack=True)
    assert r.status_code == 303
    inspect_pending_once()
    # 超過最終收件期限 → 一律拒收
    set_lab(lab, final_deadline=utcnow() - timedelta(minutes=1))
    page = b42.get(f"/assignments/{lab['id']}")
    assert "已超過最終收件期限" in page.text
    assert submit(b42, lab["id"], [("late2.pdf", make_pdf(), "application/pdf")], ack=True).status_code == 409
    set_lab(lab, final_deadline=utcnow() + timedelta(days=3))


def test_late_note(browser, lab):
    with SessionLocal() as db:
        sub = db.scalar(select(Submission).where(Submission.assignment_id == lab["id"],
                                                 Submission.student_id == uid("s042@demo.ais3.local")))
        late = db.scalar(select(Revision).where(Revision.submission_id == sub.id))
        sub41 = db.scalar(select(Submission).where(Submission.assignment_id == lab["id"],
                                                   Submission.student_id == uid("s041@demo.ais3.local")))
        ontime = db.scalar(select(Revision).where(Revision.submission_id == sub41.id))
    ta = browser()
    ta.login("ta041@demo.ais3.local")
    ta.get(f"/staff/submissions/{sub.id}")
    assert ta.post(f"/staff/revisions/{late.id}/late-note", {"note": "x"}).status_code == 403
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    page = ins.get(f"/staff/submissions/{sub.id}")
    assert "遲交核准註記" in page.text
    assert ins.post(f"/staff/revisions/{ontime.id}/late-note", {"note": "x"}).status_code == 400
    assert ins.post(f"/staff/revisions/{late.id}/late-note", {"note": "現場網路中斷，已確認"}).status_code == 303
    with SessionLocal() as db:
        r = db.get(Revision, late.id)
        assert r.is_late and r.late_note == "現場網路中斷，已確認"  # LATE 標記不變


# ---------------------------------------------------------------- 9. downloads
def _zip(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text[:300]
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_downloads(browser, lab):
    s41, s42, s43, s44 = (uid(f"s04{i}@demo.ais3.local") for i in (1, 2, 3, 4))
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    page = ins.get(f"/downloads?assignment_id={lab['id']}")
    assert f'value="s{s41}"' in page.text
    z = _zip(ins.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s}" for s in (s41, s42, s43, s44)]}))
    names = z.namelist()
    with SessionLocal() as db:
        n41 = db.get(User, s41).name
        n42 = db.get(User, s42).name
    assert f"Group04_{n41}/report.pdf" in names
    assert f"Group04_{n42}_LATE/late.pdf" in names and f"Group04_{n42}_LATE/README.txt" in names
    readme = z.read(f"Group04_{n42}_LATE/README.txt").decode()
    assert "遲交 2 小時" in readme and "已核准遲交：現場網路中斷" in readme
    summary = z.read("_SUMMARY.txt").decode()
    assert "未交" in summary and "MANIFEST.csv" in names
    # 強制統一命名 + 所有版本
    z = _zip(ins.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s43}", f"s{s42}"],
                                         "versions": "all", "force_naming": "1"}))
    names = z.namelist()
    with SessionLocal() as db:
        n43 = db.get(User, s43).name
    assert f"Group04_{n43}/v2/Group04_{n43}_WebSecurity_FeatLab_1.pdf" in names
    assert f"Group04_{n43}/v1/Group04_{n43}_WebSecurity_FeatLab.pdf" in names
    assert f"Group04_{n42}_LATE/v1_LATE/Group04_{n42}_WebSecurity_FeatLab_LATE.pdf" in names
    # 學員只能下載自己的；送別人的 key → 403
    s = browser()
    s.login("s041@demo.ais3.local")
    page = s.get(f"/downloads?assignment_id={lab['id']}")
    assert f'value="s{s41}"' in page.text and f'value="s{s42}"' not in page.text
    assert s.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s42}"]}).status_code == 403
    z = _zip(s.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s41}"]}))
    assert "已核准" not in z.read("_SUMMARY.txt").decode()
    # 助教：自己帶的組可以，別組不行
    ta = browser()
    ta.login("ta041@demo.ais3.local")
    assert ta.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s41}"]}).status_code == 200
    other = browser()
    other.login("ta051@demo.ais3.local")
    assert other.post("/downloads/zip", {"assignment_id": lab["id"], "units": [f"s{s41}"]}).status_code == 403


# ---------------------------------------------------------------- 8. appeals
def test_appeals(browser, lab, sent, monkeypatch):
    with SessionLocal() as db:
        for email, score in (("s041@demo.ais3.local", "7"), ("s042@demo.ais3.local", "5"), ("s043@demo.ais3.local", "9")):
            sub = db.scalar(select(Submission).where(Submission.assignment_id == lab["id"],
                                                     Submission.student_id == uid(email)))
            rev = db.scalar(select(Revision).where(Revision.submission_id == sub.id, Revision.scan_state == "ACCEPTED")
                            .order_by(Revision.revision_no.desc()))
            db.add(GradeItem(revision_id=rev.id, question_id=lab["q"], score=Decimal(score)))
        db.commit()
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    ins.get(f"/staff/assignments/{lab['id']}/release")
    assert ins.post(f"/staff/assignments/{lab['id']}/release", {"confirm": "1"}).status_code == 303
    assert any(ch == "student" and "成績已發布" in t for ch, t in sent)
    s = browser()
    s.login("s041@demo.ais3.local")
    page = s.get(f"/assignments/{lab['id']}")
    assert "提出申覆" in page.text
    assert s.post(f"/assignments/{lab['id']}/appeals", {"question_id": str(lab["q"]), "reason": "短"}).status_code == 400
    r = s.post(f"/assignments/{lab['id']}/appeals", {"question_id": str(lab["q"]), "reason": "第二小題的 payload 其實有成功"})
    assert r.status_code == 303
    assert any(ch == "staff" and "成績申覆" in t for ch, t in sent)
    # 同一題已有處理中 → 409
    assert s.post(f"/assignments/{lab['id']}/appeals", {"question_id": str(lab["q"]), "reason": "再申覆一次可以嗎"}).status_code == 409
    with SessionLocal() as db:
        ap = db.scalar(select(GradeAppeal).where(GradeAppeal.assignment_id == lab["id"]))
    # 助教不能處理
    ta = browser()
    ta.login("ta041@demo.ais3.local")
    ta.get("/staff")
    assert ta.post(f"/staff/appeals/{ap.id}/resolve", {"decision": "KEPT", "response": "x"}).status_code == 403
    page = ins.get("/staff/appeals")
    assert "第二小題的 payload" in page.text
    assert ins.post(f"/staff/appeals/{ap.id}/resolve", {"decision": "KEPT", "response": "評分標準是要拿到 shell"}).status_code == 303
    assert "評分標準是要拿到 shell" in s.get(f"/assignments/{lab['id']}").text
    # 超過期限（第一次發布後 3 天）→ 409
    monkeypatch.setattr(settings, "appeal_days", -1)
    s2 = browser()
    s2.login("s043@demo.ais3.local")
    s2.get(f"/assignments/{lab['id']}")
    assert s2.post(f"/assignments/{lab['id']}/appeals", {"question_id": "total", "reason": "總分好像算錯了"}).status_code == 409


# ---------------------------------------------------------------- 10. similarity
def test_similarity_compute_unit():
    from aams.similarity import compute

    common = " ".join(words(1, 6))
    docs = [{"key": f"s{i}", "owner": f"s{i}", "label": f"S{i}", "text": common + " " + " ".join(words(100 + i, 20))}
            for i in range(5)]
    docs[1]["text"] = docs[0]["text"]  # 完全抄
    docs.append({"key": "s9", "owner": "s9", "label": "S9", "text": "截圖"})  # 抽不到文字
    res = compute(docs)
    top = res["pairs"][0]
    assert {top["a"], top["b"]} == {"s0", "s1"} and top["containment"] > 0.9 and top["passages"]
    assert all({p["a"], p["b"]} == {"s0", "s1"} for p in res["pairs"])  # 共同段落（超過一半都有）被忽略
    assert any(d["key"] == "s9" and d["status"] == "NO_TEXT" for d in res["docs"])


def test_similarity_end_to_end(browser, lab):
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    ins.get(f"/staff/assignments/{lab['id']}/similarity")
    r = ins.post(f"/staff/assignments/{lab['id']}/similarity", files={"template": ("t.pdf", make_text_pdf(["hello"]), "application/pdf")})
    assert r.status_code == 303
    deadline = time.time() + 60
    while time.time() < deadline:
        with SessionLocal() as db:
            sr = db.scalar(select(SimilarityRun).where(SimilarityRun.assignment_id == lab["id"]).order_by(SimilarityRun.id.desc()))
            if sr.status in ("DONE", "FAILED"):
                break
        time.sleep(0.5)
    assert sr.status == "DONE", sr.detail
    page = ins.get(f"/staff/assignments/{lab['id']}/similarity")
    # s042 的遲交版本抄了 s041 的 20 行 → 應該是第一名
    assert "比對 3 份" in sr.detail
    assert re.search(r'class="pct high">\d+%', page.text)
    # 學員、助教不能看
    s = browser()
    s.login("s041@demo.ais3.local")
    assert s.c.get(f"/staff/assignments/{lab['id']}/similarity").status_code == 403
    ta = browser()
    ta.login("ta041@demo.ais3.local")
    assert ta.c.get(f"/staff/assignments/{lab['id']}/similarity").status_code == 403


# ---------------------------------------------------------------- 6/7. discord + monitor
def test_discord_payload_and_config(monkeypatch):
    import json

    body = json.loads(notify.payload("@everyone 測試"))
    assert body["allowed_mentions"] == {"parse": [], "roles": []}  # 不 @ 任何人
    monkeypatch.setattr(settings, "discord_staff_webhook", "https://evil.example.com/api/webhooks/1/x")
    assert not notify.configured("staff") and notify.invalid_config() == ["staff"]
    assert notify.send("staff", "x") is False  # 不合法的 URL 不會送
    calls = []
    good = "https://discord.com/api/webhooks/123456789012345678/" + "a" * 68
    monkeypatch.setattr(settings, "discord_staff_webhook", good)
    monkeypatch.setattr(notify, "_post", lambda url, b: calls.append(url) or (204, 0.0))
    assert notify.deliver("staff", "hi") and calls == [good]
    for bad in ("https://discord.com/api/webhooks/1/abc",                       # ID / Token 長度不對
                "https://discord.com.evil.io/api/webhooks/123456789012345678/" + "a" * 68,
                "http://discord.com/api/webhooks/123456789012345678/" + "a" * 68,
                good + "/../../x", good + "?wait=true"):
        monkeypatch.setattr(settings, "discord_staff_webhook", bad)
        assert not notify.configured("staff"), bad


# ---------------------------------------------------------------- 6c. 網頁設定 webhook／提醒預設
WH1 = "https://discord.com/api/webhooks/111111111111111111/" + "T1" * 34
WH2 = "https://discord.com/api/webhooks/222222222222222222/" + "T2" * 34


def _extra_admin(browser):
    """另一位管理員（已同意個資、已啟用 TOTP）。"""
    from aams.models import UserRole
    from aams.security import hash_password

    email = "staff-extra@test.local"
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.canonical_email == email)) is None:
            u = User(email=email, canonical_email=email, name="另一位管理員", status="ACTIVE",
                     password_hash=hash_password("Ais3-Demo-2026"), consented_at=utcnow())
            u.roles.append(UserRole(role="ADMIN"))
            db.add(u)
            db.commit()
    b = browser()
    b.login(email)
    page = b.get("/mfa/setup?required=1")
    secret = re.search(r'手動金鑰：<span class="mono">([A-Z2-7]+)</span>', page.text).group(1)
    assert b.post("/mfa/setup", {"code": pyotp.TOTP(secret).now()}).status_code == 303
    return b


def test_escalating_lock_timing(monkeypatch):
    import types

    from aams import security

    t = [1000.0]
    monkeypatch.setattr(security, "time", types.SimpleNamespace(monotonic=lambda: t[0], time=time.time))
    lock = security.EscalatingLock(first=5, first_s=300, again=3, again_s=900, forget_s=3600)
    assert [lock.fail("k") for _ in range(4)] == [0, 0, 0, 0] and lock.remaining("k") == 0
    assert lock.fail("k") == 300 and lock.remaining("k") == 300       # 第 5 次 → 5 分鐘
    t[0] += 301
    assert lock.remaining("k") == 0
    assert [lock.fail("k") for _ in range(2)] == [0, 0]
    assert lock.fail("k") == 900 and lock.remaining("k") == 900       # 解鎖後再錯 3 次 → 15 分鐘
    t[0] += 901
    assert lock.fail("k") == 0 and lock.fail("k") == 0 and lock.fail("k") == 900  # 之後每 3 次都 15 分鐘
    t[0] += 901 + 3601
    assert [lock.fail("k") for _ in range(4)] == [0, 0, 0, 0]           # 1 小時沒再錯 → 歸零，重新從 5 次算
    lock.reset("k")
    assert lock.remaining("k") == 0 and lock.locked() == []


def test_login_lock_and_admin_unlock(browser, admin):
    from aams.security import login_fail_lock

    b = browser()
    for i in range(4):
        assert b.login("s013@demo.ais3.local", "wrong-password", follow=False).status_code == 401
    r = b.login("s013@demo.ais3.local", "wrong-password", follow=False)
    assert r.status_code == 429 and "5 分鐘" in r.text and "工作人員" in r.text
    # 鎖住期間正確密碼也不行
    assert b.login("s013@demo.ais3.local", follow=False).status_code == 429
    page = admin.get("/admin/system").text
    assert "s013@demo.ais3.local" in page
    key = next(k for k, _, _ in login_fail_lock.locked() if k.startswith("s013@"))
    assert admin.post("/admin/system/unlock", {"kind": "login", "key": key}).status_code == 303
    assert admin.post("/admin/system/unlock", {"kind": "login", "key": key}).status_code == 404  # 已解除
    assert b.login("s013@demo.ais3.local", follow=False).status_code == 303
    # 非管理員不能解除
    ta = browser()
    ta.login("ta041@demo.ais3.local")
    assert ta.post("/admin/system/unlock", {"kind": "login", "key": key}).status_code == 403


def test_web_webhook_settings(admin, browser, monkeypatch):
    import json

    from aams import sysconfig
    from aams.models import AuditEvent, SystemSetting
    posts = []
    monkeypatch.setattr(notify, "_post", lambda url, b: posts.append((url, json.loads(b)["content"])) or (204, 0.0))
    monkeypatch.setattr(settings, "discord_student_webhook", "")
    pw = "Ais3-Demo-2026"
    url = "/admin/system/discord-webhook"

    # 講師、助教、學員都不能設定
    ins = browser()
    ins.login("instructor1@demo.ais3.local")
    ins.get("/staff")
    assert ins.post(url, {"channel": "student", "url": WH1, "password": pw}).status_code == 403

    admin.get("/admin/system")
    # 格式錯誤（含 SSRF 嘗試）、密碼錯誤都不會存
    assert admin.post(url, {"channel": "student", "url": "https://evil.example/api/webhooks/1/x", "password": pw}).status_code == 400
    assert admin.post(url, {"channel": "student", "url": WH1, "password": "wrong"}).status_code == 403
    assert sysconfig.webhook_source("student") == "none"

    # 第一次設定：不需要勾選警示；存完立刻送測試訊息
    r = admin.post(url, {"channel": "student", "url": WH1, "password": pw})
    assert r.status_code == 303 and "webhook_saved" in r.headers["location"]
    assert notify.webhook_url("student") == WH1 and posts[-1][0] == WH1
    with SessionLocal() as db:
        raw = db.get(SystemSetting, "discord_student_webhook").value
    assert WH1 not in raw and "T1T1" not in raw  # 加密存放

    # 網頁上只顯示 Webhook ID，Token 永遠不出現
    page = admin.get("/admin/system").text
    assert "✓ 已設定" in page and "111111111111111111" in page and "T1T1" not in page and "已經設定過" in page

    # 已設定後更換：沒勾「我了解」→ 拒絕；勾了 → 先通知舊頻道，再切換
    assert admin.post(url, {"channel": "student", "url": WH2, "password": pw}).status_code == 400
    posts.clear()
    r = admin.post(url, {"channel": "student", "action": "set", "url": WH2, "password": pw, "confirm": "yes"})
    assert r.status_code == 303
    assert posts[0][0] == WH1 and "已被" in posts[0][1]   # 舊頻道收到變更通知
    assert posts[1][0] == WH2                            # 新頻道收到測試訊息
    assert notify.webhook_url("student") == WH2

    # 密碼錯 5 次 → 鎖住（連正確密碼也不行），通知 staff 頻道；自己不能解，另一位管理員可以
    from aams.security import webhook_pw_lock

    sends = []
    monkeypatch.setattr(notify, "send", lambda ch, t, role_ids=None: sends.append((ch, t)) or True)
    for _ in range(5):
        assert admin.post(url, {"channel": "student", "action": "clear", "password": "nope", "confirm": "yes"}).status_code == 403
    assert any(ch == "staff" and "密碼連續錯誤" in t for ch, t in sends)
    assert admin.post(url, {"channel": "student", "action": "clear", "password": pw, "confirm": "yes"}).status_code == 429
    page = admin.get("/admin/system").text
    assert "請另一位管理員解除" in page
    me_id = next(k for k, _, _ in webhook_pw_lock.locked())
    assert admin.post("/admin/system/unlock", {"kind": "webhook", "key": me_id}).status_code == 403
    other = _extra_admin(browser)
    other.get("/admin/system")
    assert other.post("/admin/system/unlock", {"kind": "webhook", "key": me_id}).status_code == 303
    assert webhook_pw_lock.locked() == []

    # 停用
    posts.clear()
    assert admin.post(url, {"channel": "student", "action": "clear", "password": pw, "confirm": "yes"}).status_code == 303
    assert posts and posts[0][0] == WH2 and not notify.configured("student")

    # .env 有填 → 鎖住
    monkeypatch.setattr(settings, "discord_student_webhook", WH1)
    assert sysconfig.webhook_source("student") == "env"
    assert admin.post(url, {"channel": "student", "url": WH2, "password": pw, "confirm": "yes"}).status_code == 409
    page = admin.get("/admin/system").text
    assert "由伺服器 .env 設定" in page and "T1T1" not in page

    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("system.webhook%"))))
    assert {"system.webhook_set", "system.webhook_change", "system.webhook_clear", "system.webhook_bad_password"} <= actions


def test_web_reminder_hours(admin, monkeypatch):
    from aams import reminders, sysconfig

    monkeypatch.setattr(settings, "reminder_hours_env", "")
    url = "/admin/system/reminder-hours"
    admin.get("/admin/system")
    assert reminders.default_hours() == [2.0, 1.0] and sysconfig.reminder_source() == "default"
    assert admin.post(url, {"hours": "abc"}).status_code == 400
    try:
        assert admin.post(url, {"hours": "3, 1，0.5"}).status_code == 303
        assert reminders.default_hours() == [3.0, 1.0, 0.5] and sysconfig.reminder_source() == "web"
        assert "3 小時、1 小時、30 分鐘" in admin.get("/admin/system").text
    finally:
        assert admin.post(url, {"action": "reset"}).status_code == 303
    assert reminders.default_hours() == [2.0, 1.0]
    monkeypatch.setattr(settings, "reminder_hours_env", "4")
    assert reminders.default_hours() == [4.0]
    assert admin.post(url, {"hours": "1"}).status_code == 409


def test_monitor_alerts_and_throttle(sent, monkeypatch, admin):
    monkeypatch.setattr(settings, "alert_disk_free_pct", 101)  # 強制觸發
    monkeypatch.setattr(settings, "alert_errors_5min", 3)
    for _ in range(3):
        monitor.record_error()
    first = monitor.run_once()
    assert any("磁碟空間" in m for m in first) and any("伺服器錯誤" in m for m in first)
    assert any(ch == "staff" and "磁碟空間" in t for ch, t in sent)
    assert monitor.run_once() == []  # 30 分鐘內不重複發
    page = admin.get("/admin/system")
    assert "這個版本還沒做自動備份" in page.text and "磁碟空間不足" in page.text


# ---------------------------------------------------------------- 0. schema upgrade
def test_add_missing_columns():
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE "users" DROP COLUMN "announcements_seen_at"'))
    added = add_missing_columns()
    assert "users.announcements_seen_at" in added
    assert add_missing_columns() == []


# ---------------------------------------------------------------- 6b. 截止前提醒與 @身分組
def test_reminder_parse_hours():
    from aams.reminders import hours_label, parse_hours

    assert parse_hours("2,1") == [2.0, 1.0]
    assert parse_hours(" 1，0.5 , 1 ") == [1.0, 0.5]  # 全形逗號、重複
    assert hours_label(0.5) == "30 分鐘" and hours_label(2) == "2 小時" and hours_label(1.5) == "1 小時 30 分"
    for bad in ("abc", "0", "-1", "200", "1,2,3,4,5,6"):
        with pytest.raises(ValueError):
            parse_hours(bad)


def _mk_assignment(title, due, **kw):
    with SessionLocal() as db:
        course = db.scalar(select(Course).where(Course.title == "Reverse Engineering"))
        ins = db.scalar(select(User).where(User.canonical_email == "instructor2@demo.ais3.local"))
        a = Assignment(course_id=course.id, title=title, status="PUBLISHED", due_at=due,
                       final_deadline=due + timedelta(days=7), created_by=ins.id, **kw)
        db.add(a)
        db.commit()
        return a.id


def test_discord_roles_page_and_payload(browser):
    ta = browser()
    ta.login("ta021@demo.ais3.local")
    assert ta.c.get("/staff/discord").status_code == 403
    ins = browser()
    ins.login("instructor2@demo.ais3.local")
    assert ins.get("/staff/discord").status_code == 200
    assert ins.post("/staff/discord/roles", {"label": "學員", "role_id": "123"}).status_code == 400
    assert ins.post("/staff/discord/roles", {"label": "@學員", "role_id": "<@&112233445566778899>"}).status_code == 303
    assert ins.post("/staff/discord/roles", {"label": "重複", "role_id": "112233445566778899"}).status_code == 409
    import json

    body = json.loads(notify.payload("截止提醒 @everyone", ["112233445566778899"]))
    assert body["content"].startswith("<@&112233445566778899> ")
    assert body["allowed_mentions"] == {"parse": [], "roles": ["112233445566778899"]}  # @everyone 仍不會通知
    assert json.loads(notify.payload("x", ["not-a-number"]))["allowed_mentions"]["roles"] == []


def test_reminders_send_once_with_role_and_catch_up(browser, sent):
    from aams import reminders
    from aams.models import DiscordRole

    with SessionLocal() as db:
        role = db.scalar(select(DiscordRole).where(DiscordRole.role_id == "112233445566778899"))
    due = utcnow().replace(second=0) + timedelta(days=30)
    a_id = _mk_assignment("提醒測試 A", due, discord_role_id=role.id)  # 用系統預設 2,1 小時
    b_id = _mk_assignment("提醒測試 B", due + timedelta(hours=5))
    draft_id = _mk_assignment("提醒測試草稿", due)
    with SessionLocal() as db:
        db.get(Assignment, draft_id).status = "DRAFT"  # 草稿不提醒
        db.commit()

    def mine():
        return [t for ch, t in sent if "提醒測試" in t]

    assert not [m for m in reminders.run_once(now=due - timedelta(hours=3)) if "提醒測試" in m]
    reminders.run_once(now=due - timedelta(hours=2) + timedelta(minutes=1))
    assert len(mine()) == 1 and "提醒測試 A" in mine()[0] and "1 小時 59 分後截止" in mine()[0]
    assert sent.roles[-1] == ["112233445566778899"]
    assert all(ch == "student" for ch, _ in sent)
    reminders.run_once(now=due - timedelta(hours=1, minutes=30))  # 同一個時段不重發
    assert len(mine()) == 1
    reminders.run_once(now=due - timedelta(minutes=59))
    assert len(mine()) == 2 and "59 分鐘後截止" in mine()[1]
    assert not any("草稿" in t for t in mine())
    # B：伺服器「停機」到截止前 30 分鐘才檢查 → 只補發最近的一次（1 小時），2 小時那次略過
    b_due = due + timedelta(hours=5)
    sent.clear()
    reminders.run_once(now=b_due - timedelta(minutes=30))
    assert len(mine()) == 1 and "30 分鐘後截止" in mine()[0] and sent.roles[-1] is None
    with SessionLocal() as db:
        b = db.get(Assignment, b_id)
        states = {s.hours: s.state for s in reminders.schedule(db, b, b_due - timedelta(minutes=29))}
        assert states == {2.0: "SKIPPED", 1.0: "SENT"}
        # 截止時間延後 → due_version 改變 → 依新時間重新提醒
        b.due_at, b.due_version = b_due + timedelta(days=1), b.due_version + 1
        db.commit()
    sent.clear()
    reminders.run_once(now=b_due + timedelta(days=1) - timedelta(hours=1, minutes=50))
    assert len(mine()) == 1 and "提醒測試 B" in mine()[0]


def test_reminder_settings_form(browser):
    due = utcnow() + timedelta(days=40)
    a_id = _mk_assignment("提醒設定表單", due)
    ins = browser()
    ins.login("instructor2@demo.ais3.local")
    page = ins.get(f"/staff/assignments/{a_id}/edit")
    assert 'name="reminder_mode" value="default" checked' in page.text and "@學員" in page.text
    from aams.utils import to_local_input

    base = {"title": "提醒設定表單", "submission_mode": "INDIVIDUAL", "due_at": to_local_input(due),
            "allowed_types": ["pdf"], "max_total_mb": "20", "status": "PUBLISHED", "accepting_submissions": "1",
            "allow_late": "1"}
    r = ins.post(f"/staff/assignments/{a_id}/edit", {**base, "reminder_mode": "custom", "reminder_custom": "abc"})
    assert r.status_code == 400 and "截止前提醒" in r.text
    assert ins.post(f"/staff/assignments/{a_id}/edit", {**base, "reminder_mode": "custom",
                                                       "reminder_custom": "3, 0.5"}).status_code == 303
    with SessionLocal() as db:
        assert db.get(Assignment, a_id).reminder_hours == "3,0.5"
    page = ins.get(f"/staff/assignments/{a_id}")
    assert "Discord 截止提醒" in page.text and "截止前 30 分鐘" in page.text
    assert ins.post(f"/staff/assignments/{a_id}/edit", {**base, "reminder_mode": "off"}).status_code == 303
    from aams import reminders

    with SessionLocal() as db:
        a = db.get(Assignment, a_id)
        assert a.reminder_hours == "off" and reminders.effective_hours(a) == []
    assert "提醒設定表單" in ins.get("/staff/discord").text


def test_announcement_role_mention(browser, admin, sent):
    from aams.models import DiscordRole

    with SessionLocal() as db:
        role = db.scalar(select(DiscordRole).where(DiscordRole.role_id == "112233445566778899"))
    admin.get("/announcements/new")
    assert admin.post("/announcements/new", {"course_id": "global", "title": "集合提醒", "body": "9:00 大廳",
                                             "discord_role": str(role.id)}).status_code == 303
    assert sent.roles[-1] == ["112233445566778899"]
    assert admin.post("/announcements/new", {"course_id": "global", "title": "無提及", "body": "x"}).status_code == 303
    assert sent.roles[-1] is None
