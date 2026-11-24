from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_connect_args = {"check_same_thread": False, "timeout": 30} if settings.is_sqlite else {}
# 約 100 人同時使用：連線池需大於 threadpool（預設 40）以免尖峰時排隊逾時
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True,
                       pool_size=20, max_overflow=40, pool_timeout=60)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> list[str]:
    """簡易升級：既有資料表缺少新版欄位時自動 ALTER TABLE ADD COLUMN。

    只處理「新增欄位」（新欄位一律可為 NULL 或有 server_default）；不做改名／刪除。
    正式改版仍建議導入 Alembic。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
                if col.server_default is not None:
                    arg = col.server_default.arg
                    ddl += f" DEFAULT '{arg}'" if isinstance(arg, str) else f" DEFAULT {arg.compile(dialect=engine.dialect)}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added
