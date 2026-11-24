"""截止尖峰壓測（規格 §9.3 #14）：72 位學員同時提交。

前置：伺服器以 `manage.py seed-demo --active` 建立示範資料並啟動。

    python tools/load_test.py --base http://127.0.0.1:8000 --mb 20
    python tools/load_test.py --base http://192.168.1.50:8000 --mb 19 --assignment "Lab 1"
    python tools/load_test.py --mb 35 --assignment "Crackme" --files 2   # 40 MB 額度的團體作業

注意：預設作業 Lab 1 的上限是 20 MB（20,000,000 bytes），檔案會略小於指定 MB 以保留 PDF 結構空間。
"""
from __future__ import annotations

import argparse
import io
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from pypdf import PdfWriter

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
IDEM = re.compile(r'name="idempotency_key" value="([^"]+)"')


def make_pdf(total_bytes: int) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=300, height=300)
    buf = io.BytesIO()
    w.write(buf)
    data = buf.getvalue()
    pad = max(0, total_bytes - len(data) - 4)
    head, rest = data.split(b"\n", 1)
    return head + b"\n%" + b"A" * pad + b"\n" + rest


def student_emails() -> list[str]:
    return [f"s{g:02d}{k}@demo.ais3.local" for g in range(1, 13) for k in range(1, 7)]


def run_one(base: str, email: str, password: str, title: str, blobs: list[bytes], barrier: threading.Barrier) -> dict:
    out = {"email": email, "ok": False, "status": None, "seconds": None, "error": ""}
    try:
        with httpx.Client(base_url=base, timeout=300, follow_redirects=False) as c:
            r = c.get("/login")
            r = c.post("/login", data={"email": email, "password": password, "csrf_token": CSRF.search(r.text).group(1), "next": "/"})
            if r.status_code != 303 or r.headers.get("location") != "/":
                out["error"] = f"login {r.status_code} {r.headers.get('location')}"
                barrier.abort()
                return out
            home = c.get("/").text
            m = re.search(r'/assignments/(\d+)">' + re.escape(title), home)
            if not m:
                out["error"] = "assignment not found"
                barrier.abort()
                return out
            page = c.get(f"/assignments/{m.group(1)}").text
            data = {"csrf_token": CSRF.search(page).group(1), "idempotency_key": IDEM.search(page).group(1), "late_ack": "1"}
            files = [("files", (f"report{i}.pdf", b, "application/pdf")) for i, b in enumerate(blobs)]
            barrier.wait(timeout=120)  # 全部準備好後同時送出
            t0 = time.perf_counter()
            r = c.post(f"/assignments/{m.group(1)}/submit", data=data, files=files)
            out["seconds"] = time.perf_counter() - t0
            out["status"] = r.status_code
            out["ok"] = r.status_code == 303 and "/receipt" in r.headers.get("location", "")
            if not out["ok"]:
                out["error"] = r.text[:120]
    except threading.BrokenBarrierError:
        out["error"] = out["error"] or "barrier broken"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--password", default="Ais3-Demo-2026")
    ap.add_argument("--assignment", default="Lab 1", help="作業標題開頭（首頁連結文字）")
    ap.add_argument("--mb", type=float, default=19.5, help="每次提交合計大小（十進位 MB）")
    ap.add_argument("--files", type=int, default=1, help="每次提交拆成幾個檔案")
    ap.add_argument("--users", type=int, default=72)
    args = ap.parse_args()

    total = int(args.mb * 1_000_000)
    per = total // args.files
    blobs = [make_pdf(per) for _ in range(args.files)]
    emails = student_emails()[: args.users]
    print(f"{len(emails)} 位學員同時提交，每人 {args.files} 檔、合計 {sum(map(len, blobs)) / 1e6:.2f} MB → {args.base}")
    barrier = threading.Barrier(len(emails))
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(emails)) as ex:
        results = list(ex.map(lambda e: run_one(args.base, e, args.password, args.assignment, blobs, barrier), emails))
    wall = time.perf_counter() - t0
    ok = [r for r in results if r["ok"]]
    times = [r["seconds"] for r in ok]
    print(f"成功收據 {len(ok)}/{len(results)}，總耗時 {wall:.1f}s")
    if times:
        print(f"單次提交耗時：中位數 {statistics.median(times):.2f}s，最大 {max(times):.2f}s")
    for r in results:
        if not r["ok"]:
            print("  失敗", r["email"], r["status"], r["error"])
    print("提示：到 管理後台 首頁觀察「檢查中」數量下降的速度，即為檔案檢查佇列的消化速度。")


if __name__ == "__main__":
    main()
