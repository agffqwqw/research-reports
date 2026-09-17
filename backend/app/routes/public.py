# -*- coding: utf-8 -*-
"""公开查询接口（免登录）

设计要点：
- 列表走 SQLite（关系查询、分页、搜索）
- 详情优先读 Redis Hash（加速层），不可用时回退读 SQLite 的 content_json
- 只返回 is_public=1 且 is_deleted=0 的研报
"""
import json

from fastapi import APIRouter, HTTPException, Query

from app.db import db, query_all, query_one
from app.redis_client import get_redis, load_report

# 拼音首字母：装了就精准取首字母，没装也不阻断站点（降级见 _initial_of）
try:
    from pypinyin import Style as _PyStyle, lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover
    _PyStyle = None
    _lazy_pinyin = None

router = APIRouter(prefix="/api", tags=["public"])

DIM_SHORT = {"dim_name", "rating", "rating_code"}


def _initial_of(name: str | None) -> str:
    """取公司简称首字的拼音首字母，用于右侧 A-Z 索引。

    无法识别（非中文且非 ASCII 字母、空值）统一归入 "#"。
    """
    if not name:
        return "#"
    if _lazy_pinyin is not None:
        try:
            py = _lazy_pinyin(name[0], style=_PyStyle.FIRST_LETTER)
            if py and py[0] and py[0][0].isalpha():
                return py[0][0].upper()
        except Exception:  # noqa: BLE001 —— 拼音库异常不应影响列表页
            pass
    head = name[0].upper()
    return head if head.isascii() and head.isalpha() else "#"


def _codes_with_initial(conn, initial: str) -> list[str]:
    """返回首字母等于 initial 的公司代码（拼音计算在 Python 侧，公司量级很小）。"""
    rows = query_all(conn, "SELECT code, name FROM company")
    return [r["code"] for r in rows if _initial_of(r["name"]) == initial]


def _ratings_brief(dims: list) -> dict:
    """把六维度压成 {维度名: 评级}，供列表页显示色块。"""
    return {d.get("dim_name", ""): d.get("rating", "") for d in dims or []}


@router.get("/companies", summary="公司列表（含研报篇数）")
def list_companies(q: str | None = Query(None, description="按公司名或代码模糊搜索")):
    sql = """
        SELECT c.code, c.name, c.market, c.industry,
               COUNT(r.id) AS report_count
        FROM company c
        LEFT JOIN report r
               ON r.company_code = c.code AND r.is_public = 1 AND r.is_deleted = 0
    """
    params: tuple = ()
    if q:
        sql += " WHERE c.code LIKE ? OR c.name LIKE ?"
        params = (f"%{q}%", f"%{q}%")
    sql += " GROUP BY c.code ORDER BY report_count DESC, c.code ASC"

    with db() as conn:
        return {"items": query_all(conn, sql, params)}


@router.get("/initials", summary="右侧 A-Z 索引：有研报的字母及各字母篇数")
def list_initials():
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT c.code, c.name, COUNT(r.id) AS n
               FROM company c
               LEFT JOIN report r
                      ON r.company_code = c.code AND r.is_public = 1 AND r.is_deleted = 0
               GROUP BY c.code""",
        )
    counts: dict[str, int] = {}
    for row in rows:
        n = row["n"] or 0
        if not n:
            continue
        key = _initial_of(row["name"])
        counts[key] = counts.get(key, 0) + n
    return {"items": counts, "total": sum(counts.values())}


@router.get("/reports", summary="研报列表（分页 + 搜索 + 首字母筛选）")
def list_reports(
    q: str | None = Query(None, description="按公司名或股票代码搜索"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    period: str | None = Query(None, description="按报告期筛选，如 2026H1"),
    initial: str | None = Query(None, description="按公司简称拼音首字母筛选，如 B"),
):
    where = ["r.is_public = 1", "r.is_deleted = 0"]
    params: list = []

    if q:
        where.append("(c.code LIKE ? OR c.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if period:
        where.append("r.period = ?")
        params.append(period)
    if initial and initial.strip():
        want = initial.strip().upper()[:1]
        with db() as conn:
            codes = _codes_with_initial(conn, want)
        if not codes:
            return {"items": [], "page": page, "size": size, "total": 0, "pages": 0}
        where.append(f"r.company_code IN ({','.join('?' * len(codes))})")
        params += codes

    cond = " AND ".join(where)
    base = f"FROM report r JOIN company c ON c.code = r.company_code WHERE {cond}"

    with db() as conn:
        total = query_one(conn, f"SELECT COUNT(*) AS n {base}", tuple(params))["n"]
        rows = query_all(
            conn,
            f"""SELECT r.id, r.company_code AS code, c.name, c.industry,
                       r.period, r.report_type, r.disclosure_date, r.quality,
                       r.generated_at, r.content_json
                {base}
                ORDER BY r.disclosure_date DESC, r.id DESC
                LIMIT ? OFFSET ?""",
            tuple(params) + (size, (page - 1) * size),
        )

    items = []
    for row in rows:
        try:
            dims = (json.loads(row.pop("content_json") or "{}") or {}).get("dims") or []
        except json.JSONDecodeError:
            dims = []
        row["ratings"] = _ratings_brief(dims)
        items.append(row)

    return {
        "items": items,
        "page": page,
        "size": size,
        "total": total,
        "pages": (total + size - 1) // size if size else 0,
    }


@router.get("/reports/{code}/{period}", summary="研报详情（六维度总表）")
def get_report(code: str, period: str):
    # 1) 先确认这条记录可见（同时拿到 SQLite 里的全量备份）
    with db() as conn:
        row = query_one(
            conn,
            """SELECT r.id, r.quality, r.is_public, r.is_deleted, r.content_json,
                      c.name, c.industry
               FROM report r JOIN company c ON c.code = r.company_code
               WHERE r.company_code = ? AND r.period = ?""",
            (code, period),
        )
    if not row or not row["is_public"] or row["is_deleted"]:
        raise HTTPException(status_code=404, detail="研报不存在或未公开")

    # 2) 优先用 Redis 加速层
    data = None
    try:
        data = load_report(get_redis(), code, period)
    except Exception:  # noqa: BLE001
        data = None

    source = "redis"
    if not data:
        source = "sqlite"
        try:
            data = json.loads(row["content_json"] or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="研报内容损坏")

    data["company"] = {"code": code, "name": row["name"], "industry": row["industry"]}
    data["quality"] = row["quality"]
    data["_source"] = source  # 便于开发期确认走了哪条路径
    return data
