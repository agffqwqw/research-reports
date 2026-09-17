# -*- coding: utf-8 -*-
"""FastAPI 应用入口

当前为第一阶段脚手架：只提供健康检查与配置自检，业务路由在后续步骤挂载。
"""
import sys

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import config
from app.db import db, get_conn, query_one
from app.ratelimit import limiter, rate_limit_handler
from app.redis_client import get_redis
from app.routes import (
    auth,
    me,
    notifications,
    permissions,
    public,
    reports_admin,
    securities,
    tasks,
)
from app.schema import apply_migrations, check_schema

app = FastAPI(
    title="上市公司研报站点",
    description="基于巨潮资讯网公开信息、由 AI 按六维度框架生成的上市公司简易研报",
    version="0.1.0",
    # 接口文档端点由 ENABLE_DOCS 控制，**默认关闭**。
    #
    # 为什么：/docs、/redoc、/openapi.json 会完整列出所有接口的路径、参数结构
    # 与所需权限 —— 对扫描者等于一张现成的 API 地图，能省掉大量探测成本。
    # 生产环境（对外）必须关；本机开发在 app.env 里设 ENABLE_DOCS=true 即可恢复。
    #
    # 注意：关闭文档**不等于**隐藏接口。前端打包的 JS 里本就含全部 /api 路径，
    # 真正的防护是鉴权（JWT / WORKER_TOKEN）与限流，这里只是减少噪音目标。
    docs_url="/docs" if config.ENABLE_DOCS else None,
    redoc_url="/redoc" if config.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if config.ENABLE_DOCS else None,
)

# 文档开关的启动日志 —— 部署后一眼能看出当前是开还是关
print(
    "[docs] 接口文档 %s（/docs、/redoc、/openapi.json）"
    % ("已开启 —— 仅限本机开发使用" if config.ENABLE_DOCS else "已关闭"),
    flush=True,
)

# ---------------------------------------------------------------- 接口限流
# 应用层限流（slowapi）。与网络层的 fail2ban 互补：
#   fail2ban  读 Caddy 日志封恶意 IP，被封的请求到不了应用（省资源，粗粒度）
#   slowapi   按接口给配额，能防住「正常 IP 的滥用」与慢速爆破（细粒度）
# 计数走 Redis（重启不丢），Redis 不可用时自动降级为内存计数。
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
print(
    "[ratelimit] 已启用 · 存储 redis://%s:%d/%d（Redis 不可用时降级为内存计数）"
    % (config.REDIS_HOST, config.REDIS_PORT, config.REDIS_DB),
    flush=True,
)


@app.on_event("startup")
def _ensure_schema() -> None:
    """启动时自动补列迁移。

    这样只要代码更新了，服务重启就会把新字段补上 —— 不依赖「记得跑 init_db.py」。
    ALTER TABLE ADD COLUMN 是幂等的，重复执行无副作用。
    失败不阻断启动（否则一次迁移问题会让整站起不来），但会在日志里明确报出来。
    """
    try:
        with db() as conn:
            added = apply_migrations(conn)
        if added:
            print("[schema] 已自动补齐字段: %s" % ", ".join(added), flush=True)
        else:
            gap = None
            with db() as conn:
                gap = check_schema(conn)
            if gap:
                print("[schema] ⚠ 仍缺失字段: %s" % ", ".join(gap), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("[schema] ⚠ 启动自检失败: %s: %s" % (type(exc).__name__, exc), flush=True)


app.include_router(public.router)
app.include_router(securities.router)
app.include_router(auth.router)
app.include_router(me.router)
app.include_router(tasks.router)
app.include_router(permissions.router)
app.include_router(reports_admin.router)
app.include_router(notifications.router)


@app.get("/health", summary="健康检查（供监测与 Caddy 探活使用）")
def health():
    """轻量探活：检查 SQLite 与 Redis 是否可用。

    刻意保持轻量 —— 监测脚本每 5 分钟调一次，不能有重查询。
    """
    result = {"status": "ok", "sqlite": "unknown", "redis": "unknown"}

    try:
        with db() as conn:
            row = query_one(conn, "SELECT 1 AS ok")
            result["sqlite"] = "ok" if row and row.get("ok") == 1 else "fail"
    except Exception as exc:  # noqa: BLE001
        result["sqlite"] = f"fail: {type(exc).__name__}"
        result["status"] = "degraded"

    try:
        get_redis().ping()
        result["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["redis"] = f"fail: {type(exc).__name__}"
        result["status"] = "degraded"

    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=code)


@app.get("/api/config-check", summary="配置自检（仅启动阶段使用，后续应加保护）")
def config_check():
    """返回配置问题清单，便于部署后立即发现漏配的环境变量。"""
    problems = config.check()
    return {
        "ok": not problems,
        "base_url": config.BASE_URL,
        "db_path": config.DB_PATH,
        "redis": f"{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB}",
        "mail_enabled": config.MAIL_ENABLED,
        "problems": problems,
    }


@app.get("/", summary="根路由")
def root():
    return {
        "name": "上市公司研报站点",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":
    import uvicorn

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
