# -*- coding: utf-8 -*-
"""数据库结构演进：增量列迁移

为什么单独一个模块：
    `CREATE TABLE IF NOT EXISTS` 只在表**不存在**时生效；表已存在时新加的列
    不会自动出现。线上库不能重建（会丢数据），所以必须用 ALTER TABLE 逐列补齐。

    这里有两条使用路径，共用同一份定义，避免"脚本补了但服务没补"的错位：
      - `scripts/init_db.py`  部署时执行（含建表）
      - `app/main.py` 启动时自动调用（兜住"忘了跑迁移"的情况）
"""

# 格式：{表名: [(列名, 列定义), ...]}
# 新增字段时在这里加一行即可，重复执行无副作用。
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "user": [
        ("can_edit_report",   "INTEGER DEFAULT 0"),
        ("can_delete_report", "INTEGER DEFAULT 0"),
        ("pwd_change_token",  "TEXT"),
        ("pwd_change_hash",   "TEXT"),
        ("pwd_change_at",     "TEXT"),
    ],
    "permission_request": [
        # 申请范围：unlimited / edit / delete / all
        ("scope", "TEXT DEFAULT 'unlimited'"),
    ],
    "notification": [
        # 已有队列在跑时补上重试计数字段
        ("attempts", "INTEGER DEFAULT 0"),
    ],
}


def _existing_columns(conn, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    except Exception:  # noqa: BLE001
        return set()


def apply_migrations(conn) -> list[str]:
    """补缺失的列。返回本次新增的「表.列」清单（空表示无需迁移）。"""
    added: list[str] = []
    for table, cols in MIGRATIONS.items():
        present = _existing_columns(conn, table)
        if not present:
            continue  # 表还不存在，建表时会带上全部列
        for name, ddl in cols:
            if name not in present:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
                added.append("%s.%s" % (table, name))
    if added:
        conn.commit()
    return added


def check_schema(conn) -> list[str]:
    """校验关键列是否齐全，返回缺失清单。"""
    missing: list[str] = []
    for table, cols in MIGRATIONS.items():
        present = _existing_columns(conn, table)
        if not present:
            missing.append("%s（整张表缺失）" % table)
            continue
        for name, _ddl in cols:
            if name not in present:
                missing.append("%s.%s" % (table, name))
    return missing
