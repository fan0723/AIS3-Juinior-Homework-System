"""Request context: session loading, role gates, CSRF, rendering, audit."""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ROLE_LABELS, AuditEvent, AuthSession, MfaFactor, Notification, User
from .security import new_token, token_hash
from .utils import fmt_bytes, fmt_dt, fmt_score, to_local_input, utcnow

COOKIE = "aams_session"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LOCAL_BRAND_DIR = Path(__file__).parent / "static" / "local"


def has_local_brand_asset(filename: str) -> bool:
    return (LOCAL_BRAND_DIR / filename).is_file()


templates.env.globals.update(
    fmt_dt=fmt_dt, fmt_bytes=fmt_bytes, fmt_score=fmt_score, to_local_input=to_local_input,
    ROLE_LABELS=ROLE_LABELS, settings=settings, has_local_brand_asset=has_local_brand_asset,
)


def _default_reminder_hours() -> str:
    from . import sysconfig

    return sysconfig.reminder_hours_raw()


templates.env.globals["default_reminder_hours"] = _default_reminder_hours

MESSAGES = {
    "saved": "已儲存。",
    "created": "已建立。",
    "deleted": "已刪除。",
    "pw_changed": "密碼已更新，請以新密碼重新登入。",
    "logged_out": "已登出。",
    "mfa_enabled": "已啟用兩步驟驗證（TOTP）。",
    "submitted": "已收件！請保存下方收據。檔案仍需通過安全檢查。",
    "released": "成績已統一發布。",
    "revoked": "已撤銷。",
    "imported": "匯入完成。",
    "reset": "已重設密碼，請將臨時密碼以安全方式交給本人。",
    "conflict": "儲存衝突：其他人已先修改此項目，請重新整理後再試。",
    "appeal_created": "已送出成績申覆，講師處理後會在此頁回覆。",
    "discord_ok": "Discord 測試訊息已送出。",
    "discord_fail": "Discord 測試訊息送出失敗，請檢查 Webhook 設定與伺服器對外連線（discord.com）。",
    "webhook_saved": "Webhook 已儲存，測試訊息也成功送出。",
    "webhook_saved_unverified": "Webhook 已儲存，但測試訊息送出失敗：請確認網址正確、Webhook 沒被刪除，以及伺服器能連到 discord.com。",
    "webhook_cleared": "已停用這個頻道的 Discord 通知。",
    "unlocked": "已解除鎖定，對方可以立即再試。",
    "reminder_saved": "截止提醒系統預設已更新，所有「使用系統預設」的作業會照新設定提醒。",
}


class RedirectTo(Exception):
    def __init__(self, url: str):
        self.url = url


@dataclass
class Ctx:
    request: Request
    db: Session
    user: User
    sess: AuthSession
    roles: set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return "ADMIN" in self.roles

    @property
    def is_instructor(self) -> bool:
        return "INSTRUCTOR" in self.roles

    @property
    def is_ta(self) -> bool:
        return "TA" in self.roles

    @property
    def is_student(self) -> bool:
        return "STUDENT" in self.roles

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & {"TA", "INSTRUCTOR", "ADMIN"})

    @property
    def ip(self) -> str:
        return client_ip(self.request)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def create_session(db: Session, user: User, scope: str, request: Request, mfa_verified: bool = False) -> str:
    token = new_token()
    db.add(
        AuthSession(
            id_hash=token_hash(token),
            user_id=user.id,
            scope=scope,
            csrf_token=new_token(24),
            mfa_verified=mfa_verified,
            credential_version=user.credential_version,
            expires_at=utcnow() + timedelta(hours=settings.session_hours if scope == "FULL" else 1),
            ip=client_ip(request)[:64],
        )
    )
    return token


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="lax",
        max_age=settings.session_hours * 3600, path="/",
    )


def revoke_user_sessions(db: Session, user_id: int) -> None:
    now = utcnow()
    for s in db.scalars(select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))):
        s.revoked_at = now


