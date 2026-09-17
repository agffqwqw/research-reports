# -*- coding: utf-8 -*-
"""Redis 客户端与研报 Hash 读写

职责：把技能包产出的 report.json（46 字段）按**唯一映射规则**写入 Redis Hash。

扁平化规则（来自技能包 `references/report-json-schema.md`，勿另立标准）：
    schema_version      -> meta_schema_version
    meta.code           -> meta_code
    dims[0].rating_code -> dim1_rating_code      (数组下标 + 1)
    dims[1].evidence    -> dim2_evidence         (整体 json.dumps 成一个字符串)
"""
from __future__ import annotations

import json

from app.config import config

# 惰性导入：本项目对 Redis 是「可用则用、不可用则降级」的定位，
# 所以顶层 import redis 会破坏这个承诺 —— 机器上没装 redis 包时，
# 连 "Redis 不可用，已降级" 这句提示都打不出来（会变成 ImportError/NameError）。
# 放进 get_redis() 内部，让降级路径真的可走。
redis = None  # type: ignore[assignment]

_pool = None

DIM_FIELDS = ("dim", "dim_name", "rating", "rating_code", "conclusion", "evidence")
META_FIELDS = ("code", "name", "market", "period", "report_type",
               "disclosure_date", "source_url", "row_count", "generated_at")


def get_redis():
    """返回 Redis 客户端。首次调用时才导入 redis 包。

    导入失败会抛 ImportError，调用方据此走降级分支（只写 SQLite）。
    """
    global _pool, redis
    if redis is None:
        import redis as _redis  # 惰性导入，见文件头说明
        redis = _redis
    if _pool is None:
        _pool = redis.ConnectionPool(
            host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB,
            decode_responses=True, max_connections=20,
        )
    return redis.Redis(connection_pool=_pool)


def report_key(code: str, period: str) -> str:
    """研报 Hash 的 key 命名规则，与 SQLite 的 report.redis_key 字段一致。"""
    return f"report:{code}:{period}"


def flatten(obj: dict) -> dict[str, str]:
    """把 report.json 压平成 46 个字段的字典。"""
    flat: dict[str, str] = {"meta_schema_version": str(obj.get("schema_version", "1.0"))}
    meta = obj.get("meta") or {}
    for f in META_FIELDS:
        flat[f"meta_{f}"] = str(meta.get(f, ""))

    for d in obj.get("dims") or []:
        n = int(d.get("order") or 0)
        if not n:
            continue
        for f in DIM_FIELDS:
            val = d.get(f)
            # evidence 是数组：整体序列化为一个字符串，不拆散
            flat[f"dim{n}_{f}"] = json.dumps(val, ensure_ascii=False) if isinstance(val, (list, dict)) else str(val or "")
    return flat


def save_report(r: redis.Redis, code: str, period: str, obj: dict) -> str:
    """写入研报 Hash，返回 key。"""
    key = report_key(code, period)
    flat = flatten(obj)
    r.hset(key, mapping=flat)
    return key


def load_report(r: redis.Redis, code: str, period: str) -> dict | None:
    """读取研报 Hash 并还原成嵌套结构（供接口直接返回给前端）。"""
    key = report_key(code, period)
    flat = r.hgetall(key)
    if not flat:
        return None

    out: dict = {
        "schema_version": flat.get("meta_schema_version", "1.0"),
        "meta": {f: flat.get(f"meta_{f}", "") for f in META_FIELDS},
        "dims": [],
    }
    for n in range(1, 7):
        if f"dim{n}_dim" not in flat:
            continue
        d: dict = {"order": n}
        for f in DIM_FIELDS:
            raw = flat.get(f"dim{n}_{f}", "")
            if f == "evidence":
                try:
                    d[f] = json.loads(raw) if raw else []
                except json.JSONDecodeError:
                    d[f] = []
            else:
                d[f] = raw
        out["dims"].append(d)
    return out
