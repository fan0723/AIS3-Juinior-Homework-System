"""Runtime configuration (環境變數 / .env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v and v.strip() else default


class Settings:
    def __init__(self) -> None:
        self.secret_key: str = os.environ.get("AAMS_SECRET_KEY", "")
        self.data_dir: Path = Path(os.environ.get("AAMS_DATA_DIR") or (BASE_DIR / "data")).resolve()
        self.database_url: str = os.environ.get("AAMS_DATABASE_URL") or (
            "sqlite:///" + (self.data_dir / "aams.db").as_posix()
        )
        self.host: str = os.environ.get("AAMS_HOST", "127.0.0.1")
        self.port: int = _int("AAMS_PORT", 8000)
        self.preview_port: int = _int("AAMS_PREVIEW_PORT", 8001)
        # 若留空，預覽來源會依使用者連線的主機名稱 + AAMS_PREVIEW_PORT 自動推導
        self.preview_url: str = os.environ.get("AAMS_PREVIEW_URL", "").rstrip("/")
        self.cookie_secure: bool = _bool("AAMS_COOKIE_SECURE", False)
        self.session_hours: int = _int("AAMS_SESSION_HOURS", 12)
        # 每個來源 IP 5 分鐘內的登入嘗試上限（同一 NAT 後方大量測試時可調高）
        self.login_ip_limit: int = _int("AAMS_LOGIN_IP_LIMIT", 60)
        self.forwarded_allow_ips: str = os.environ.get("AAMS_FORWARDED_ALLOW_IPS", "127.0.0.1")

        # 系統層檔案類型上限（講師只能在這個範圍內勾選）
        self.system_allowed_types: list[str] = [
            t.strip().lower()
            for t in os.environ.get("AAMS_SYSTEM_ALLOWED_TYPES", "pdf,png,jpg,txt").split(",")
            if t.strip()
        ]
        # 十進位 MB：1 MB = 1,000,000 bytes
        self.default_total_bytes: int = 20_000_000
        self.max_total_bytes_cap: int = 40_000_000
        self.max_files_cap: int = 10
        self.retained_versions: int = 5
        # 反向代理之外，應用層對單一上傳 request 的硬上限（上限額度 + multipart overhead）
        self.max_upload_request_bytes: int = self.max_total_bytes_cap + 2_000_000
        self.max_material_bytes: int = _int("AAMS_MAX_MATERIAL_BYTES", 100_000_000)
        self.max_csv_bytes: int = 2_000_000
        self.max_csv_rows: int = 2000

        self.event_name: str = os.environ.get("AAMS_EVENT_NAME", "AIS3 Junior 2026")
        # 營隊結束日（台北時間，YYYY-MM-DD 或 YYYY-MM-DDTHH:MM）；作業「最終收件期限」預設為此日 + 7 天
        self.event_end_at: str = os.environ.get("AAMS_EVENT_END_AT", "")
        self.final_deadline_days: int = 7
        self.appeal_days: int = 3  # 成績申覆期限：第一次發布後 3 天

        # Discord 頻道 Webhook：建議留空，改由管理員在網頁「管理後台 › 系統」設定（加密存放）。
        # 在 .env 填了就以 .env 為準，網頁上鎖住不能改（適合 Docker / 自動化部署）。
        self.discord_staff_webhook: str = os.environ.get("AAMS_DISCORD_STAFF_WEBHOOK", "").strip()
        self.discord_student_webhook: str = os.environ.get("AAMS_DISCORD_STUDENT_WEBHOOK", "").strip()
        # 截止前提醒系統預設（小時，逗號分隔）：留空 = 在網頁設定（未設定時為 2,1）；填了就鎖住網頁設定
        self.reminder_hours_env: str = os.environ.get("AAMS_REMINDER_HOURS", "").strip()
        # 監控警示門檻
        self.alert_disk_free_pct: int = _int("AAMS_ALERT_DISK_FREE_PCT", 15)
        self.alert_queue_minutes: int = _int("AAMS_ALERT_QUEUE_MINUTES", 5)
        self.alert_errors_5min: int = _int("AAMS_ALERT_ERRORS_5MIN", 10)
        self.alert_login_fail_ip_5min: int = _int("AAMS_ALERT_LOGIN_FAIL_IP_5MIN", 20)
        self.alert_login_fail_account_5min: int = _int("AAMS_ALERT_LOGIN_FAIL_ACCOUNT_5MIN", 10)
        self.alert_repeat_minutes: int = _int("AAMS_ALERT_REPEAT_MINUTES", 30)
        # 批次下載：同時產生 ZIP 的數量上限（避免塞爆網路／磁碟）
        self.download_max_concurrent: int = _int("AAMS_DOWNLOAD_MAX_CONCURRENT", 2)
        self.inspector_mode: str = os.environ.get("AAMS_INSPECTOR_MODE", "subprocess")  # subprocess | spool
        self.inspector_timeout: int = _int("AAMS_INSPECTOR_TIMEOUT", 60)
        self.run_worker: bool = _bool("AAMS_RUN_WORKER", True)
        self.preview_token_ttl: int = 300
        self.export_max_bytes: int = _int("AAMS_EXPORT_MAX_BYTES", 3_000_000_000)

        for sub in ("quarantine", "accepted", "materials", "tmp", "spool", "backups"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def event_end_utc(self):
        """AAMS_EVENT_END_AT → naive UTC；只寫日期時視為當天 23:59（台北）。未設定或格式錯誤 → None。"""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        v = self.event_end_at.strip()
        if not v:
            return None
        for fmt, date_only in (("%Y-%m-%d", True), ("%Y-%m-%dT%H:%M", False), ("%Y-%m-%d %H:%M", False)):
            try:
                d = datetime.strptime(v, fmt)
            except ValueError:
                continue
            if date_only:
                d = d.replace(hour=23, minute=59)
            return d.replace(tzinfo=ZoneInfo("Asia/Taipei")).astimezone(timezone.utc).replace(tzinfo=None)
        return None


settings = Settings()
