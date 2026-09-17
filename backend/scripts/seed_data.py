# -*- coding: utf-8 -*-
"""灌入初始数据（D1 研报入库 + D2 公司表）

用法：
    python scripts/seed_data.py                  # 从 ../reports 灌入
    python scripts/seed_data.py --dir <路径>      # 指定目录
    python scripts/seed_data.py --no-redis       # 只写 SQLite，跳过 Redis

设计说明：
    SQLite 的 report.content_json 是**唯一事实源**（全量 report.json）。
    Redis Hash 是**加速层**（46 字段扁平化），随时可从 SQLite 重建。
    本机没有 Redis 时自动跳过，不影响数据入库。
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config          # noqa: E402
from app.db import get_conn            # noqa: E402
# ⚠️ 这两个都要导入：get_redis 用于探测连接，save_report 用于写 Hash。
#    之前漏了 get_redis，异常被 except 吞掉，导致「Redis 可用时也从没写过」。
from app.redis_client import get_redis, save_report  # noqa: E402

def _default_dir() -> Path:
    """按优先级找一个存在的 reports/ 目录。

    1) 项目内 research-reports/reports/  ← 部署后的标准位置（进 tar 包一起上传）
    2) 工作区根目录的 reports/           ← 本机开发时的位置
    这样本机与服务器都能直接跑，不需要手工传 --dir。
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "reports",        # research-reports/reports
        here.parent.parent.parent.parent / "reports",  # 工作区根/reports（本机）
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("report_*.json")):
            return c
    return candidates[0]


DEFAULT_DIR = _default_dir()

# 公司补充信息：技能包不产出行业，这里手工维护（后续可批量导入或被数据源替换）
COMPANY_EXTRA = {
    "002594": {"industry": "汽车整车"},
    "300750": {"industry": "动力电池·储能电池"},
    "300502": {"industry": "光模块·光通信"},
    "600519": {"industry": "白酒"},
    "600703": {"industry": "LED芯片·化合物半导体"},
}

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description="灌入初始研报数据")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="研报 JSON 所在目录")
    ap.add_argument("--no-redis", action="store_true", help="跳过 Redis 写入")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "report_*.json")))
    if not files:
        print("!! 在 %s 下没找到 report_*.json" % args.dir)
        return 1

    # Redis 可用性探测（不可用则降级，不算失败）
    # 注意：这里的宽 except 是有意的 —— Redis 挂了不该阻断建库。
    # 但必须把原始错误打全，否则编码类错误（如名字没导入）会被静默吞掉。
    r = None
    if not args.no_redis:
        try:
            r = get_redis()
            r.ping()
            print("Redis：已连接 %s:%s/%s" % (config.REDIS_HOST, config.REDIS_PORT, config.REDIS_DB))
        except Exception as exc:  # noqa: BLE001
            r = None
            print("Redis：不可用 [%s.%s] %s" % (type(exc).__module__, type(exc).__name__, exc))
            print("       → 仅写入 SQLite；Redis 加速层可稍后重建")
    else:
        print("Redis：已按参数跳过")

    print("\n待处理 %d 份研报\n" % len(files))

    conn = get_conn()
    ok = 0
    try:
        for f in files:
            obj = json.load(open(f, encoding="utf-8"))
            meta = obj.get("meta") or {}
            code = meta.get("code")
            period = meta.get("period")
            if not code or not period:
                print("  跳过 %s（缺 code/period）" % os.path.basename(f))
                continue

            # --- D2 公司表 ---
            extra = COMPANY_EXTRA.get(code, {})
            conn.execute(
                """INSERT INTO company (code, name, market, industry, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name,
                     market=excluded.market,
                     industry=COALESCE(NULLIF(excluded.industry,''), company.industry)""",
                (code, meta.get("name", ""), meta.get("market", ""),
                 extra.get("industry", ""), now()),
            )

            # --- D1 研报表（content_json 为唯一事实源）---
            rkey = "report:%s:%s" % (code, period)
            conn.execute(
                """INSERT INTO report
                     (company_code, period, report_type, disclosure_date, source_url,
                      schema_version, redis_key, content_json, quality,
                      is_public, is_deleted, generated_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,1,0,?,?,?)
                   ON CONFLICT(company_code, period) DO UPDATE SET
                     report_type=excluded.report_type,
                     disclosure_date=excluded.disclosure_date,
                     source_url=excluded.source_url,
                     schema_version=excluded.schema_version,
                     redis_key=excluded.redis_key,
                     content_json=excluded.content_json,
                     quality=excluded.quality,
                     generated_at=excluded.generated_at,
                     updated_at=excluded.updated_at""",
                (code, period, meta.get("report_type", ""), meta.get("disclosure_date", ""),
                 meta.get("source_url", ""), obj.get("schema_version", "1.0"),
                 rkey, json.dumps(obj, ensure_ascii=False), "deep",
                 meta.get("generated_at", ""), now(), now()),
            )

            # --- Redis 加速层 ---
            rmsg = "redis 跳过"
            if r is not None:
                try:
                    save_report(r, code, period, obj)
                    rmsg = "redis ok"
                except Exception as exc:  # noqa: BLE001
                    rmsg = "redis 失败(%s)" % type(exc).__name__

            dims = obj.get("dims") or []
            print("  %s %-8s %-8s  %d 维度  %s" %
                  (code, meta.get("name", ""), period, len(dims), rmsg))
            ok += 1

        conn.commit()

        # 汇总
        n_co = conn.execute("SELECT COUNT(*) FROM company").fetchone()[0]
        n_rp = conn.execute("SELECT COUNT(*) FROM report").fetchone()[0]
        print("\n✅ 入库完成：company %d 行 / report %d 行（本次处理 %d 份）" % (n_co, n_rp, ok))

        if r is not None:
            try:
                keys = sorted(k for k in r.scan_iter("report:*"))
                print("   Redis 中研报 Hash：%d 个" % len(keys))
                if keys:
                    sample = r.hlen(keys[0])
                    print("   抽样 %s 字段数 = %d（应为 46）" % (keys[0], sample))
            except Exception:  # noqa: BLE001
                pass
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
