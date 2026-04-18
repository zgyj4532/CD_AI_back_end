"""数据库连接与依赖（基于 pymysql）。

本文件提供 `get_db` 依赖，返回一个 `pymysql` 连接对象。
调用方可使用 `conn.cursor()`、`conn.commit()` 等原生方法。

环境变量 `DATABASE_URL` 支持类似：
  mysql+pymysql://user:password@host:3306/dbname?charset=utf8mb4
"""

from __future__ import annotations

import os
import pymysql
from urllib.parse import urlparse, parse_qs
from typing import Dict, Generator
from app.config import settings


def parse_mysql_url(url: str) -> Dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("DATABASE_URL must start with mysql:// or mysql+pymysql://")

    user = parsed.username or "root"
    password = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3306
    db = parsed.path.lstrip("/") or None
    qs = parse_qs(parsed.query)
    charset = qs.get("charset", ["utf8mb4"])[0]

    return dict(host=host, port=port, user=user, password=password, database=db, charset=charset)


_DEFAULT_DB_URL = getattr(settings, "DATABASE_URL", None) or os.getenv("DATABASE_URL")
if not _DEFAULT_DB_URL:
    raise RuntimeError("DATABASE_URL is not configured in settings or environment")

_CONN_PARAMS = parse_mysql_url(_DEFAULT_DB_URL)


def get_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=_CONN_PARAMS['host'],
        port=_CONN_PARAMS['port'],
        user=_CONN_PARAMS['user'],
        password=_CONN_PARAMS['password'],
        database=_CONN_PARAMS['database'],
        charset=_CONN_PARAMS.get('charset', 'utf8mb4'),
        autocommit=False,
    )


def get_db() -> Generator[pymysql.connections.Connection, None, None]:
    """FastAPI dependency that yields a raw pymysql connection.

    Usage in path operation:
        def endpoint(db=Depends(get_db)):
            cur = db.cursor()
            cur.execute(...)
            db.commit()
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
    

