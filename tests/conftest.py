import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="aams-test-"))
os.environ["AAMS_DATA_DIR"] = str(_TMP)
os.environ["AAMS_DATABASE_URL"] = "sqlite:///" + (_TMP / "test.db").as_posix()
os.environ["AAMS_SECRET_KEY"] = "test-secret-key-" + "x" * 40
os.environ["AAMS_RUN_WORKER"] = "false"
os.environ["AAMS_COOKIE_SECURE"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from aams.main import app, preview_app  # noqa: E402

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(scope="session", autouse=True)
def seeded():
    import argparse

    import manage

    from aams.db import init_db

    init_db()
    manage.cmd_seed_demo(argparse.Namespace(active=True))
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


def csrf_of(html: str) -> str:
    m = CSRF_RE.search(html)
    assert m, "no csrf token in page"
    return m.group(1)


class Browser:
    def __init__(self):
        self.c = TestClient(app, base_url="http://testserver")
        self.c.__enter__()
        self.csrf = ""

    def login(self, email, password="Ais3-Demo-2026", follow=True):
        from aams.security import login_ip_limiter
        login_ip_limiter._hits.clear()
        r = self.c.get("/login")
        tok = csrf_of(r.text)
        r = self.c.post("/login", data={"email": email, "password": password, "csrf_token": tok, "next": "/"},
                        follow_redirects=follow)
        if follow and r.status_code == 200 and r.url.path == "/consent":
            r = self.consent()
        if r.status_code == 200 and "csrf_token" in r.text:
            self.csrf = csrf_of(r.text)
        return r

    def consent(self):
        """勾選個資告知全部項目（測試用）。"""
        from aams.routes.auth import CONSENT_ITEMS

        page = self.c.get("/consent")
        return self.c.post("/consent", data={"csrf_token": csrf_of(page.text), "next": "/",
                                             "items": [k for k, _ in CONSENT_ITEMS]}, follow_redirects=True)

    def get(self, url, **kw):
        r = self.c.get(url, **kw)
        if r.status_code == 200 and 'name="csrf_token"' in r.text:
            self.csrf = csrf_of(r.text)
        return r

    def post(self, url, data=None, files=None, follow=False):
        data = dict(data or {})
        data.setdefault("csrf_token", self.csrf)
        return self.c.post(url, data=data, files=files, follow_redirects=follow)


@pytest.fixture
def browser():
    return Browser


@pytest.fixture
def preview_client():
    return TestClient(preview_app)
