"""Isolated file inspector.

刻意不 import 任何 AAMS 設定／資料庫模組：由 worker 以「清空環境變數的子行程」
或獨立容器（network_mode: none）執行，只能讀到單一檔案路徑。

    python -I -m aams.inspector <path> <declared_type>   -> stdout JSON
    python -I -m aams.inspector --spool <dir>             -> 監看 spool 目錄（容器模式）
    python -I -m aams.inspector --text <path>             -> 抽出 PDF 文字（相似度比對用）
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import zlib

MAX_INFLATE_TOTAL = 200_000_000  # 解壓縮輸出總量上限（防 zip/flate bomb）
FORBIDDEN_PDF_NAMES = {
    b"JavaScript", b"JS", b"Launch", b"EmbeddedFile", b"EmbeddedFiles", b"RichMedia",
    b"XFA", b"SubmitForm", b"ImportData", b"GoToE", b"Encrypt",
}
NAME_RE = re.compile(rb"/((?:[A-Za-z0-9_.+-]|#[0-9A-Fa-f]{2}){1,64})")
STREAM_RE = re.compile(rb"stream\r?\n")


def _decode_name(raw: bytes) -> bytes:
    return re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)


def _forbidden_names(blob: bytes) -> set[str]:
    found = set()
    for m in NAME_RE.finditer(blob):
        name = _decode_name(m.group(1))
        if name in FORBIDDEN_PDF_NAMES:
            found.add(name.decode())
    return found


def inspect_pdf(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"%PDF-1.") and not data.startswith(b"%PDF-2."):
        return False, "不是有效的 PDF（檔頭錯誤）"
    if b"%%EOF" not in data[-4096:]:
        return False, "PDF 結尾不完整"
    bad = _forbidden_names(data)
    # 解開 FlateDecode stream（含 object stream）再掃一次
    budget = MAX_INFLATE_TOTAL
    for m in STREAM_RE.finditer(data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end == -1:
            break
        d = zlib.decompressobj()
        try:
            out = d.decompress(data[start:end], budget)
        except zlib.error:
            continue
        budget -= len(out)
        if budget <= 0:
            return False, "PDF 解壓縮內容過大"
        bad |= _forbidden_names(out)
    if bad:
        return False, "PDF 含不允許的主動內容：" + ", ".join(sorted(bad))
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return False, "不接受加密 PDF"
        n = len(reader.pages)
        if n < 1:
            return False, "PDF 沒有頁面"
        if n > 2000:
            return False, "PDF 頁數過多"
    except Exception as exc:  # noqa: BLE001
        return False, f"PDF 解析失敗：{type(exc).__name__}"
    return True, f"PDF OK，{n} 頁"


def inspect_png(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return False, "不是有效的 PNG"
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    if not (0 < w <= 12000 and 0 < h <= 12000):
        return False, "PNG 尺寸不合理"
    if b"IEND" not in data[-64:]:
        return False, "PNG 結尾不完整（可能附加其他內容）"
    return True, f"PNG {w}x{h}"


def inspect_jpg(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"\xff\xd8\xff"):
        return False, "不是有效的 JPEG"
    if not data.rstrip(b"\x00\r\n ").endswith(b"\xff\xd9"):
        return False, "JPEG 結尾不完整（可能附加其他內容）"
    return True, "JPEG OK"


def inspect_txt(data: bytes) -> tuple[bool, str]:
    if b"\x00" in data:
        return False, "文字檔含二進位內容"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False, "文字檔必須為 UTF-8"
    return True, "UTF-8 文字"


INSPECTORS = {"pdf": inspect_pdf, "png": inspect_png, "jpg": inspect_jpg, "txt": inspect_txt}


def inspect_path(path: str, declared: str) -> dict:
    fn = INSPECTORS.get(declared)
    if fn is None:
        return {"ok": False, "type": "", "detail": "不支援的檔案類型"}
    try:
        with open(path, "rb") as f:
            data = f.read(41_000_000)
    except OSError:
        return {"ok": False, "type": "", "detail": "無法讀取檔案"}
    ok, detail = fn(data)
    return {"ok": ok, "type": declared if ok else "", "detail": detail[:250]}


def quick_magic_ok(head: bytes, declared: str) -> bool:
    """上傳當下的快速檢查；完整檢查仍由 worker 執行。"""
    if declared == "pdf":
        return head.startswith(b"%PDF-")
    if declared == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if declared == "jpg":
        return head.startswith(b"\xff\xd8\xff")
    if declared == "txt":
        return b"\x00" not in head
    return False


MAX_TEXT_PAGES = 300
MAX_TEXT_CHARS = 400_000


def extract_text(path: str) -> dict:
    """抽出 PDF 文字（相似度比對用）。只在隔離子行程／容器中執行。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return {"ok": False, "text": "", "detail": "加密 PDF"}
        parts: list[str] = []
        total = 0
        for i, page in enumerate(reader.pages):
            if i >= MAX_TEXT_PAGES or total > MAX_TEXT_CHARS:
                break
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - 單頁壞掉不影響其他頁
                t = ""
            parts.append(t)
            total += len(t)
        return {"ok": True, "text": "\n".join(parts)[:MAX_TEXT_CHARS], "detail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "detail": f"無法讀取 PDF：{type(exc).__name__}"}


def spool_loop(spool_dir: str) -> None:  # pragma: no cover - container mode
    """容器模式：讀取 <key>.job（JSON: file, type），寫出 <key>.result。"""
    while True:
        for name in os.listdir(spool_dir):
            if not name.endswith(".job"):
                continue
            job_path = os.path.join(spool_dir, name)
            try:
                with open(job_path, encoding="utf-8") as f:
                    job = json.load(f)
                fname = os.path.basename(job["file"])
                if job.get("op") == "text":
                    result = extract_text(os.path.join(spool_dir, fname))
                else:
                    result = inspect_path(os.path.join(spool_dir, fname), job["type"])
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "type": "", "detail": f"inspector error {type(exc).__name__}"}
            tmp = job_path[:-4] + ".result.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f)
            os.replace(tmp, job_path[:-4] + ".result")
            os.remove(job_path)
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--spool":
        spool_loop(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "--text":
        sys.stdout.buffer.write(json.dumps(extract_text(sys.argv[2]), ensure_ascii=True).encode("ascii") + b"\n")
    elif len(sys.argv) == 3:
        print(json.dumps(inspect_path(sys.argv[1], sys.argv[2]), ensure_ascii=False))
    else:
        print(json.dumps({"ok": False, "type": "", "detail": "usage"}))
        sys.exit(2)
