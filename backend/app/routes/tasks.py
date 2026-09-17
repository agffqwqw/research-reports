# -*- coding: utf-8 -*-
"""任务队列 + 次数扣减（B7 / B9）

流程：
    用户 POST /api/tasks 提交 → 建 task(pending)
        ↓
    本机 worker GET /api/worker/tasks/next 拉取 → 标记 processing
        ↓
    本机生成完成 POST /api/worker/tasks/{id}/result
        ├─ success → 写库 + **扣次数** + task=success
        └─ failed  → task=failed + 原因，**不扣次数**

次数扣减策略（B9）：
    - **成功才扣**，失败不消耗 —— 这正是「失败的生成不应扣次数」的实现
    - 为防并发超额，提交时按「剩余配额 − 进行中任务数」判断可用容量
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import config
from app.db import db, query_all, query_one
from app.deps import get_current_user, remaining_capacity, require_worker
from app.ratelimit import LIMITS, limiter
from app.redis_client import get_redis, save_report
from app.services.cninfo import latest_periodic_report, lookup

router = APIRouter(prefix="/api", tags=["tasks"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SubmitIn(BaseModel):
    query: str = Field(min_length=1, max_length=32, description="股票代码或公司名称")
    quality: str = Field(default="deep", pattern="^(deep|quick)$")


class ResultIn(BaseModel):
    status: str = Field(pattern="^(success|failed)$")
    error_msg: str | None = None
    report: dict | None = Field(default=None, description="success 时必传，需符合技能包 report.json 契约")


# ============================================================
# 用户侧
# ============================================================
@router.get("/tasks", summary="我的任务列表（含公司名与结果报告期）")
def my_tasks(user: dict = Depends(get_current_user)):
    """返回任务列表，供「新增研报」页展示进度。

    额外带上公司名与**已生成研报的报告期** —— 否则前端只知道「成功了」，
    却不知道成功之后去哪看；用户的抱怨正是「不知道出结果了」。
    报告期用相关子查询取最新一期，避免一公司多期时行数翻倍。
    """
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT t.id, t.company_code, t.quality, t.status, t.error_msg,
                      t.created_at, t.started_at, t.finished_at,
                      c.name AS company_name,
                      (SELECT r.period FROM report r
                        WHERE r.company_code = t.company_code AND r.is_deleted = 0
                        ORDER BY r.disclosure_date DESC, r.id DESC LIMIT 1) AS period
               FROM generation_task t
               LEFT JOIN company c ON c.code = t.company_code
               WHERE t.user_id = ?
               ORDER BY t.id DESC LIMIT 50""",
            (user["id"],),
        )

    for r in rows:
        # 前端据此决定是否显示「查看研报」链接
        r["has_report"] = bool(r.get("period")) and r["status"] == "success"

    return {"items": rows, "count": len(rows)}


