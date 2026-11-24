"""批次下載 ZIP 產生器。

結構（預設保留原檔名）：
    {Course}_{Assignment}.zip
    ├─ _SUMMARY.txt                總表：誰交了、誰沒交、誰遲交、誰有問題
    ├─ MANIFEST.csv                每個檔案的來源、遲交與否、SHA-256
    ├─ Group01_王小明/              個人作業：組別_姓名
    │   └─ report.pdf
    ├─ Group01_李小華_LATE/         有遲交時資料夾加 _LATE
    │   ├─ README.txt              只有「有狀況」才放：遲交多久、核准註記、被拒絕／淘汰的版本
    │   └─ report.pdf
    └─ Group02/                    團體作業：只有組別
勾選「強制統一命名」→ Group01_王小明_{CourseEn}_{AssignmentEn}[_LATE][_n].ext
勾選「所有版本」    → 資料夾內再分 v3/、v2_LATE/ …
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .models import Artifact, Assignment, Course, Group, Revision, User
from .security import new_token
from .services import Unit
from .storage import TMP, artifact_path
from .utils import csv_safe, fmt_dt, sanitize_filename, utcnow

STATUS_TEXT = {"OK": "已交", "LATE": "遲交", "NONE": "未交", "ISSUE": "有問題"}


class ExportTooLarge(Exception):
    pass


def fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} 天")
    if h:
        parts.append(f"{h} 小時")
    parts.append(f"{m} 分")
    return " ".join(parts)


def group_code(g: Group | None) -> str:
    return f"Group{g.ordinal:02d}" if g is not None else "NoGroup"


def _safe_part(text: str, max_len: int = 40) -> str:
    return sanitize_filename(text, max_len).replace(" ", "_")


def _ext_of(art: Artifact) -> str:
    _stem, dot, ext = (art.original_filename or "").rpartition(".")
    ext = ext.lower() if dot else ""
    return ext if ext.isalnum() and len(ext) <= 5 else (art.validated_type or art.declared_type or "bin")


@dataclass
class UnitResult:
    folder: str
    label: str
    status: str
    notes: list[str] = field(default_factory=list)
    files: int = 0


def build_zip(db, a: Assignment, units: list[Unit], *, all_versions: bool, force_naming: bool,
              include_internal: bool, downloader: User) -> tuple[Path, dict]:
    course = db.get(Course, a.course_id)
    tmp = TMP / f"download-{new_token(12)}.zip"
    total = 0
    used_folders: set[str] = set()
    manifest = [["path", "owner", "revision_no", "submitted_at(Asia/Taipei)", "is_late", "late_by", "late_note",
                 "original_filename", "bytes", "sha256"]]
    results: list[UnitResult] = []
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for u in units:
                g = db.get(Group, u.group_id) if u.group_id else None
                if a.submission_mode == "GROUP":
                    base = group_code(g)
                    label = base
                else:
                    student = u.members[0] if u.members else None
                    name = student.name if student else "unknown"
                    base = f"{group_code(g)}_{_safe_part(name)}"
                    label = f"{group_code(g)} {name}"
                if u.submission is None:
                    results.append(UnitResult("", label, "NONE", ["未繳交"]))
                    continue
                revs = list(db.scalars(select(Revision).where(Revision.submission_id == u.submission.id)
                                       .order_by(Revision.revision_no.desc())))
                usable = [r for r in revs if r.scan_state == "ACCEPTED" and r.purged_at is None]
                include = usable if all_versions else usable[:1]
                notes: list[str] = []
                latest = revs[0] if revs else None
                if latest is not None and latest.scan_state != "ACCEPTED":
                    state = "仍在檔案檢查中" if latest.scan_state == "PENDING_SCAN" else "未通過檔案檢查"
                    if include:
                        notes.append(f"最新的第 {latest.revision_no} 版{state}，這裡提供的是第 {include[0].revision_no} 版。")
                    else:
                        notes.append(f"最新的第 {latest.revision_no} 版{state}，沒有可下載的檔案。")
                elif not include:
                    notes.append("沒有可下載的檔案（所有版本都未通過檢查或已淘汰）。")
                if all_versions:
                    purged = [r.revision_no for r in revs if r.purged_at is not None]
                    rejected = [r.revision_no for r in revs if r.scan_state == "REJECTED" and r.purged_at is None]
                    if purged:
                        notes.append("第 " + "、".join(map(str, sorted(purged))) + " 版已依「只保留最近 5 版」規則刪除。")
                    if rejected:
                        notes.append("第 " + "、".join(map(str, sorted(rejected))) + " 版未通過檔案檢查，未包含。")
                late_revs = [r for r in include if r.is_late]
                for r in late_revs:
                    notes.append(f"第 {r.revision_no} 版遲交 {fmt_duration((r.finalized_at - r.due_at_snapshot).total_seconds())}"
                                 f"（截止 {fmt_dt(r.due_at_snapshot)}，提交 {fmt_dt(r.finalized_at)}）。")
                    if include_internal and r.late_note:
                        approver = db.get(User, r.late_note_by) if r.late_note_by else None
                        notes.append(f"    已核准遲交：{r.late_note}（{approver.name if approver else '?'}，"
                                     f"{fmt_dt(r.late_note_at)}）")
                folder = base + ("_LATE" if late_revs else "")
                n = 2
                while folder in used_folders:
                    folder = f"{base}{'_LATE' if late_revs else ''}_{n}"
                    n += 1
                used_folders.add(folder)
                status = "ISSUE" if (not include or (latest is not None and latest.scan_state != "ACCEPTED")) else \
                    ("LATE" if late_revs else "OK")
                res = UnitResult(folder, label, status, notes)
                for r in include:
                    sub = "" if not all_versions else f"v{r.revision_no}{'_LATE' if r.is_late else ''}/"
                    arts = [x for x in db.scalars(select(Artifact).where(Artifact.revision_id == r.id).order_by(Artifact.id))
                            if x.inspection_state == "ACCEPTED"]
                    used_names: set[str] = set()
                    for i, art in enumerate(arts, start=1):
                        p = artifact_path(art)
                        if p is None:
                            notes.append(f"第 {r.revision_no} 版的檔案 {art.original_filename} 在伺服器上找不到，未包含。")
                            continue
                        total += art.size
                        if total > settings.export_max_bytes:
                            raise ExportTooLarge
                        if force_naming:
                            fname = (f"{base}_{course.file_name}_{a.file_name}{'_LATE' if r.is_late else ''}"
                                     f"{f'_{i}' if len(arts) > 1 else ''}.{_ext_of(art)}")
                        else:
                            fname = sanitize_filename(art.original_filename)
                        stem, dot, ext = fname.rpartition(".")
                        cand, k = fname, 2
                        while cand in used_names:
                            cand = f"{stem} ({k}).{ext}" if dot else f"{fname} ({k})"
                            k += 1
                        used_names.add(cand)
                        arc = f"{folder}/{sub}{cand}"
                        zf.write(p, arcname=arc)
                        res.files += 1
                        manifest.append([arc, label, r.revision_no, fmt_dt(r.finalized_at), r.is_late,
                                         fmt_duration((r.finalized_at - r.due_at_snapshot).total_seconds()) if r.is_late else "",
                                         r.late_note if include_internal else "", art.original_filename, art.size, art.sha256])
                if notes:
                    zf.writestr(f"{folder}/README.txt", _readme(label, a, course, notes))
                elif res.files == 0:
                    zf.writestr(f"{folder}/README.txt", _readme(label, a, course, ["沒有可下載的檔案。"]))
                results.append(res)
            zf.writestr("_SUMMARY.txt", _summary(a, course, results, downloader, all_versions, force_naming))
            buf = io.StringIO()
            csv.writer(buf).writerows([[csv_safe(c) for c in row] for row in manifest])
            zf.writestr("MANIFEST.csv", "﻿" + buf.getvalue())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    stats = {"units": len(results), "files": len(manifest) - 1, "bytes": total,
             "missing": sum(1 for r in results if r.status == "NONE")}
    return tmp, stats


def _readme(label: str, a: Assignment, course: Course, notes: list[str]) -> str:
    lines = [f"{label} · {course.title} / {a.title}", "=" * 40, ""]
    lines += [f"- {n}" if not n.startswith("    ") else n for n in notes]
    lines += ["", "（由 AIS3 Junior 作業繳交系統自動產生）", ""]
    return "\r\n".join(lines)


def _summary(a: Assignment, course: Course, results: list[UnitResult], downloader: User,
             all_versions: bool, force_naming: bool) -> str:
    counts = {k: sum(1 for r in results if r.status == k) for k in STATUS_TEXT}
    lines = [
        f"{course.title} / {a.title}",
        f"截止時間：{fmt_dt(a.due_at)}（台北）",
        f"下載時間：{fmt_dt(utcnow())}（台北） · 下載者：{downloader.name}",
        f"選項：{'保留的所有版本' if all_versions else '只要最新版'} · {'強制統一命名' if force_naming else '保留原檔名'}",
        "",
        f"共 {len(results)} 份：已交 {counts['OK']} · 遲交 {counts['LATE']} · 有問題 {counts['ISSUE']} · 未交 {counts['NONE']}",
        "",
    ]
    order = {"ISSUE": 0, "LATE": 1, "NONE": 2, "OK": 3}
    for r in sorted(results, key=lambda x: (order[x.status], x.label)):
        head = f"[{STATUS_TEXT[r.status]}] {r.label}"
        if r.folder:
            head += f"  → {r.folder}/（{r.files} 個檔案）"
        lines.append(head)
        for n in r.notes:
            if r.status != "NONE":
                lines.append(f"      {n.strip()}")
    lines += ["", "（由 AIS3 Junior 作業繳交系統自動產生）", ""]
    return "\r\n".join(lines)