def load_session(request: Request, db: Session) -> tuple[User, AuthSession] | None:
    tok = request.cookies.get(COOKIE)
    if not tok or len(tok) > 200:
        return None
    sess = db.get(AuthSession, token_hash(tok))
    if sess is None or sess.revoked_at is not None or sess.expires_at < utcnow():
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.status == "DISABLED" or user.credential_version != sess.credential_version:
        return None
    return user, sess


def get_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    loaded = load_session(request, db)
    if not loaded:
        raise RedirectTo("/login?next=" + quote(request.url.path))
    user, sess = loaded
    if sess.scope == "RESTRICTED" or user.status == "PENDING_PASSWORD_CHANGE":
        raise RedirectTo("/change-initial-password")
    if sess.scope == "MFA_PENDING":
        raise RedirectTo("/mfa/verify")
    if sess.scope != "FULL" or user.status != "ACTIVE":
        raise RedirectTo("/login")
    if user.consented_at is None:  # 所有角色都必須先同意個資告知
        raise RedirectTo("/consent?next=" + quote(request.url.path))
    ctx = Ctx(request=request, db=db, user=user, sess=sess, roles=user.role_set)
    # 錯誤頁用的純值快照（交易 rollback 後 ORM 物件可能已失效）
    from types import SimpleNamespace

    request.state.view = SimpleNamespace(
        me=SimpleNamespace(name=user.name, role_set=set(ctx.roles)),
        ctx=SimpleNamespace(is_staff=ctx.is_staff, is_admin=ctx.is_admin),
        csrf=sess.csrf_token,
    )
    return ctx


def admin_ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(403, "僅限管理員")
    if not ctx.sess.mfa_verified:
        factor = ctx.db.scalar(select(MfaFactor).where(MfaFactor.user_id == ctx.user.id))
        if factor is None or factor.enabled_at is None:
            raise RedirectTo("/mfa/setup?required=1")
        raise HTTPException(403, "需要兩步驟驗證")
    return ctx


def staff_ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_staff:
        raise HTTPException(403, "僅限工作人員")
    return ctx


def require_csrf(sess: AuthSession, token: str | None) -> None:
    if not token or not hmac.compare_digest(sess.csrf_token, token):
        raise HTTPException(403, "CSRF 驗證失敗，請重新整理頁面後再試")


def audit(db: Session, actor_id: int | None, action: str, target_type: str = "", target_id="",
          detail: dict | None = None, ip: str = "") -> None:
    db.add(
        AuditEvent(
            actor_id=actor_id, action=action, target_type=target_type, target_id=str(target_id),
            detail=json.dumps(detail or {}, ensure_ascii=False, default=str)[:4000], ip=ip[:64],
        )
    )


def preview_base(request: Request) -> str:
    if settings.preview_url:
        return settings.preview_url
    host = request.url.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{request.url.scheme}://{host}:{settings.preview_port}"


def main_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def render(request: Request, name: str, ctx: Ctx | None = None, status_code: int = 200, **kw):
    unread = ann_unread = 0
    if ctx is not None:
        from .routes.announcements import unread_count

        unread = ctx.db.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.recipient_id == ctx.user.id, Notification.read_at.is_(None)
            )
        ) or 0
        ann_unread = unread_count(ctx.db, ctx.user)
    msg_key = request.query_params.get("msg", "")
    data = {
        "request": request,
        "ctx": ctx,
        "me": ctx.user if ctx else None,
        "csrf": ctx.sess.csrf_token if ctx else kw.pop("csrf", ""),
        "unread": unread,
        "ann_unread": ann_unread,
        "flash": MESSAGES.get(msg_key, ""),
        "flash_kind": "warn" if msg_key in ("conflict", "discord_fail", "webhook_saved_unverified") else "ok",
    }
    data.update(kw)
    return templates.TemplateResponse(request, name, data, status_code=status_code)