@router.post("/tasks", summary="提交生成任务")
@limiter.limit(LIMITS["task_create"])
def submit_task(request: Request, data: SubmitIn,
                user: dict = Depends(get_current_user)):
    """限流按 IP —— 生成任务最终落到本机 Worker 上跑（抓公告 + AI 评判），
    不限流的话，一个脚本就能把队列和本机 CPU 一起压满。

    配额本身（quota_left）只约束**单账号**，挡不住「多注册几个号一起刷」，
    所以这里用 IP 维度补上。"""
    # 注册后即可生成，不再要求邮箱验证（邮箱验证改为可选项）
    if not user.get("can_generate") and not user.get("is_unlimited"):
        raise HTTPException(403, "当前账号没有生成权限")

    # 1) 证券校验（B4）：A 股唯一 + 非银行券商
    sec = lookup(data.query)
    if not sec.get("ok"):
        raise HTTPException(400, sec.get("detail", "证券校验未通过"))
    code = sec["code"]

    with db() as conn:
        # 2) 可用容量 = 剩余配额 − 进行中任务数
        pending = query_one(
            conn,
            """SELECT COUNT(*) AS n FROM generation_task
               WHERE user_id = ? AND status IN ('pending','processing')""",
            (user["id"],),
        )["n"]
        if remaining_capacity(user, pending) <= 0:
            raise HTTPException(
                429,
                f"生成次数不足（剩余 {user.get('quota_left')} 次，进行中 {pending} 个）。"
                "可申请无限次权限，或等待进行中的任务完成。",
            )

        # 3) 防重复生成：比对「巨潮最新一期定期报告的披露日期」
        #    与「库里最近一篇研报的披露日期」
        #    - 日期相同 → 是同一期，已经生成过 → 复用，不重复生成
        #    - 日期不同 → 公司出了新报告 → 允许生成
        #    - 巨潮查不到（网络/未披露）→ 退回保守策略：已有研报就复用
        existing = query_one(
            conn,
            """SELECT id, period, disclosure_date FROM report
               WHERE company_code = ? AND is_deleted = 0
               ORDER BY disclosure_date DESC LIMIT 1""",
            (code,),
        )

        latest = latest_periodic_report(code, sec.get("org_id", ""))
        dup_reason = ""
        if existing:
            if latest.get("ok"):
                if (existing.get("disclosure_date") or "") == latest["disclosure_date"]:
                    dup_reason = "已生成过最新一期（%s，披露日 %s）" % (
                        latest.get("period") or existing["period"], latest["disclosure_date"])
            else:
                dup_reason = "暂无法确认是否有新报告，先复用已有研报"

        if dup_reason:
            return {
                "ok": True,
                "reused": True,
                "company_code": code,
                "company_name": sec["name"],
                "period": (latest.get("period") or existing["period"]) if existing else "",
                "message": f"该公司{dup_reason}，已直接复用，未消耗生成次数",
            }

        cur = conn.execute(
            """INSERT INTO company (code, name, market, industry, created_at)
               VALUES (?, ?, ?, '', ?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, market=excluded.market""",
            (code, sec["name"], sec.get("market", ""), now()),
        )
        cur = conn.execute(
            """INSERT INTO generation_task
                 (user_id, company_code, quality, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (user["id"], code, data.quality, now()),
        )
        task_id = cur.lastrowid

    return {
        "ok": True,
        "reused": False,
        "task_id": task_id,
        "company_code": code,
        "company_name": sec["name"],
        "market": sec.get("market", ""),
        "message": "任务已进入队列，本机生成完成后即可查看",
        # 让前端能说明「为什么这次会真的生成」——即检测到了新一期报告
        "latest_report": {
            "period": latest.get("period", ""),
            "disclosure_date": latest.get("disclosure_date", ""),
            "title": latest.get("title", ""),
        } if latest.get("ok") else None,
    }


@router.get("/tasks/{task_id}", summary="查询任务状态")
def task_status(task_id: int, user: dict = Depends(get_current_user)):
    with db() as conn:
        row = query_one(
            conn,
            """SELECT t.id, t.company_code, t.quality, t.status, t.error_msg,
                      t.created_at, t.started_at, t.finished_at,
                      r.period
               FROM generation_task t
               LEFT JOIN report r ON r.company_code = t.company_code AND r.is_deleted = 0
               WHERE t.id = ? AND t.user_id = ?""",
            (task_id, user["id"]),
        )
    if not row:
        raise HTTPException(404, "任务不存在")
    return row


# ============================================================
# Worker 侧（需 X-Worker-Token）
# ============================================================
@router.get("/worker/tasks/peek", summary="[worker] 只读查看队列（不改变状态）")
def worker_peek(_: bool = Depends(require_worker)):
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT t.id, t.company_code, t.quality, t.status, t.created_at,
                      c.name AS company_name, c.market
               FROM generation_task t
               LEFT JOIN company c ON c.code = t.company_code
               WHERE t.status IN ('pending','processing')
               ORDER BY t.id ASC LIMIT 20""",
        )
        pending = query_one(
            conn, "SELECT COUNT(*) AS n FROM generation_task WHERE status='pending'")["n"]
    return {"ok": True, "pending_count": pending, "items": rows}


