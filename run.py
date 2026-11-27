"""啟動 AAMS：主站（AAMS_PORT）＋ 隔離預覽來源（AAMS_PREVIEW_PORT）。

    python run.py
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from aams.config import settings


async def _main() -> None:
    from aams.main import app, preview_app

    common = dict(proxy_headers=True, forwarded_allow_ips=settings.forwarded_allow_ips, server_header=False,
                  date_header=False, log_level="info")
    main_srv = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, **common))
    prev_srv = uvicorn.Server(uvicorn.Config(preview_app, host=settings.host, port=settings.preview_port,
                                             lifespan="off", **common))
    print(f"\n  AAMS 主站     : http://{settings.host}:{settings.port}")
    print(f"  預覽來源       : http://{settings.host}:{settings.preview_port}  (只服務短效簽章連結)")
    print(f"  資料目錄       : {settings.data_dir}")
    print(f"  資料庫         : {'SQLite' if settings.is_sqlite else 'PostgreSQL'}\n")
    tasks = [asyncio.create_task(main_srv.serve()), asyncio.create_task(prev_srv.serve())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    # 任一個停止（例如主站連不上資料庫、啟動失敗）就整個結束，讓 Docker 的 restart 重新啟動，
    # 而不是只剩預覽來源在跑、主站永遠 502
    main_srv.should_exit = prev_srv.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    if not (main_srv.started and prev_srv.started):
        raise SystemExit("AAMS 啟動失敗（詳見上方錯誤訊息）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
