"""網頁可修改的系統設定（管理員／staff 限定）。

規則：
- `.env` 有填 → 以 `.env` 為準，網頁顯示「由 .env 設定」且不能修改。
- `.env` 沒填 → 使用網頁設定（存在 system_settings 資料表）。
- Discord webhook 是機密：用 AAMS_SECRET_KEY 衍生的金鑰加密後存放，存入後網頁永遠不再顯示完整網址，
  只顯示 Webhook ID（不是機密）。只拿到資料庫或備份檔、沒有 .env 的人解不開。
- 讀取有 15 秒快取（多個 worker 行程時，其他行程最慢 15 秒後生效）；本行程修改後立即生效。
"""
from __future__ import annotations

import re
import threading
import time

from sqlalchemy import select

from .config import settings
from .utils import utcnow

DEFAULT_REMINDER_HOURS = "2,1"
WEBHOOK_RE = re.compile(r"https://(?:discord\.com|discordapp\.com)/api/webhooks/(\d{15,21})/([A-Za-z0-9_-]{30,100})")
_KEYS = {"staff": "discord_staff_webhook", "student": "discord_student_webhook"}
REMINDER_KEY = "reminder_hours"

_cache: dict[str, str] = {}
_cache_at = 0.0
_lock = threading.Lock()
TTL = 15.0


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.fullmatch(url or ""))


def webhook_id(url: str) -> str:
    m = WEBHOOK_RE.fullmatch(url or "")
    return m.group(1) if m else ""


def _env_webhook(channel: str) -> str:
    return settings.discord_staff_webhook if channel == "staff" else settings.discord_student_webhook


def _load() -> dict[str, str]:
    global _cache, _cache_at
    with _lock:
        if time.monotonic() - _cache_at < TTL:
            return _cache
    from .db import SessionLocal
    from .models import SystemSetting

    try:
        with SessionLocal() as db:
            data = {r.key: r.value for r in db.scalars(select(SystemSetting))}
    except Exception:  # noqa: BLE001 - 資料表尚未建立等情況：視為沒有網頁設定
        data = {}
    with _lock:
        _cache, _cache_at = data, time.monotonic()
    return data


def invalidate() -> None:
    global _cache_at
    with _lock:
        _cache_at = 0.0


# ---------------------------------------------------------------- Discord webhook
def env_locked(channel: str) -> bool:
    return bool(_env_webhook(channel))


def webhook(channel: str) -> str:
    """實際使用的 webhook（不合法 → 空字串 = 不傳送）。"""
    if channel not in _KEYS:
        return ""
    if env_locked(channel):
        url = _env_webhook(channel)
    else:
        blob = _load().get(_KEYS[channel], "")
        if not blob:
            return ""
        from .security import decrypt_secret

        url = decrypt_secret(blob) or ""  # AAMS_SECRET_KEY 換過 → 解不開 → 視為未設定
    return url if valid_webhook(url) else ""


def webhook_source(channel: str) -> str:
    """env | web | none"""
    if env_locked(channel):
        return "env"
    return "web" if _load().get(_KEYS.get(channel, ""), "") else "none"


def web_undecryptable(channel: str) -> bool:
    """網頁設定存在但解不開（多半是 AAMS_SECRET_KEY 被換掉）。"""
    if env_locked(channel):
        return False
    blob = _load().get(_KEYS[channel], "")
    if not blob:
        return False
    from .security import decrypt_secret

    return decrypt_secret(blob) is None


def set_webhook(db, channel: str, url: str | None, user_id: int) -> None:
    """url=None 表示清除。呼叫端負責權限、密碼確認、稽核與 commit。"""
    from .security import encrypt_secret

    _put(db, _KEYS[channel], encrypt_secret(url) if url else None, user_id)


# ---------------------------------------------------------------- 截止提醒預設
def reminder_locked() -> bool:
    return bool(settings.reminder_hours_env)


def reminder_hours_raw() -> str:
    if reminder_locked():
        return settings.reminder_hours_env
    return _load().get(REMINDER_KEY, "") or DEFAULT_REMINDER_HOURS


def reminder_source() -> str:
    if reminder_locked():
        return "env"
    return "web" if _load().get(REMINDER_KEY, "") else "default"


def set_reminder_hours(db, raw: str | None, user_id: int) -> None:
    """raw=None 表示恢復內建值 2,1。"""
    _put(db, REMINDER_KEY, raw, user_id)


def _put(db, key: str, value: str | None, user_id: int) -> None:
    from .models import SystemSetting

    row = db.get(SystemSetting, key)
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(SystemSetting(key=key, value=value, updated_by=user_id, updated_at=utcnow()))
    else:
        row.value, row.updated_by, row.updated_at = value, user_id, utcnow()
    db.flush()
    invalidate()


def updated_info(db, channel_or_key: str):
    from .models import SystemSetting

    return db.get(SystemSetting, _KEYS.get(channel_or_key, channel_or_key))
