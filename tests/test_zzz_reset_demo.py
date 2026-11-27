"""manage.py reset-demo：會清空整個測試資料庫，所以放在最後執行（檔名排序）。"""
import argparse

import pytest
from sqlalchemy import func, select

import manage
from aams.db import SessionLocal
from aams.models import Assignment, MfaFactor, Submission, User, UserRole
from aams.security import hash_password
from aams.storage import ACCEPTED


def _reset(force=False):
    manage.cmd_reset_demo(argparse.Namespace(yes=True, force=force))


def test_totp_code_explains_undecryptable_secret():
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.canonical_email == "staff1@demo.ais3.local"))
        db.query(MfaFactor).filter(MfaFactor.user_id == u.id).delete()
        db.add(MfaFactor(user_id=u.id, encrypted_secret="encrypted-with-an-old-secret"))
        db.commit()
    with pytest.raises(SystemExit, match="AAMS_SECRET_KEY 已更換"):
        manage.cmd_totp_code(argparse.Namespace(email="staff1@demo.ais3.local"))


def test_reset_demo_refuses_real_accounts_then_wipes_with_force():
    with SessionLocal() as db:
        u = User(email="real@school.example", canonical_email="real@school.example", name="Real",
                 password_hash=hash_password("x" * 12), status="ACTIVE")
        u.roles.append(UserRole(role="STUDENT"))
        db.add(u)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Submission)) > 0  # 前面的測試留下的提交
    (ACCEPTED / "leftover").write_bytes(b"x")

    with pytest.raises(SystemExit, match="非示範帳號"):
        _reset()
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.canonical_email == "real@school.example")) is not None

    _reset(force=True)
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.canonical_email == "real@school.example")) is None
        assert db.scalar(select(func.count()).select_from(Submission)) == 0
        assert db.scalar(select(func.count()).select_from(MfaFactor)) == 0  # TOTP 綁定全部清掉，可重新綁定
        assert db.scalar(select(func.count()).select_from(User)) == 102
        assert db.scalar(select(func.count()).select_from(Assignment)) == 3
        assert db.scalar(select(User).where(User.canonical_email == "s011@demo.ais3.local")).status == "ACTIVE"
    assert not (ACCEPTED / "leftover").exists()
