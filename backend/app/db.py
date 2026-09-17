# -*- coding: utf-8 -*-
"""SQLite 连接管理

设计要点：
1. **开启 WAL 模式** —— 允许读写并发（读不阻塞写），对"访客查询 + 后台写入"
   这种场景是必需的。默认的 rollback journal 模式下读会阻塞写。
2. **row_factory = sqlite3.Row** —— 查询结果可按列名访问，像字典一样用。
3. **开启外键约束** —— SQLite 默认不检查外键，必须显式打开。
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import config


def get_conn() -> sqlite3.Connection:
    """创建一个新连接。调用方负责关闭，或使用下方的 db() 上下文管理器。"""
    db_path = Path(config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def db():
    """推荐用法：

        with db() as conn:
            conn.execute("INSERT ...")
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_one(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def query_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]
