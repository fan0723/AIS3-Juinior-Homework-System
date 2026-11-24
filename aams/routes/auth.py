"""登入、首次改密碼、MFA、個人設定（AUTH）。"""
from __future__ import annotations

import pyotp
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import (
    COOKIE,
    Ctx,
    RedirectTo,
    audit,
    client_ip,
    create_session,
    get_ctx,
    load_session,
    render,
    require_csrf,
    revoke_user_sessions,
    set_session_cookie,
)
from ..models import MfaFactor, User
from ..security import (
    account_fail_global,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    fmt_wait,
    login_fail_lock,
    login_ip_limiter,
    new_totp_secret,
    password_problems,
    totp_qr_svg_data_uri,
    totp_verify,
    verify_password,
)
from ..utils import canonical_email, clean_text, utcnow

router = APIRouter()

LOGIN_CSRF_COOKIE = "aams_login_csrf"


def _safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/"


def _login_page(request: Request, error: str = "", email: str = "", status: int = 200):
    from ..security import new_token

    token = request.cookies.get(LOGIN_CSRF_COOKIE) or new_token(16)
    resp = render(request, "login.html", None, status_code=status, csrf=token, error=error, email=email,
                  next=_safe_next(request.query_params.get("next")))
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True, samesite="strict", path="/login")
    return resp


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if load_session(request, db):
        return RedirectResponse("/", 303)
    return _login_page(request)


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf_token: str = Form(""),
          next: str = Form("/"), db: Session = Depends(get_db)):
    import hmac

    cookie_tok = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not cookie_tok or not hmac.compare_digest(cookie_tok, csrf_token):
        return _login_page(request, "表單已過期，請再試一次。", status=403)
    ip = client_ip(request)
    canon = canonical_email(email)[:254]
    generic = "帳號或密碼錯誤。"
    lock_key = f"{canon}|{ip}"
    wait = login_fail_lock.remaining(lock_key)
    if wait or not login_ip_limiter.hit(ip):
        audit(db, None, "auth.rate_limited", "email", canon[:64], {}, ip)
        db.commit()
        msg = (f"密碼錯誤次數過多，請 {fmt_wait(wait)}後再試；急用請找工作人員協助解除。" if wait
               else "嘗試次數過多，請稍後再試。")
        return _login_page(request, msg, email=email, status=429)
    user = db.scalar(select(User).where(User.canonical_email == canon))
    ok = verify_password(user.password_hash if user else None, password[:256])
    if not ok or user is None or user.status == "DISABLED":
        locked_for = login_fail_lock.fail(lock_key)
        if not account_fail_global.hit(canon):
            audit(db, user.id if user else None, "auth.anomaly_many_failures", "user", user.id if user else "", {}, ip)
        audit(db, user.id if user else None, "auth.login_failed", "user", user.id if user else "", {}, ip)
        if locked_for:
            audit(db, user.id if user else None, "auth.locked", "email", canon[:64], {"seconds": locked_for}, ip)
        db.commit()
        if locked_for:
            return _login_page(request, f"密碼錯誤次數過多，請 {fmt_wait(locked_for)}後再試；急用請找工作人員協助解除。",
                               email=email, status=429)
        return _login_page(request, generic, email=email, status=401)

    login_fail_lock.reset(lock_key)
    factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id, MfaFactor.enabled_at.is_not(None)))
    if user.status == "PENDING_PASSWORD_CHANGE":
        # 首次登入：先同意個資告知 → 再設定新密碼
        scope, dest = "RESTRICTED", ("/change-initial-password" if user.consented_at else "/consent")
    elif factor is not None:
        scope, dest = "MFA_PENDING", "/mfa/verify?next=" + _safe_next(next)
    else:
        scope, dest = "FULL", _safe_next(next)
    token = create_session(db, user, scope, request)
    audit(db, user.id, "auth.login", "user", user.id, {"scope": scope}, ip)
    db.commit()
    resp = RedirectResponse(dest, 303)
    set_session_cookie(resp, token)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/login")
    return resp


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    loaded = load_session(request, db)
    if loaded:
        user, sess = loaded
        require_csrf(sess, csrf_token)
        sess.revoked_at = utcnow()
        audit(db, user.id, "auth.logout", "user", user.id, {}, client_ip(request))
        db.commit()
    resp = RedirectResponse("/login?msg=logged_out", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---------------- 首次改密碼（受限 Session 白名單） ----------------
def _restricted(request: Request, db: Session):
    loaded = load_session(request, db)
    if not loaded:
        raise RedirectTo("/login")
    user, sess = loaded
    if sess.scope != "RESTRICTED" or user.status != "PENDING_PASSWORD_CHANGE":
        return None
    return user, sess


@router.get("/change-initial-password")
def initial_pw_form(request: Request, db: Session = Depends(get_db)):
    r = _restricted(request, db)
    if r is None:
        return RedirectResponse("/", 303)
    user, sess = r
    if user.consented_at is None:
        return RedirectResponse("/consent", 303)
    return render(request, "change_initial_password.html", None, csrf=sess.csrf_token, user=user, errors=[])


@router.post("/change-initial-password")
def initial_pw(request: Request, current_password: str = Form(""), new_password: str = Form(""),
               confirm_password: str = Form(""), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    r = _restricted(request, db)
    if r is None:
        return RedirectResponse("/", 303)
    user, sess = r
    require_csrf(sess, csrf_token)
    if user.consented_at is None:
        return RedirectResponse("/consent", 303)
    errors = []
    if not verify_password(user.password_hash, current_password[:256]):
        errors.append("目前（初始）密碼錯誤")
    if new_password != confirm_password:
        errors.append("兩次輸入的新密碼不一致")
    errors += password_problems(new_password, user.email, current_password)
    if errors:
        return render(request, "change_initial_password.html", None, csrf=sess.csrf_token, user=user,
                      errors=errors, status_code=400)
    # atomic activate：換 hash、狀態轉 ACTIVE、credential_version+1 → 所有舊 Session 失效
    user.password_hash = hash_password(new_password)
    user.status = "ACTIVE"
    user.activated_at = utcnow()
    user.credential_version += 1
    revoke_user_sessions(db, user.id)
    audit(db, user.id, "auth.activate", "user", user.id, {}, client_ip(request))
    db.commit()
    resp = RedirectResponse("/login?msg=pw_changed", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ---------------- 個資告知與同意（所有角色） ----------------
CONSENT_ITEMS = [
    ("read", "我已完整閱讀上方的個人資料蒐集告知。"),
    ("purpose", "我同意主辦單位在上述目的與範圍內蒐集、處理及利用我的個人資料。"),
    ("records", "我了解系統會記錄登入、IP 與操作紀錄，作為資訊安全稽核用途。"),
]


def _consent_session(request: Request, db: Session):
    """允許 RESTRICTED（首次改密碼前）與 FULL session；MFA 尚未驗證者先去驗證。"""
    loaded = load_session(request, db)
    if not loaded:
        raise RedirectTo("/login")
    user, sess = loaded
    if sess.scope == "MFA_PENDING":
        raise RedirectTo("/mfa/verify")
    return user, sess


def _after_consent(user: User, nxt: str) -> str:
    return "/change-initial-password" if user.status == "PENDING_PASSWORD_CHANGE" else _safe_next(nxt)


@router.get("/consent")
def consent_form(request: Request, db: Session = Depends(get_db)):
    user, sess = _consent_session(request, db)
    nxt = _safe_next(request.query_params.get("next"))
    if user.consented_at is not None:
        return RedirectResponse(_after_consent(user, nxt), 303)
    return render(request, "consent.html", None, csrf=sess.csrf_token, user=user, items=CONSENT_ITEMS, next=nxt,
                  pending=user.status == "PENDING_PASSWORD_CHANGE", error="")


@router.post("/consent")
def consent(request: Request, items: list[str] = Form([]), next: str = Form("/"), csrf_token: str = Form(""),
            db: Session = Depends(get_db)):
    user, sess = _consent_session(request, db)
    require_csrf(sess, csrf_token)
    if user.consented_at is None:
        if set(items) != {k for k, _ in CONSENT_ITEMS}:
            return render(request, "consent.html", None, csrf=sess.csrf_token, user=user, items=CONSENT_ITEMS,
                          next=_safe_next(next), pending=user.status == "PENDING_PASSWORD_CHANGE",
                          error="請逐項勾選同意後再繼續。", status_code=400)
        user.consented_at = utcnow()
        user.consent_ip = client_ip(request)[:64]
        audit(db, user.id, "privacy.consent", "user", user.id, {"items": sorted(items)}, client_ip(request))
        db.commit()
    return RedirectResponse(_after_consent(user, next), 303)


@router.get("/privacy")
def privacy(request: Request, db: Session = Depends(get_db)):
    """公開頁面：任何人（含未登入）都可以閱讀個資告知。"""
    loaded = load_session(request, db)
    viewer = loaded[0] if loaded and loaded[1].scope == "FULL" else None
    return render(request, "privacy.html", None, viewer=viewer)


# ---------------- MFA ----------------
@router.get("/mfa/verify")
def mfa_verify_form(request: Request, db: Session = Depends(get_db)):
    loaded = load_session(request, db)
    if not loaded or loaded[1].scope != "MFA_PENDING":
        return RedirectResponse("/login", 303)
    return render(request, "mfa_verify.html", None, csrf=loaded[1].csrf_token, error="",
                  next=_safe_next(request.query_params.get("next")))


@router.post("/mfa/verify")
def mfa_verify(request: Request, code: str = Form(""), csrf_token: str = Form(""), next: str = Form("/"),
               db: Session = Depends(get_db)):
    loaded = load_session(request, db)
    if not loaded or loaded[1].scope != "MFA_PENDING":
        return RedirectResponse("/login", 303)
    user, sess = loaded
    require_csrf(sess, csrf_token)
    factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == user.id, MfaFactor.enabled_at.is_not(None)))
    secret = decrypt_secret(factor.encrypted_secret) if factor else None
    step = totp_verify(secret, code, factor.last_used_step) if secret else None
    if step is None:
        sess.mfa_failures += 1
        audit(db, user.id, "auth.mfa_failed", "user", user.id, {}, client_ip(request))
        if sess.mfa_failures >= 5:
            sess.revoked_at = utcnow()
            db.commit()
            return RedirectResponse("/login", 303)
        db.commit()
        return render(request, "mfa_verify.html", None, csrf=sess.csrf_token, error="驗證碼錯誤。", next=_safe_next(next),
                      status_code=401)
    factor.last_used_step = step
    sess.revoked_at = utcnow()  # session rotation
    token = create_session(db, user, "FULL", request, mfa_verified=True)
    audit(db, user.id, "auth.mfa_ok", "user", user.id, {}, client_ip(request))
    db.commit()
    resp = RedirectResponse(_safe_next(next), 303)
    set_session_cookie(resp, token)
    return resp


@router.get("/mfa/setup")
def mfa_setup_form(request: Request, ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == ctx.user.id))
    if factor is not None and factor.enabled_at is not None:
        return RedirectResponse("/me", 303)
    if factor is None:
        factor = MfaFactor(user_id=ctx.user.id, encrypted_secret=encrypt_secret(new_totp_secret()))
        db.add(factor)
        db.commit()
    secret = decrypt_secret(factor.encrypted_secret)
    uri = pyotp.TOTP(secret).provisioning_uri(name=ctx.user.email, issuer_name="AIS3 Junior 作業繳交系統")
    return render(request, "mfa_setup.html", ctx, secret=secret, qr=totp_qr_svg_data_uri(uri),
                  required=request.query_params.get("required") == "1", error="")


