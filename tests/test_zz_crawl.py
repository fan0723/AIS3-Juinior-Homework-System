"""爬過每個角色可見的所有 GET 頁面，確認沒有 500（模板／程式錯誤）。"""
import re

import pyotp
import pytest

LINK = re.compile(r'href="(/[^"#?]*)(\?[^"#]*)?"')
SKIP = ("/static/", "/logout")


def crawl(b, start="/", limit=250):
    seen, queue, errors = set(), [start], []
    while queue and len(seen) < limit:
        url = queue.pop(0)
        if url in seen or url.startswith(SKIP):
            continue
        seen.add(url)
        r = b.c.get(url, follow_redirects=True)
        if r.status_code >= 500:
            errors.append((url, r.status_code))
            continue
        if "text/html" in r.headers.get("content-type", ""):
            for m in LINK.finditer(r.text):
                path = m.group(1) + (m.group(2) or "")
                if "/download" in path:
                    continue
                if path not in seen:
                    queue.append(path)
    return seen, errors


@pytest.mark.parametrize("email", [
    "s021@demo.ais3.local", "ta021@demo.ais3.local", "ta081@demo.ais3.local",
    "instructor2@demo.ais3.local", "instructor1@demo.ais3.local",
])
def test_crawl_no_server_errors(browser, email):
    b = browser()
    b.login(email)
    seen, errors = crawl(b)
    assert len(seen) > 3
    assert errors == []


def test_crawl_admin(browser):
    b = browser()
    b.login("staff2@demo.ais3.local")
    page = b.get("/mfa/setup?required=1")
    secret = re.search(r'手動金鑰：<span class="mono">([A-Z2-7]+)</span>', page.text).group(1)
    b.post("/mfa/setup", {"code": pyotp.TOTP(secret).now()})
    seen, errors = crawl(b, "/admin", limit=400)
    assert "/admin/exports" in seen and errors == []
    # 匯出 CSV / ZIP 可運作
    b.get("/admin/exports")
    assert b.post("/admin/exports/submissions").status_code == 200
    r = b.post("/admin/exports/users", {"include_phone": ""})
    assert r.status_code == 200 and "phone" not in r.text.splitlines()[0]