@router.get("/worker/tasks/next", summary="[worker] 拉取一个待处理任务")
def worker_next(_: bool = Depends(require_worker)):
    with db() as conn:
        task = query_one(
            conn,
            """SELECT id, company_code, quality, created_at
               FROM generation_task WHERE status = 'pending'
               ORDER BY id ASC LIMIT 1""",
        )
        if not task:
            return {"ok": True, "task": None}

        conn.execute(
            "UPDATE generation_task SET status='processing', started_at=? WHERE id=?",
            (now(), task["id"]),
        )
        company = query_one(
            conn, "SELECT name, market FROM company WHERE code = ?",
            (task["company_code"],),
        )

    task["company_name"] = (company or {}).get("name", "")
    task["market"] = (company or {}).get("market", "")
    return {"ok": True, "task": task}


@router.post("/worker/tasks/{task_id}/result", summary="[worker] 回传生成结果")
def worker_result(task_id: int, data: ResultIn, _: bool = Depends(require_worker)):
    with db() as conn:
        task = query_one(
            conn,
            "SELECT id, user_id, company_code, quality, status FROM generation_task WHERE id = ?",
            (task_id,),
        )
        if not task:
            raise HTTPException(404, "任务不存在")
        if task["status"] == "success":
            return {"ok": True, "already": True, "message": "该任务已完成，忽略重复回传"}

        # ---------- 失败：只标记，**不扣次数** ----------
        if data.status == "failed":
            conn.execute(
                """UPDATE generation_task
                   SET status='failed', error_msg=?, finished_at=?
                   WHERE id=?""",
                ((data.error_msg or "生成失败")[:500], now(), task_id),
            )
            return {"ok": True, "charged": False, "message": "已记录失败，未消耗生成次数"}

        # ---------- 成功：写库 + 扣次数 ----------
        rep = data.report or {}
        meta = rep.get("meta") or {}
        if not meta.get("code") or not meta.get("period"):
            raise HTTPException(400, "report 缺 meta.code / meta.period")
        if len(rep.get("dims") or []) != 6:
            raise HTTPException(400, "report.dims 必须为 6 个维度")

        code = meta["code"]
        period = meta["period"]
        rkey = f"report:{code}:{period}"

        # 公司表（技能包不产出行业，留空待补）
        conn.execute(
            """INSERT INTO company (code, name, market, industry, created_at)
               VALUES (?, ?, ?, '', ?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market""",
            (code, meta.get("name", ""), meta.get("market", ""), now()),
        )

        # 研报表（content_json 为唯一事实源）
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
             meta.get("source_url", ""), rep.get("schema_version", "1.0"),
             rkey, json.dumps(rep, ensure_ascii=False), task["quality"],
             meta.get("generated_at", ""), now(), now()),
        )

        # 扣次数（仅成功时，且无限权限不扣）
        u = query_one(conn, "SELECT is_unlimited, quota_left FROM user WHERE id = ?", (task["user_id"],))
        charged = False
        if u and not u["is_unlimited"]:
            conn.execute(
                "UPDATE user SET quota_left = MAX(0, quota_left - 1) WHERE id = ?",
                (task["user_id"],),
            )
            charged = True

        conn.execute(
            """UPDATE generation_task
               SET status='success', finished_at=?,
                   payload = NULL
               WHERE id=?""",
            (now(), task_id),
        )

        left = query_one(conn, "SELECT quota_left, is_unlimited FROM user WHERE id = ?", (task["user_id"],))

    # Redis 加速层（不可用则跳过，可从 SQLite 重建）
    redis_ok = False
    try:
        save_report(get_redis(), code, period, rep)
        redis_ok = True
    except Exception:  # noqa: BLE001
        redis_ok = False

    return {
        "ok": True,
        "charged": charged,
        "redis": redis_ok,
        "company_code": code,
        "period": period,
        "quota_left": (left or {}).get("quota_left"),
        "is_unlimited": bool((left or {}).get("is_unlimited")),
    }