@router.post("/mfa/setup")
def mfa_setup(request: Request, code: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    db = ctx.db
    factor = db.scalar(select(MfaFactor).where(MfaFactor.user_id == ctx.user.id))
    if factor is None or factor.enabled_at is not None:
        return RedirectResponse("/me", 303)
    secret = decrypt_secret(factor.encrypted_secret)
    step = totp_verify(secret, code, 0) if secret else None
    if step is None:
        uri = pyotp.TOTP(secret).provisioning_uri(name=ctx.user.email, issuer_name="AIS3 Junior 作業繳交系統")
        return render(request, "mfa_setup.html", ctx, secret=secret, qr=totp_qr_svg_data_uri(uri), required=False,
                      error="驗證碼錯誤，請確認手機時間正確後再試。", status_code=400)
    factor.enabled_at = utcnow()
    factor.last_used_step = step
    ctx.sess.mfa_verified = True
    audit(db, ctx.user.id, "auth.mfa_enrolled", "user", ctx.user.id, {}, ctx.ip)
    db.commit()
    return RedirectResponse("/me?msg=mfa_enabled", 303)


# ---------------- 個人設定 ----------------
@router.get("/me")
def me(request: Request, ctx: Ctx = Depends(get_ctx)):
    from .. import acl

    factor = ctx.db.scalar(select(MfaFactor).where(MfaFactor.user_id == ctx.user.id, MfaFactor.enabled_at.is_not(None)))
    group = acl.active_group_of(ctx.db, ctx.user.id) if ctx.is_student else None
    return render(request, "me.html", ctx, mfa=factor is not None, group=group, errors=[])


@router.post("/me/discord")
def me_discord(request: Request, discord_id: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    import re

    value = clean_text(discord_id, 40)
    if value and not re.fullmatch(r"[A-Za-z0-9_.#\-]{2,40}", value):
        raise HTTPException(400, "Discord ID 只允許英數字與 _ . # -，長度 2–40")
    ctx.user.discord_id = value
    audit(ctx.db, ctx.user.id, "user.update_discord", "user", ctx.user.id, {}, ctx.ip)
    ctx.db.commit()
    return RedirectResponse("/me?msg=saved", 303)


@router.post("/me/password")
def me_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                confirm_password: str = Form(""), csrf_token: str = Form(""), ctx: Ctx = Depends(get_ctx)):
    require_csrf(ctx.sess, csrf_token)
    errors = []
    if not verify_password(ctx.user.password_hash, current_password[:256]):
        errors.append("目前密碼錯誤")
    if new_password != confirm_password:
        errors.append("兩次輸入的新密碼不一致")
    errors += password_problems(new_password, ctx.user.email, current_password)
    if errors:
        factor = ctx.db.scalar(select(MfaFactor).where(MfaFactor.user_id == ctx.user.id, MfaFactor.enabled_at.is_not(None)))
        return render(request, "me.html", ctx, mfa=factor is not None, group=None, errors=errors, status_code=400)
    ctx.user.password_hash = hash_password(new_password)
    ctx.user.credential_version += 1
    revoke_user_sessions(ctx.db, ctx.user.id)
    audit(ctx.db, ctx.user.id, "auth.password_changed", "user", ctx.user.id, {}, ctx.ip)
    ctx.db.commit()
    resp = RedirectResponse("/login?msg=pw_changed", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp
