"""PDF 文字相似度比對（只給工作人員看；系統只列出可疑組合，不自動判定抄襲）。

做法：
1. 每份提交取「最新一版、通過檢查」的 PDF，在隔離子行程抽出文字。
2. 正規化（NFKC、小寫、去掉空白與標點），切成長度 K 的字元片段（中英文都適用）。
3. 去掉「超過一半提交都有」的片段（題目敘述、共同範本）與講師上傳的題目範本片段。
4. 兩兩比對：重疊片段數 / 較短那份的片段數（containment），由高到低排序。
5. 對前幾名列出重疊的原文段落，方便人工判斷。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import unicodedata
from pathlib import Path

from sqlalchemy import select

from .db import SessionLocal
from .models import Artifact, Assignment, Revision, SimilarityRun
from .services import assignment_units
from .storage import artifact_path, run_text_extract
from .utils import utcnow

log = logging.getLogger("aams.similarity")

K = 12                 # 片段長度（字元）
MIN_CHARS = 150        # 少於這個字數（正規化後）視為「抽不到文字」
COMMON_RATIO = 0.5     # 超過一半提交都出現的片段視為共同內容
MIN_REPORT = 0.15      # containment 低於此值不列出
MAX_PAIRS = 100
PASSAGE_PAIRS = 30
MAX_PASSAGES = 5

_running: set[int] = set()
_lock = threading.Lock()


def normalize(text: str) -> tuple[str, list[int]]:
    """回傳（正規化字串, 每個字元在原文中的位置）。"""
    out, pos = [], []
    for i, ch in enumerate(text):
        c = unicodedata.normalize("NFKC", ch).lower()
        for cc in c:
            cat = unicodedata.category(cc)
            if cat[0] in "LN":  # 只保留文字與數字
                out.append(cc)
                pos.append(i)
    return "".join(out), pos


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def shingles(norm: str) -> list[int]:
    return [_h(norm[i:i + K]) for i in range(max(0, len(norm) - K + 1))]


def passages(doc: dict, other: set[int], ignore: set[int]) -> list[str]:
    """找出 doc 中與 other 重疊的連續段落，回傳原文片段（最長的幾段）。"""
    sh = doc["sh"]
    ranges: list[list[int]] = []
    for i, h in enumerate(sh):
        if h in other and h not in ignore:
            if ranges and i <= ranges[-1][1]:
                ranges[-1][1] = i + K
            else:
                ranges.append([i, i + K])
    ranges.sort(key=lambda r: r[1] - r[0], reverse=True)
    out = []
    pos, text = doc["pos"], doc["text"]
    for a, b in ranges[:MAX_PASSAGES]:
        if b - a < K * 2:
            continue
        start, end = pos[a], pos[min(b, len(pos)) - 1] + 1
        snippet = " ".join(text[start:end].split())
        out.append(snippet if len(snippet) <= 400 else snippet[:400] + "…")
    return out


def compute(docs: list[dict], template_text: str = "") -> dict:
    """docs: [{key, label, text}] → 結果 dict（純函式，方便測試）。"""
    usable = []
    report_docs = []
    for d in docs:
        norm, pos = normalize(d["text"])
        ok = len(norm) >= MIN_CHARS
        report_docs.append({"key": d["key"], "label": d["label"], "chars": len(norm),
                            "status": "OK" if ok else "NO_TEXT", "submission_id": d.get("submission_id")})
        if ok:
            sh = shingles(norm)
            usable.append({**d, "norm": norm, "pos": pos, "sh": sh, "set": set(sh)})
    ignore: set[int] = set()
    if template_text:
        ignore |= set(shingles(normalize(template_text)[0]))
    if len(usable) >= 4:
        freq: dict[int, int] = {}
        for d in usable:
            for h in d["set"]:
                freq[h] = freq.get(h, 0) + 1
        ignore |= {h for h, n in freq.items() if n > COMMON_RATIO * len(usable)}
    for d in usable:
        d["eff"] = d["set"] - ignore
    pairs = []
    for i in range(len(usable)):
        a = usable[i]
        if not a["eff"]:
            continue
        for j in range(i + 1, len(usable)):
            b = usable[j]
            if not b["eff"] or a.get("owner") == b.get("owner"):
                continue
            inter = len(a["eff"] & b["eff"])
            if not inter:
                continue
            containment = inter / min(len(a["eff"]), len(b["eff"]))
            if containment < MIN_REPORT:
                continue
            jaccard = inter / len(a["eff"] | b["eff"])
            pairs.append({"a": a["key"], "b": b["key"], "a_label": a["label"], "b_label": b["label"],
                          "a_sub": a.get("submission_id"), "b_sub": b.get("submission_id"),
                          "containment": round(containment, 4), "jaccard": round(jaccard, 4), "shared": inter,
                          "_ia": i, "_ib": j})
    pairs.sort(key=lambda p: (p["containment"], p["shared"]), reverse=True)
    pairs = pairs[:MAX_PAIRS]
    for p in pairs[:PASSAGE_PAIRS]:
        p["passages"] = passages(usable[p["_ia"]], usable[p["_ib"]]["eff"], ignore)
    for p in pairs:
        p.pop("_ia")
        p.pop("_ib")
        p.setdefault("passages", [])
    return {"docs": report_docs, "pairs": pairs, "ignored_common": len(ignore), "k": K}


def collect_docs(db, a: Assignment) -> list[dict]:
    docs = []
    for u in assignment_units(db, a):
        if u.submission is None:
            continue
        rev = db.scalar(select(Revision).where(Revision.submission_id == u.submission.id,
                                               Revision.scan_state == "ACCEPTED", Revision.purged_at.is_(None))
                        .order_by(Revision.revision_no.desc()))
        if rev is None:
            continue
        texts, notes = [], []
        for art in db.scalars(select(Artifact).where(Artifact.revision_id == rev.id).order_by(Artifact.id)):
            if art.validated_type != "pdf" or art.inspection_state != "ACCEPTED":
                continue
            p = artifact_path(art)
            if p is None:
                continue
            res = run_text_extract(p)
            if res.get("ok"):
                texts.append(res.get("text", ""))
            else:
                notes.append(res.get("detail", ""))
        owner = f"g{u.group_id}" if u.student_id is None else f"s{u.student_id}"
        docs.append({"key": owner, "owner": owner, "label": f"{u.label}（第 {rev.revision_no} 版）",
                     "text": "\n".join(texts), "submission_id": u.submission.id, "notes": notes})
    return docs


def run(run_id: int, template_path: Path | None = None) -> None:
    """背景執行一次比對（也可在測試中直接同步呼叫）。"""
    with SessionLocal() as db:
        sr = db.get(SimilarityRun, run_id)
        if sr is None:
            return
        sr.status = "RUNNING"
        db.commit()
        try:
            a = db.get(Assignment, sr.assignment_id)
            template_text = ""
            if template_path is not None and template_path.is_file():
                res = run_text_extract(template_path)
                template_text = res.get("text", "") if res.get("ok") else ""
            docs = collect_docs(db, a)
            result = compute(docs, template_text)
            sr.result_json = json.dumps(result, ensure_ascii=False)
            sr.status = "DONE"
            sr.detail = f"比對 {sum(1 for d in result['docs'] if d['status'] == 'OK')} 份，列出 {len(result['pairs'])} 組"
        except Exception as exc:  # noqa: BLE001
            log.exception("similarity run %s failed", run_id)
            sr.status = "FAILED"
            sr.detail = f"比對失敗：{type(exc).__name__}"
        finally:
            sr.finished_at = utcnow()
            db.commit()
            if template_path is not None:
                template_path.unlink(missing_ok=True)
            with _lock:
                _running.discard(sr.assignment_id)


def fail_interrupted_runs() -> None:
    """啟動時把上次被中斷（重開機）的比對標為失敗，否則畫面會一直顯示執行中。"""
    with _lock:
        busy = set(_running)
    with SessionLocal() as db:
        for sr in db.scalars(select(SimilarityRun).where(SimilarityRun.status.in_(("PENDING", "RUNNING")))):
            if sr.assignment_id not in busy:
                sr.status, sr.detail, sr.finished_at = "FAILED", "伺服器重新啟動，比對中斷，請重新執行", utcnow()
        db.commit()


def start(run_id: int, assignment_id: int, template_path: Path | None) -> bool:
    with _lock:
        if assignment_id in _running:
            return False
        _running.add(assignment_id)
    threading.Thread(target=run, args=(run_id, template_path), name=f"aams-sim-{run_id}", daemon=True).start()
    return True
