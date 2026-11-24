"""附件／教材下載與隔離預覽（SEC-009, SEC-010）。"""
from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import acl
from ..config import settings
from ..db import SessionLocal
from ..deps import Ctx, audit, get_ctx, main_origin, preview_base, render
from ..models import Artifact, Assignment, Course, CourseMaterial, Revision, Submission
from ..security import sign, unsign
from ..storage import CONTENT_TYPES, artifact_path, material_path
from ..utils import sanitize_filename

router = APIRouter()
preview_router = APIRouter()


def _artifact_with_acl(ctx: Ctx, artifact_id: int) -> tuple[Artifact, Submission, Assignment]:
    db = ctx.db
    art = db.get(Artifact, artifact_id)
    if art is None:
        raise HTTPException(404)
    rev = db.get(Revision, art.revision_id)
    sub = db.get(Submission, rev.submission_id)
    a = db.get(Assignment, sub.assignment_id)
    if not acl.can_view_submission(ctx, sub, a):  # 每次請求重新檢查 ACL
        raise HTTPException(404)
    if art.inspection_state != "ACCEPTED" or art.purged_at is not None:
        raise HTTPException(409, "此檔案尚未通過安全檢查、檢查失敗或已被淘汰，無法預覽／下載")
    return art, sub, a


def _attachment_headers(filename: str) -> dict:
    safe = sanitize_filename(filename)
    ascii_name = safe.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "no-store",
    }


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: int, ctx: Ctx = Depends(get_ctx)):
    art, sub, a = _artifact_with_acl(ctx, artifact_id)
    path = artifact_path(art)
    if path is None:
        raise HTTPException(404)
    if ctx.is_admin and not acl.is_owner(ctx, sub):
        audit(ctx.db, ctx.user.id, "file.download", "artifact", art.id, {}, ctx.ip)
        ctx.db.commit()
    return FileResponse(path, media_type="application/octet-stream", headers=_attachment_headers(art.original_filename))


@router.get("/artifacts/{artifact_id}/preview")
def preview_artifact(artifact_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    art, sub, a = _artifact_with_acl(ctx, artifact_id)
    token = sign({"k": "a", "id": art.id, "u": ctx.user.id, "o": main_origin(request),
                  "exp": int(time.time()) + settings.preview_token_ttl})
    src = f"{preview_base(request)}/p/{token}"
    return render(request, "preview.html", ctx, src=src, fname=art.original_filename, ftype=art.validated_type,
                  back=request.headers.get("referer", "/"), download=f"/artifacts/{art.id}/download")


def _material_with_acl(ctx: Ctx, material_id: int) -> CourseMaterial:
    m = ctx.db.get(CourseMaterial, material_id)
    if m is None:
        raise HTTPException(404)
    course = ctx.db.get(Course, m.course_id)
    if not acl.can_manage_course(ctx, m.course_id):
        if course.status != "PUBLISHED" or m.visibility != "PUBLISHED":
            raise HTTPException(404)
    return m


@router.get("/materials/{material_id}/download")
def download_material(material_id: int, ctx: Ctx = Depends(get_ctx)):
    m = _material_with_acl(ctx, material_id)
    path = material_path(m.storage_key)
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", headers=_attachment_headers(m.original_filename))


@router.get("/materials/{material_id}/preview")
def preview_material(material_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    m = _material_with_acl(ctx, material_id)
    token = sign({"k": "m", "id": m.id, "u": ctx.user.id, "o": main_origin(request),
                  "exp": int(time.time()) + settings.preview_token_ttl})
    return render(request, "preview.html", ctx, src=f"{preview_base(request)}/p/{token}", fname=m.original_filename,
                  ftype=m.file_type, back=f"/courses/{m.course_id}", download=f"/materials/{m.id}/download")


# ------------------- 預覽專用 Origin（不同 port，無 Cookie 驗證，只認短效簽章） -------------------
@preview_router.get("/p/{token}")
def preview_serve(token: str):
    payload = unsign(token)
    if payload is None:
        raise HTTPException(403)
    with SessionLocal() as db:
        if payload.get("k") == "a":
            art = db.get(Artifact, int(payload["id"]))
            if art is None or art.inspection_state != "ACCEPTED" or art.purged_at is not None:
                raise HTTPException(404)
            path, ftype = artifact_path(art), art.validated_type
        elif payload.get("k") == "m":
            m = db.get(CourseMaterial, int(payload["id"]))
            if m is None:
                raise HTTPException(404)
            path, ftype = material_path(m.storage_key), m.file_type
        else:
            raise HTTPException(403)
    if path is None or ftype not in CONTENT_TYPES:
        raise HTTPException(404)
    origin = str(payload.get("o", ""))
    if not origin.startswith(("http://", "https://")) or any(c in origin for c in " ;'\"\r\n"):
        origin = "'none'"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-site",
    }
    if ftype == "pdf":
        # 瀏覽器內建 PDF viewer 在 CSP sandbox 下會被封鎖，因此以「獨立 Origin」隔離
        headers["Content-Security-Policy"] = f"frame-ancestors {origin}; form-action 'none'; base-uri 'none'"
    else:
        headers["Content-Security-Policy"] = f"default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox; frame-ancestors {origin}"
    return FileResponse(path, media_type=CONTENT_TYPES[ftype], headers=headers)


@preview_router.get("/")
def preview_root():
    return {"service": "aams-preview"}
