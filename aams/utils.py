from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def fmt_dt(dt: datetime | None, with_seconds: bool = True) -> str:
    if dt is None:
        return "—"
    local = dt.replace(tzinfo=timezone.utc).astimezone(TAIPEI)
    return local.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def to_local_input(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).astimezone(TAIPEI).strftime("%Y-%m-%dT%H:%M")


def parse_local_input(value: str) -> datetime:
    """Parse <input type=datetime-local> (Asia/Taipei) -> naive UTC."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(value, fmt).replace(tzinfo=TAIPEI)
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    raise ValueError("時間格式錯誤")


def canonical_email(email: str) -> str:
    """去頭尾空白 + NFKC + 小寫。刻意不做 Gmail 句點／+tag 移除。"""
    return unicodedata.normalize("NFKC", (email or "").strip()).lower()


EMAIL_RE = re.compile(r"^[^@\s<>\"'`]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or "")) and len(email) <= 254


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value: str | None, max_len: int, multiline: bool = False) -> str:
    value = unicodedata.normalize("NFC", value or "")
    if multiline:
        value = value.replace("\r\n", "\n")
        value = "".join(ch for ch in value if ch in "\n\t" or not _CTRL.match(ch))
    else:
        value = _CTRL.sub("", value)
    return value.strip()[:max_len]


def sanitize_filename(name: str, max_len: int = 150) -> str:
    """供顯示與 ZIP 內檔名使用；實際儲存路徑永遠用隨機 key。"""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = _CTRL.sub("", name)
    name = re.sub(r'[<>:"|?*]', "_", name).strip(" .")
    if not name or name in (".", ".."):
        name = "file"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: max_len - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:max_len]
    return name


def csv_safe(value) -> str:
    """CSV / Formula Injection 防護（OWASP）。"""
    if value is None:
        return ""
    s = str(value)
    if s and (s[0] in "=+-@\t\r" or s.lstrip()[:1] in ("=", "+", "-", "@")):
        # 純數字（含負數）不需處理
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return s
        return "'" + s
    return s


TWO = Decimal("0.01")


def parse_score(value: str | None) -> Decimal | None:
    """'' -> None（未評分）；'0' -> Decimal(0)（真實零分）。"""
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("分數格式錯誤") from exc
    if not d.is_finite():
        raise ValueError("分數格式錯誤")
    try:
        return d.quantize(TWO, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # 位數太多，例如 1e30
        raise ValueError("分數格式錯誤") from exc


def fmt_score(d: Decimal | None) -> str:
    if d is None:
        return "—"
    d = Decimal(d).quantize(TWO)
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s
