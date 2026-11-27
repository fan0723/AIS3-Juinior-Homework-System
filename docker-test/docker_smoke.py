"""Docker 測試站端到端冒煙測試：對 https://localhost 跑，在專案根目錄執行。

會建立一份提交、發布成績、替 staff1 綁定 TOTP。跑完請用 reset-demo 重置（見 docker_run_readme.md 第 5-1 節）。
"""
import re
import subprocess
import sys
import time

import httpx


def make_text_pdf(lines):
    """同 tests/test_features.py：含真正文字的最小 PDF。"""
    stream = "BT /F1 10 Tf 40 800 Td 12 TL " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


BASE = "https://localhost"
DC = ["docker", "compose", "-p", "aams-test", "--env-file", "docker-test/.env", "-f", "docker-compose.yml"]
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def sql(q):
    out = subprocess.run(DC + ["exec", "-T", "db", "psql", "-U", "aams", "-d", "aams", "-tAc", q],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return out.strip()


def manage(*args):
    return subprocess.run(DC + ["exec", "-T", "app", "python", "manage.py", *args], capture_output=True, text=True, encoding="utf-8")


def client():
    return httpx.Client(base_url=BASE, verify=False, follow_redirects=False, timeout=30)


def login(email, pw="Ais3-Demo-2026"):
    c = client()
    page = c.get("/login")
    r = c.post("/login", data={"email": email, "password": pw, "csrf_token": CSRF.search(page.text).group(1), "next": "/"})
    assert r.status_code == 303, (email, r.status_code)
    if c.get("/").headers.get("location", "").startswith("/consent"):
        page = c.get("/consent")
        c.post("/consent", data={"csrf_token": CSRF.search(page.text).group(1), "next": "/",
                                 "items": ["read", "purpose", "records"]})
    return c


def csrf(c, url):
    return CSRF.search(c.get(url).text).group(1)


# ---------------------------------------------------------------- student upload
aid = int(sql("select id from assignments where title like 'Lab 1%'"))
s = login("s011@demo.ais3.local")
page = s.get(f"/assignments/{aid}")
idem = re.search(r'name="idempotency_key" value="([^"]+)"', page.text).group(1)
pdf = make_text_pdf(["docker smoke test report"] * 5)
r = s.post(f"/assignments/{aid}/submit", data={"csrf_token": CSRF.search(page.text).group(1), "idempotency_key": idem},
           files=[("files", ("report.pdf", pdf, "application/pdf"))])
check("學員上傳 PDF 被收件", r.status_code == 303 and "/receipt" in r.headers.get("location", ""), str(r.status_code))
receipt = s.get(r.headers["location"])
check("收據頁顯示 SHA-256", "SHA-256" in receipt.text or "sha256" in receipt.text.lower())

art_id = int(sql("select a.id from submission_artifacts a join submission_revisions r on r.id=a.revision_id "
                 "join submissions s on s.id=r.submission_id where s.assignment_id=%d order by a.id desc limit 1" % aid))
state = ""
for _ in range(60):
    state = sql(f"select inspection_state from submission_artifacts where id={art_id}")
    if state != "PENDING":
        break
    time.sleep(1)
check("inspector 容器（spool）檢查通過", state == "ACCEPTED", state)

page = s.get(f"/assignments/{aid}")
idem = re.search(r'name="idempotency_key" value="([^"]+)"', page.text).group(1)
r = s.post(f"/assignments/{aid}/submit", data={"csrf_token": CSRF.search(page.text).group(1), "idempotency_key": idem},
           files=[("files", ("fake.pdf", b"just text, not a pdf" * 10, "application/pdf"))])
check("偽造 PDF 被拒絕", r.status_code == 400, str(r.status_code))

# ---------------------------------------------------------------- preview origin
pv = s.get(f"/artifacts/{art_id}/preview")
m = re.search(r'src="(https://[^"]+/p/[^"]+)"', pv.text)
check("預覽頁產生 :8443 簽章連結", bool(m) and ":8443/p/" in (m.group(1) if m else ""), m.group(1)[:40] if m else "")
if m:
    pr = httpx.get(m.group(1), verify=False, timeout=30)
    check("預覽來源回傳 PDF", pr.status_code == 200 and pr.headers.get("content-type", "").startswith("application/pdf"),
          f"{pr.status_code} {pr.headers.get('content-type')}")

# ---------------------------------------------------------------- grading + release
rev_id = int(sql(f"select revision_id from submission_artifacts where id={art_id}"))
sub_id = int(sql(f"select submission_id from submission_revisions where id={rev_id}"))
qids = [int(x) for x in sql(f"select id from assignment_questions where assignment_id={aid} order by ordinal").split()]
ins = login("instructor1@demo.ais3.local")
ok = True
for q in qids:
    tok = csrf(ins, f"/staff/submissions/{sub_id}?rev={rev_id}")
    r = ins.post(f"/staff/revisions/{rev_id}/grade-item", data={"csrf_token": tok, "question_id": q, "score": "10",
                                                                  "feedback": "ok", "expected_version": 0})
    ok &= r.status_code == 303 and "conflict" not in r.headers.get("location", "")
check("講師逐題評分", ok)
tok = csrf(ins, f"/staff/assignments/{aid}/release")
r = ins.post(f"/staff/assignments/{aid}/release", data={"csrf_token": tok, "confirm": "1", "reason": ""})
check("講師統一發布成績", r.status_code == 303 and "released" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
page = s.get(f"/assignments/{aid}")
check("學員看得到發布的成績", int(sql(f"select count(*) from released_grades where submission_id={sub_id}")) == 1
      and "30" in page.text)

# ---------------------------------------------------------------- admin TOTP
a = login("staff1@demo.ais3.local")
r = a.get("/admin")
check("管理員未綁 TOTP 被導去綁定", r.status_code == 303 and "/mfa/setup" in r.headers.get("location", ""))
tok = csrf(a, "/mfa/setup?required=1")
code = manage("totp-code", "staff1@demo.ais3.local").stdout.strip().splitlines()[-1]
check("totp-code 指令輸出 6 位數", bool(re.fullmatch(r"\d{6}", code)), code)
r = a.post("/mfa/setup", data={"csrf_token": tok, "code": code})
check("綁定 TOTP", r.status_code == 303 and "mfa_enabled" in r.headers.get("location", ""))
check("綁定後進得了管理後台", a.get("/admin").status_code == 200)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
