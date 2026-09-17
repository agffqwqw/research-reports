# -*- coding: utf-8 -*-
"""初始化 SQLite 数据库（幂等，可重复执行）

用法：
    python scripts/init_db.py              # 建表
    python scripts/init_db.py --drop       # 先删表再建（⚠️ 会清空数据）
    python scripts/init_db.py --check      # 只检查当前表结构，不修改

设计依据：《研报站点-数据结构设计.md》
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config  # noqa: E402
from app.db import get_conn      # noqa: E402

TABLES = {
    "company": """
        CREATE TABLE IF NOT EXISTS company (
            code        TEXT PRIMARY KEY,          -- 股票代码，如 002594
            name        TEXT NOT NULL,             -- 公司简称
            market      TEXT,                      -- SZ / SH / BJ
            industry    TEXT,                      -- 所属行业（手工补或后续导入）
            created_at  TEXT NOT NULL
        )
    """,
    "report": """
        CREATE TABLE IF NOT EXISTS report (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code    TEXT NOT NULL REFERENCES company(code),
            period          TEXT NOT NULL,         -- 2026H1 / 2026FY / ...
            report_type     TEXT,                  -- 半年报 / 年报
            disclosure_date TEXT,                  -- 披露日 YYYY-MM-DD
            source_url      TEXT NOT NULL,         -- 巨潮公告 PDF 地址（一手来源）
            schema_version  TEXT DEFAULT '1.0',    -- 技能包契约版本
            redis_key       TEXT NOT NULL,         -- 指向 Redis Hash（加速层）
            content_json    TEXT,                  -- 全量 report.json（唯一事实源）
            quality         TEXT DEFAULT 'deep',   -- deep=深度版 / quick=快速版
            is_public       INTEGER DEFAULT 1,     -- 是否公开
            is_deleted      INTEGER DEFAULT 0,     -- 软删除标记
            generated_at    TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT,
            UNIQUE(company_code, period)
        )
    """,
    "user": """
        CREATE TABLE IF NOT EXISTS user (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            email          TEXT NOT NULL UNIQUE,
            password_hash  TEXT NOT NULL,
            role           TEXT DEFAULT 'user',    -- user / admin
            email_verified INTEGER DEFAULT 0,
            verify_token   TEXT,
            can_generate   INTEGER DEFAULT 1,      -- 注册即可生成，无需批准
            quota_left     INTEGER DEFAULT 2,      -- 限 2 次
            is_unlimited   INTEGER DEFAULT 0,      -- 权限①：无限次生成（需管理员邮箱批准）
            can_edit_report   INTEGER DEFAULT 0,   -- 权限②：修改研报内容
            can_delete_report INTEGER DEFAULT 0,   -- 权限③：删除研报
            pwd_change_token  TEXT,                -- 改密待确认 token（发到本人邮箱）
            pwd_change_hash   TEXT,                -- 待生效的新密码哈希（确认后才写入 password_hash）
            pwd_change_at     TEXT,                -- 改密请求时间（用于 30 分钟有效期判断）
            created_at     TEXT NOT NULL,
            last_login_at  TEXT
        )
    """,
    "generation_task": """
        CREATE TABLE IF NOT EXISTS generation_task (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES user(id),
            company_code TEXT NOT NULL,
            quality      TEXT DEFAULT 'deep',      -- 默认深度版
            status       TEXT DEFAULT 'pending',   -- pending/processing/success/failed
            error_msg    TEXT,
            payload      TEXT,                     -- 回传的原始 JSON（可选留档）
            created_at   TEXT NOT NULL,
            started_at   TEXT,
            finished_at  TEXT
        )
    """,
    "permission_request": """
        CREATE TABLE IF NOT EXISTS permission_request (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES user(id),
            request_type  TEXT DEFAULT 'unlimited',
            status        TEXT DEFAULT 'pending',  -- pending/approved/rejected
            approve_token TEXT,
            created_at    TEXT NOT NULL,
            decided_at    TEXT
        )
    """,
    "notification": """
        CREATE TABLE IF NOT EXISTS notification (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            kind       TEXT NOT NULL,              -- permission_apply / service_alert / ...
            title      TEXT NOT NULL,
            body       TEXT,                       -- 正文（纯文本，渠道自行决定排版）
            link       TEXT,                       -- 相关链接（如批准链接）
            ref_id     INTEGER,                    -- 关联业务 id，如 permission_request.id
            status     TEXT DEFAULT 'pending',     -- pending / sending / sent / failed
            attempts   INTEGER DEFAULT 0,          -- 已尝试发送次数（有上限，防死循环）
            error_msg  TEXT,
            created_at TEXT NOT NULL,
            sent_at    TEXT
        )
    """,
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_report_company  ON report(company_code)",
    "CREATE INDEX IF NOT EXISTS idx_report_period   ON report(period)",
    "CREATE INDEX IF NOT EXISTS idx_report_public   ON report(is_public, is_deleted)",
    "CREATE INDEX IF NOT EXISTS idx_task_status     ON generation_task(status)",
    "CREATE INDEX IF NOT EXISTS idx_task_user       ON generation_task(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_perm_status     ON permission_request(status)",
    "CREATE INDEX IF NOT EXISTS idx_notif_status    ON notification(status, id)",
]

# ============================================================
# 列迁移定义统一放在 app/schema.py —— 服务启动时也会调用同一份，
# 避免「脚本补了、服务没补」的错位。
# ============================================================
from app.schema import apply_migrations, check_schema  # noqa: E402

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化研报站点数据库")
    ap.add_argument("--drop", action="store_true", help="先删除所有表（清空数据）")
    ap.add_argument("--check", action="store_true", help="只检查表结构，不修改")
    args = ap.parse_args()

    conn = get_conn()
    try:
        if args.check:
            print("数据库：%s\n" % config.DB_PATH)
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            for t in TABLES:
                mark = "存在" if t in names else "缺失"
                cnt = ""
                if t in names:
                    cnt = "  行数=%d" % conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                print("  %-18s %s%s" % (t, mark, cnt))
            extra = [n for n in names if n not in TABLES and not n.startswith("sqlite_")]
            if extra:
                print("\n  其他表：%s" % ", ".join(extra))

            # 列迁移状态
            gap = check_schema(conn)
            print()
            if gap:
                print("  待补的列（执行不带 --check 即可自动补上）：")
                for g in gap:
                    print("    - %s" % g)
            else:
                print("  ✅ 新增列均已就位")
            return 0

        if args.drop:
            print("⚠️  正在删除已有表…")
            for t in reversed(list(TABLES)):
                conn.execute("DROP TABLE IF EXISTS %s" % t)
            conn.commit()

        print("数据库：%s\n" % config.DB_PATH)
        for name, sql in TABLES.items():
            conn.execute(sql)
            print("  建表 %-18s ok" % name)
        for sql in INDEXES:
            conn.execute(sql)
        print("\n  索引 %d 个 ok" % len(INDEXES))

        # ---------- 列迁移（给已存在的表补新字段）----------
        added = apply_migrations(conn)
        if added:
            print("\n  列迁移：新增 %d 个字段" % len(added))
            for a in added:
                print("    + %s" % a)
        else:
            print("\n  列迁移：无需变更（字段已齐）")

        conn.commit()

        # 结构自检
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        missing = [t for t in TABLES if t not in names]
        if missing:
            print("\n❌ 缺失表：%s" % ", ".join(missing))
            return 1

        gap = check_schema(conn)
        if gap:
            print("\n❌ 列仍缺失：%s" % ", ".join(gap))
            return 1

        print("\n✅ 初始化完成，共 %d 张表、%d 个索引" % (len(TABLES), len(INDEXES)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
