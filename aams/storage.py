"""Private storage + background inspection worker + cleanup."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Artifact, Revision
from .utils import utcnow

log = logging.getLogger("aams.storage")

QUARANTINE = settings.data_dir / "quarantine"
ACCEPTED = settings.data_dir / "accepted"
MATERIALS = settings.data_dir / "materials"
TMP = settings.data_dir / "tmp"
SPOOL = settings.data_dir / "spool"

INSPECTOR_SCRIPT = Path(__file__).with_name("inspector.py")
DANGEROUS_INNER_EXT = {
    "php", "php3", "php4", "php5", "phtml", "phar", "html", "htm", "xhtml", "svg", "js", "mjs", "exe",
    "dll", "bat", "cmd", "ps1", "sh", "py", "pl", "cgi", "asp", "aspx", "jsp", "jar", "vbs", "msi", "hta",
}
EXT_ALIASES = {"jpeg": "jpg"}
CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "txt": "text/plain; charset=utf-8"}


def new_key() -> str:
    return secrets.token_hex(24)


def declared_type_of(filename: str, allowed: list[str]) -> str | None:
    """嚴格副檔名白名單：最後一個副檔名須在允許清單，且中間不可夾帶危險副檔名。"""
    name = (filename or "").strip().lower()
    if not name or "/" in name or "\\" in name or "\x00" in name:
        return None
    parts = name.split(".")
    if len(parts) < 2:
        return None
    ext = EXT_ALIASES.get(parts[-1], parts[-1])
    if ext not in allowed or ext not in settings.system_allowed_types:
        return None
    if any(p in DANGEROUS_INNER_EXT for p in parts[1:-1]):
        return None
    return ext


def stream_to(src, dest: Path, limit: int) -> tuple[int, str]:
    """Copy file-like to dest computing sha256; abort if > limit. Returns (size, sha256)."""
    h = hashlib.sha256()
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise OverflowError
            h.update(chunk)
            out.write(chunk)
    return size, h.hexdigest()


def artifact_path(a: Artifact) -> Path | None:
    if a.purged_at is not None:
        return None
    base = ACCEPTED if a.inspection_state == "ACCEPTED" else QUARANTINE
    p = (base / a.private_key).resolve()
    if p.parent != base.resolve() or not p.is_file():
        return None
    return p


def material_path(key: str) -> Path | None:
    p = (MATERIALS / key).resolve()
    if p.parent != MATERIALS.resolve() or not p.is_file():
        return None
    return p


# ---------------- inspection ----------------
def _clean_env() -> dict:
    keep = {"SYSTEMROOT", "PATH", "TEMP", "TMP"}
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def run_inspector(path: Path, declared: str) -> dict:
    if settings.inspector_mode == "spool":
        return _run_spool(path, declared)
    try:
        cp = subprocess.run(
            [sys.executable, "-I", str(INSPECTOR_SCRIPT), str(path), declared],
            capture_output=True, timeout=settings.inspector_timeout, env=_clean_env(),
            cwd=str(TMP), check=False,
        )
        return json.loads(cp.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "type": "", "detail": "檔案檢查逾時（fail-closed）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "type": "", "detail": f"檔案檢查失敗（fail-closed）：{type(exc).__name__}"}


def run_text_extract(path: Path) -> dict:
    """在隔離環境抽出 PDF 文字（相似度比對用），失敗回傳 ok=False。"""
    if settings.inspector_mode == "spool":
        return _run_spool(path, "pdf", op="text")
    try:
        cp = subprocess.run(
            [sys.executable, "-I", str(INSPECTOR_SCRIPT), "--text", str(path)],
            capture_output=True, timeout=settings.inspector_timeout, env=_clean_env(), cwd=str(TMP), check=False,
        )
        return json.loads(cp.stdout.decode("ascii", "replace").strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "detail": "抽取文字逾時"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "detail": f"抽取文字失敗：{type(exc).__name__}"}


def _run_spool(path: Path, declared: str, op: str = "inspect") -> dict:
    key = f"{path.name}-{op}" if op != "inspect" else path.name
    shutil.copyfile(path, SPOOL / key)
    job = SPOOL / f"{key}.job"
    job.write_text(json.dumps({"file": key, "type": declared, "op": op}), encoding="utf-8")
    result = SPOOL / f"{key}.result"
    deadline = time.time() + settings.inspector_timeout + 30
    try:
        while time.time() < deadline:
            if result.exists():
                return json.loads(result.read_text(encoding="utf-8"))
            time.sleep(0.5)
        return {"ok": False, "type": "", "detail": "檔案檢查逾時（fail-closed）"}
    finally:
        for p in (SPOOL / key, job, result):
            p.unlink(missing_ok=True)


def inspect_pending_once() -> int:
    """Process pending artifacts; returns number processed."""
    processed = 0
    with SessionLocal() as db:
        pending = list(db.scalars(select(Artifact).where(Artifact.inspection_state == "PENDING").limit(20)))
        for art in pending:
            src = (QUARANTINE / art.private_key)
            if art.purged_at is not None:
                art.inspection_state, art.inspection_detail = "REJECTED", "已依「只保留 5 版」規則淘汰，未檢查"
            elif not src.is_file():
                art.inspection_state, art.inspection_detail = "REJECTED", "檔案遺失（fail-closed）"
            else:
                res = run_inspector(src, art.declared_type)
                if res.get("ok") and res.get("type") == art.declared_type:
                    os.replace(src, ACCEPTED / art.private_key)
                    art.inspection_state, art.validated_type = "ACCEPTED", art.declared_type
                else:
                    src.unlink(missing_ok=True)  # 失敗檔案不留在任何可下載路徑
                    art.inspection_state = "REJECTED"
                art.inspection_detail = str(res.get("detail", ""))[:250]
            processed += 1
            db.flush()
            _update_revision_state(db, art.revision_id)
            db.commit()
    return processed


def _update_revision_state(db, revision_id: int) -> None:
    rev = db.get(Revision, revision_id)
    states = [a.inspection_state for a in db.scalars(select(Artifact).where(Artifact.revision_id == revision_id))]
    if any(s == "REJECTED" for s in states):
        rev.scan_state = "REJECTED"
    elif states and all(s == "ACCEPTED" for s in states):
        rev.scan_state = "ACCEPTED"


def purge_revision_files(db, rev: Revision) -> list[Path]:
    """標記淘汰並回傳要刪除的檔案路徑；呼叫端在 commit 成功後才刪（交易失敗時檔案仍在）。"""
    now = utcnow()
    paths: list[Path] = []
    for art in db.scalars(select(Artifact).where(Artifact.revision_id == rev.id, Artifact.purged_at.is_(None))):
        paths += [QUARANTINE / art.private_key, ACCEPTED / art.private_key]
        art.purged_at = now
    rev.purged_at = now
    return paths


def cleanup_tmp(max_age_s: int = 6 * 3600) -> None:
    now = time.time()
    for p in TMP.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > max_age_s:
                p.unlink()
        except OSError:
            pass


_stop = threading.Event()


def worker_loop() -> None:
    log.info("inspection worker started (mode=%s)", settings.inspector_mode)
    last_cleanup = 0.0
    while not _stop.is_set():
        try:
            n = inspect_pending_once()
            from . import monitor, reminders

            monitor.maybe_run()
            reminders.maybe_run()
            if time.time() - last_cleanup > 600:
                cleanup_tmp()
                last_cleanup = time.time()
        except Exception:  # noqa: BLE001
            log.exception("worker error")
            n = 0
        _stop.wait(0.5 if n else 2.0)


def start_worker() -> threading.Thread:
    t = threading.Thread(target=worker_loop, name="aams-inspector", daemon=True)
    t.start()
    return t


def stop_worker() -> None:
    _stop.set()
