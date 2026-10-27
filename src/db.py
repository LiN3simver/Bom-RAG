"""PLM 來源庫（MS SQL）的唯讀連線。

BOM 不落地，每次查詢直接對來源庫下遞迴 CTE：最大單張展開約 1,500 列只需 0.06s，
落地換不到速度，卻會讓資料過期。

兩種逾時都要設：
    connect(timeout=)   登入逾時
    conn.timeout        查詢逾時（pyodbc 掛在 Connection 上，預設 0 = 永不逾時）

pyodbc 延遲 import，未安裝 ODBC driver 時服務仍可啟動並回報 degraded。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

from .guard import LOGIN_TIMEOUT, STATEMENT_TIMEOUT

load_dotenv()


def mssql(*, login_timeout: int = LOGIN_TIMEOUT):
    """建立唯讀連線。`readonly=True` 只是提示，真正的保護來自伺服器端權限。"""
    import pyodbc

    parts = [
        f"DRIVER={{{os.environ.get('MSSQL_DRIVER', 'ODBC Driver 18 for SQL Server')}}}",
        f"SERVER={os.environ['MSSQL_HOST']}",
        f"DATABASE={os.environ['MSSQL_DB']}",
        f"UID={os.environ['MSSQL_USER']}",
        f"PWD={os.environ['MSSQL_PASSWORD']}",
    ]
    if os.environ.get("MSSQL_TRUST_CERT", "yes").lower() in ("yes", "true", "1"):
        parts.append("TrustServerCertificate=yes")
    conn = pyodbc.connect(";".join(parts), readonly=True, timeout=login_timeout)
    conn.timeout = STATEMENT_TIMEOUT
    return conn


def cursor(conn, *, timeout: int = STATEMENT_TIMEOUT):
    """取 cursor 並設定查詢逾時。一律用這支，不要直接 conn.cursor()。"""
    conn.timeout = timeout
    return conn.cursor()


def available() -> tuple[bool, str]:
    """來源庫是否可連線。給健康檢查用，永遠不丟例外。"""
    try:
        mssql(login_timeout=5).close()
        return True, f"{os.environ.get('MSSQL_HOST')} / {os.environ.get('MSSQL_DB')} 可連線"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
