"""PDF 相似度比對頁面（講師／管理員）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .. import acl, similarity
from ..deps import Ctx, audit, render, require_csrf, staff_ctx
from ..models import Assignment, Course, SimilarityRun, User
from ..security import new_token
from ..storage import TMP, stream_to
from ..utils import sanitize_filename

router = APIRouter(prefix="/staff")

TEMPLATE_MAX = 20_000_000


def _assignment(ctx: Ctx, assignment_id: int) -> Assignment:
    a = ctx.db.get(Assignment, assignment_id)
    if a is None:
        raise HTTPException(404)
    acl.require_manage_course(ctx, a.course_id)
    return a


@router.get("/assignments/{assignment_id}/similarity")
def similarity_page(assignment_id: int, request: Request, ctx: Ctx = Depends(staff_ctx)):
    a = _assignment(ctx, assignment_id)
    db = ctx.db
    runs = list(db.scalars(select(SimilarityRun).where(SimilarityRun.assignment_id == a.id)
                           .order_by(SimilarityRun.id.desc()).limit(10)))
    latest = runs[0] if runs else None
    result = json.loads(latest.result_json) if latest and latest.status == "DONE" and latest.result_json else None
    return render(request, "staff/similarity.html", ctx, a=a, course=db.get(Course, a.course_id), runs=runs,
                  latest=latest, result=result, users={r.created_by: db.get(User, r.created_by) for r in runs})


@router.post("/assignments/{assignment_id}/similarity")
def similarity_start(assignment_id: int, template: UploadFile | None = File(None), csrf_token: str = Form(""),
                     ctx: Ctx = Depends(staff_ctx)):
    require_csrf(ctx.sess, csrf_token)
    a = _assignment(ctx, assignment_id)
    tpl_path = None
    tpl_name = ""
    if template is not None and template.filename:
        if not template.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "題目範本只接受 PDF")
        tpl_path = TMP / f"sim-template-{new_token(12)}.pdf"
        try:
            stream_to(template.file, tpl_path, TEMPLATE_MAX)
        except OverflowError:
            raise HTTPException(413, "題目範本過大（上限 20 MB）")
        with open(tpl_path, "rb") as fh:
            if not fh.read(5).startswith(b"%PDF-"):
                tpl_path.unlink(missing_ok=True)
                raise HTTPException(400, "題目範本不是有效的 PDF")
        tpl_name = sanitize_filename(template.filename)
    sr = SimilarityRun(assignment_id=a.id, created_by=ctx.user.id, template_name=tpl_name)
    ctx.db.add(sr)
    ctx.db.flush()
    audit(ctx.db, ctx.user.id, "similarity.run", "assignment", a.id, {"run": sr.id, "template": tpl_name}, ctx.ip)
    ctx.db.commit()
    if not similarity.start(sr.id, a.id, tpl_path):
        sr.status, sr.detail = "FAILED", "這份作業已有比對正在執行，請稍後再試"
        ctx.db.commit()
        if tpl_path:
            tpl_path.unlink(missing_ok=True)
    return RedirectResponse(f"/staff/assignments/{a.id}/similarity?msg=created", 303)
