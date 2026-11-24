"""Discord 頻道通知（Webhook，只做頻道訊息、不做私訊機器人）。

- 兩個頻道：staff（工作人員警示、成績申覆）與 student（公告、成績發布）。
- Webhook 留空 = 不傳送（內網測試預設）。
- 背景執行緒送出，不會拖慢網頁回應；失敗只記錄，不影響主流程。
- 訊息絕不包含分數、檔案內容或個資。
- 預設停用所有 @mention（避免 @everyone 被濫用）；只有呼叫端明確指定的身分組（截止提醒、公告）會被 @。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

from .config import settings
from .utils import utcnow

log = logging.getLogger("aams.notify")

CHANNELS = ("staff", "student")

_q: queue.Queue = queue.Queue(maxsize=500)
_thread: threading.Thread | None = None
_lock = threading.Lock()
status = {"sent": 0, "failed": 0, "dropped": 0, "last_ok_at": None, "last_error": "", "last_error_at": None}


def webhook_url(channel: str) -> str:
    """.env 有填用 .env，否則用網頁設定（見 sysconfig）；格式不合法 → 空字串（不傳送）。"""
    from . import sysconfig

    return sysconfig.webhook(channel)


def configured(channel: str) -> bool:
    return bool(webhook_url(channel))


def invalid_config() -> list[str]:
    """.env 填了但格式不對的 webhook（系統頁顯示用）。"""
    from . import sysconfig

    out = []
    for ch, url in (("staff", settings.discord_staff_webhook), ("student", settings.discord_student_webhook)):
        if url and not sysconfig.valid_webhook(url):
            out.append(ch)
    return out


def valid_role_id(role_id: str | None) -> bool:
    return bool(role_id) and role_id.isdigit() and 15 <= len(role_id) <= 21


def send(channel: str, text: str, role_ids: list[str] | None = None) -> bool:
    """排入佇列；回傳 True 表示已排入（未設定 webhook 時回傳 False）。role_ids：要 @ 的身分組 ID。"""
    if channel not in CHANNELS or not configured(channel):
        return False
    _ensure_thread()
    try:
        _q.put_nowait((channel, text, [r for r in (role_ids or []) if valid_role_id(r)]))
        return True
    except queue.Full:
        status["dropped"] += 1
        return False


def payload(text: str, role_ids: list[str] | None = None) -> bytes:
    roles = [r for r in (role_ids or []) if valid_role_id(r)]
    prefix = "".join(f"<@&{r}> " for r in roles)
    return json.dumps({
        "content": (prefix + text)[:1900],
        # 只允許明確指定的身分組被 @；@everyone／@here／其他身分組／個人一律不通知
        "allowed_mentions": {"parse": [], "roles": roles},
        "username": "AIS3 Junior 作業繳交系統",
    }).encode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟隨重新導向：避免被導到 discord.com 以外的位址（SSRF）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post(url: str, body: bytes) -> tuple[int, float]:
    """回傳 (HTTP 狀態碼, 若 429 則建議等待秒數)。"""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "AAMS-notify/1.0"})
    try:
        with _opener.open(req, timeout=10) as resp:  # noqa: S310 - URL 已限制為 discord.com webhook
            return resp.status, 0.0
    except urllib.error.HTTPError as e:
        retry = 0.0
        if e.code == 429:
            try:
                retry = float(json.loads(e.read().decode() or "{}").get("retry_after", 2))
            except Exception:  # noqa: BLE001
                retry = 2.0
        return e.code, retry


def deliver(channel: str, text: str, role_ids: list[str] | None = None) -> bool:
    return deliver_url(webhook_url(channel), text, role_ids, label=channel)


def deliver_url(url: str, text: str, role_ids: list[str] | None = None, label: str = "") -> bool:
    """同步送到指定 webhook（只接受合法 Discord webhook）。系統頁「更換 webhook」時通知舊頻道用。"""
    from . import sysconfig

    if not sysconfig.valid_webhook(url):
        return False
    body = payload(text, role_ids)
    for _attempt in range(3):
        try:
            code, retry = _post(url, body)
        except Exception as exc:  # noqa: BLE001 - 網路錯誤
            code, retry = 0, 0.0
            status["last_error"] = f"{type(exc).__name__}"
        if 200 <= code < 300:
            status["sent"] += 1
            status["last_ok_at"] = utcnow()
            return True
        if code == 429:
            time.sleep(min(retry, 10))
            continue
        if code:
            status["last_error"] = f"HTTP {code}"
        break
    status["failed"] += 1
    status["last_error_at"] = utcnow()
    log.warning("discord %s delivery failed: %s", label, status["last_error"])
    return False


def _worker() -> None:
    while True:
        channel, text, roles = _q.get()
        try:
            deliver(channel, text, roles)
        finally:
            _q.task_done()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="aams-discord", daemon=True)
            _thread.start()
