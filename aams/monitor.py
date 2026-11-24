"""監控與警示：定時檢查，異常時發到 Discord staff 頻道（同一種警示在一段時間內只發一次）。

檢查項目：磁碟空間、檔案檢查佇列卡住、伺服器錯誤暴增、登入失敗暴增。
（備份相關警示等自動備份完成後再加。）
"""
from __future__ import annotations

import collections
import logging
import shutil
import threading
import time
from datetime import timedelta

from sqlalchemy import func, select

from . import notify
from .config import settings
from .db import SessionLocal
from .models import Artifact, AuditEvent, Revision
from .utils import fmt_dt, utcnow

log = logging.getLogger("aams.monitor")

_errors: collections.deque = collections.deque(maxlen=1000)  # 伺服器錯誤時間戳（time.time()）
_last_sent: dict[str, float] = {}
recent: collections.deque = collections.deque(maxlen=50)  # 系統頁顯示最近的警示
_lock = threading.Lock()
_last_run = 0.0


def record_error() -> None:
    _errors.append(time.time())


def errors_in_window(seconds: int = 300) -> int:
    cutoff = time.time() - seconds
    return sum(1 for t in list(_errors) if t >= cutoff)


def check(db) -> list[tuple[str, str]]:
    """回傳目前觸發的 (key, 訊息)。key 用來避免重複發送。"""
    alerts: list[tuple[str, str]] = []
    now = utcnow()

    # 1) 磁碟空間
    try:
        du = shutil.disk_usage(settings.data_dir)
        free_pct = du.free / du.total * 100 if du.total else 100
        if free_pct < settings.alert_disk_free_pct:
            alerts.append(("disk", f"磁碟空間不足：剩 {free_pct:.1f}%（{du.free / 1e9:.1f} GB），"
                                   f"門檻 {settings.alert_disk_free_pct}%"))
    except OSError as exc:
        alerts.append(("disk_error", f"無法讀取磁碟用量：{type(exc).__name__}"))

    # 2) 檔案檢查佇列卡住
    oldest = db.scalar(select(func.min(Revision.finalized_at)).join(Artifact, Artifact.revision_id == Revision.id)
                       .where(Artifact.inspection_state == "PENDING"))
    if oldest is not None and now - oldest > timedelta(minutes=settings.alert_queue_minutes):
        pending = db.scalar(select(func.count()).select_from(Artifact).where(Artifact.inspection_state == "PENDING")) or 0
        alerts.append(("queue", f"檔案檢查佇列卡住：{pending} 個檔案等待中，最舊的從 {fmt_dt(oldest)} 等到現在"
                                f"（超過 {settings.alert_queue_minutes} 分鐘）"))

    # 3) 伺服器錯誤
    n_err = errors_in_window(300)
    if n_err >= settings.alert_errors_5min:
        alerts.append(("errors", f"伺服器錯誤暴增：最近 5 分鐘 {n_err} 次（門檻 {settings.alert_errors_5min}）"))

    # 4) 登入失敗（依 IP、依帳號）
    since = now - timedelta(minutes=5)
    rows = db.execute(select(AuditEvent.ip, func.count()).where(
        AuditEvent.action.in_(("auth.login_failed", "auth.rate_limited")), AuditEvent.created_at >= since)
        .group_by(AuditEvent.ip)).all()
    for ip, n in rows:
        if n >= settings.alert_login_fail_ip_5min:
            alerts.append((f"login_ip:{ip}", f"登入失敗暴增：IP {ip or '?'} 最近 5 分鐘失敗 {n} 次"))
    rows = db.execute(select(AuditEvent.target_id, func.count()).where(
        AuditEvent.action == "auth.login_failed", AuditEvent.created_at >= since, AuditEvent.target_id != "")
        .group_by(AuditEvent.target_id)).all()
    for uid, n in rows:
        if n >= settings.alert_login_fail_account_5min:
            alerts.append((f"login_acct:{uid}", f"單一帳號登入失敗暴增：使用者 #{uid} 最近 5 分鐘失敗 {n} 次"))
    return alerts


def run_once(db=None) -> list[str]:
    """執行檢查並發送新的警示；回傳這次實際發出的訊息。"""
    own = db is None
    db = db or SessionLocal()
    sent: list[str] = []
    try:
        found = check(db)
    finally:
        if own:
            db.close()
    now = time.time()
    with _lock:
        for key, msg in found:
            if now - _last_sent.get(key, 0) < settings.alert_repeat_minutes * 60:
                continue
            _last_sent[key] = now
            recent.appendleft({"at": utcnow(), "key": key, "msg": msg})
            notify.send("staff", f"⚠️ 作業繳交系統警示：{msg}")
            log.warning("alert: %s", msg)
            sent.append(msg)
    return sent


def maybe_run(interval: int = 60) -> None:
    """由背景 worker 呼叫；每 interval 秒最多執行一次。"""
    global _last_run
    if time.time() - _last_run < interval:
        return
    _last_run = time.time()
    try:
        run_once()
    except Exception:  # noqa: BLE001
        log.exception("monitor check failed")
