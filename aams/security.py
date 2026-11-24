"""Password hashing, tokens, TOTP encryption, signed preview tokens, rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
from collections import defaultdict, deque

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_ph = PasswordHasher()  # Argon2id
_DUMMY_HASH = _ph.hash("dummy-password-for-constant-time-checks")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str | None, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash or _DUMMY_HASH, pw) and pw_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _key(purpose: str) -> bytes:
    if not settings.secret_key:
        raise RuntimeError("AAMS_SECRET_KEY 未設定，請先執行 python manage.py init")
    return hashlib.sha256(f"{purpose}:{settings.secret_key}".encode()).digest()


# ---------- TOTP ----------
def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_key("totp-secret")))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(blob: str) -> str | None:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_verify(secret: str, code: str, last_step: int) -> int | None:
    """Return matched time-step (for replay protection) or None."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in (-1, 0, 1):
        step = now // 30 + offset
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * 30), code):
            return step
    return None


def totp_qr_svg_data_uri(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------- Signed short-lived tokens (preview origin) ----------
def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_key("preview"), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    expect = hmac.new(_key("preview"), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, mac):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------- Rate limiting (in-memory, per process) ----------
class RateLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit; return False if over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return False
            while q and now - q[0] > self.window:
                q.popleft()
            return len(q) >= self.limit

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class EscalatingLock:
    """密碼錯誤的漸進式鎖定（記憶體內；伺服器重啟即清除）。

    預設（登入）：連續錯 5 次 → 鎖 5 分鐘；解鎖後再錯 3 次 → 鎖 15 分鐘（之後每再錯 3 次都鎖 15 分鐘）。
    成功一次、或 1 小時內沒有再錯 → 全部歸零。管理員可以在系統頁手動解除。
    """

    def __init__(self, first: int = 5, first_s: int = 300, again: int = 3, again_s: int = 900, forget_s: int = 3600):
        self.first, self.first_s, self.again, self.again_s, self.forget_s = first, first_s, again, again_s, forget_s
        self._st: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _get(self, key: str, now: float) -> dict | None:
        st = self._st.get(key)
        if st and st["until"] <= now and now - st["last"] > self.forget_s:
            self._st.pop(key, None)
            return None
        return st

    def remaining(self, key: str) -> int:
        """還要鎖多少秒（0 = 沒鎖）。"""
        now = time.monotonic()
        with self._lock:
            st = self._get(key, now)
            return max(0, int(st["until"] - now + 0.999)) if st else 0

    def fail(self, key: str) -> int:
        """記一次失敗；回傳這次觸發的鎖定秒數（0 = 還沒鎖）。"""
        now = time.monotonic()
        with self._lock:
            st = self._get(key, now) or self._st.setdefault(key, {"n": 0, "tier": 0, "until": 0.0, "last": now, "at": 0.0})
            st["n"] += 1
            st["last"] = now
            need = self.first if st["tier"] == 0 else self.again
            if st["n"] >= need:
                secs = self.first_s if st["tier"] == 0 else self.again_s
                st.update(n=0, tier=st["tier"] + 1, until=now + secs, at=time.time())
                return secs
            return 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._st.pop(key, None)

    def locked(self) -> list[tuple[str, int, int]]:
        """目前鎖住的 [(key, 剩餘秒數, 第幾次鎖定)]，管理頁顯示用。"""
        now = time.monotonic()
        with self._lock:
            return [(k, int(st["until"] - now + 0.999), st["tier"]) for k, st in self._st.items() if st["until"] > now]


def fmt_wait(secs: int) -> str:
    return f"{max(1, (secs + 59) // 60)} 分鐘"


# 每個 IP 的登入嘗試；每個 (帳號, IP) 的密碼錯誤漸進式鎖定。
# 不做「帳號全域鎖定」，避免惡意學生低成本封鎖同學帳號（規格 §2.5）。
login_ip_limiter = RateLimiter(limit=settings.login_ip_limit, window_s=300)
login_fail_lock = EscalatingLock(first=5, first_s=300, again=3, again_s=900)
# 管理員修改 Discord webhook 時的密碼確認：錯 5 次鎖 15 分鐘；只能由「其他」管理員解除
webhook_pw_lock = EscalatingLock(first=5, first_s=900, again=3, again_s=900)
account_fail_global = RateLimiter(limit=50, window_s=900)  # 異常偵測用：只記錄稽核，不鎖帳號

COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123",
    "11111111", "00000000", "abc12345", "iloveyou", "admin123", "letmein1", "qwertyuiop",
    "ais3junior", "ais32026", "aaaaaaaa", "passw0rd", "1qaz2wsx", "zaq12wsx", "asdfghjkl",
}


def password_problems(new: str, email: str, current: str | None = None) -> list[str]:
    errs: list[str] = []
    if len(new) < 10:
        errs.append("新密碼至少 10 個字元")
    if len(new) > 128:
        errs.append("新密碼最多 128 個字元")
    low = new.lower()
    if low in COMMON_PASSWORDS or len(set(new)) <= 2:
        errs.append("新密碼過於常見或過於簡單")
    if new.isdigit():
        errs.append("新密碼不可只有數字（避免與生日／身分證格式相近）")
    local = email.split("@")[0].lower()
    if local and len(local) >= 4 and local in low:
        errs.append("新密碼不可包含帳號名稱")
    if current is not None and hmac.compare_digest(new, current):
        errs.append("新密碼不可與初始／目前密碼相同")
    return errs
