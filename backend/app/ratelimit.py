# -*- coding: utf-8 -*-
"""接口限流（应用层 · slowapi）

为什么要有**两层**限流，而不是只做一层：

| 层 | 手段 | 拦在哪里 | 擅长什么 |
|:---|:---|:---|:---|
| 网络层 | fail2ban + Caddy 日志 | 到不了应用 | 恶意 IP 的高频冲击，**不消耗应用资源** |
| 应用层 | slowapi（本模块） | 进入业务逻辑前 | 精确频率控制，能防住「正常 IP 的滥用」 |

两者互补：fail2ban 是粗粒度、跨分钟的封禁；slowapi 是细粒度、按接口的配额。
单靠任何一层都有明显缺口 —— 例如 fail2ban 挡不住「每分钟 9 次、永不触发阈值」的慢速爆破。

---

## 存储选择

用 **Redis**（复用站点已有的 DB 1），而不是内存：
- 内存后端一重启计数就清零，等于给攻击者留了「重启即重置」的后门
- 但 Redis 不可用时**自动降级为内存计数**（`in_memory_fallback_enabled`）：
  限流是防护层、不是核心功能，宁可少一层，也不能因为限流组件挂了导致整站不可用。

## 为什么 key 用 IP 而不是账号

登录接口在**验证身份之前**执行，此时还不知道「你是谁」，只可能是按 IP 限。
对已登录接口（如提交任务）另可叠加按用户限流，但本模块先用 IP 覆盖所有关键点 ——
简单、无死角，且 uvicorn 的 `--proxy-headers` 已保证能拿到 Caddy 转发的真实 IP。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import config

# ---------------------------------------------------------------- 限流器
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="redis://%s:%d/%d" % (config.REDIS_HOST, config.REDIS_PORT, config.REDIS_DB),
    # Redis 连不上时退回内存计数 —— 服务可用性优先
    in_memory_fallback_enabled=True,
    # 不设全局默认限流，避免误伤静态资源等；逐个接口显式声明
    default_limits=[],
    # ⚠️ 刻意 **不开** headers_enabled。
    #
    # 开启后 slowapi 会往响应里注入 X-RateLimit-* 头，代价是**每个被限流的函数
    # 签名里都必须有 `response: Response` 参数**，否则抛：
    #     Exception: parameter `response` must be an instance of starlette.responses.Response
    # 表现为接口直接 500（本项目的 captcha 就踩过这个坑）。
    #
    # 权衡：响应头只是可观测性锦上添花，而 429 的响应体已给出 detail，前端足够用。
    # 为此让 6 个业务函数都挂上一个仅为框架服务的参数，得不偿失，且容易漏加。
    # 若将来确实需要这些头，再逐个补 `response: Response` 参数并打开此开关。
    headers_enabled=False,
)

# ---------------------------------------------------------------- 各接口配额
# 放在这里统一管理，改动配额不用翻各个路由文件。
#
# 为什么登录是 10/分钟而不是 5：
#   登录已叠加图像验证码，爆破成本很高；而 NAT 场景（学校/公司几十人共用一个出口 IP）
#   阈值过严会大面积误伤真实用户。10/分钟 + fail2ban 的长周期封禁，防护更均衡。
LIMITS = {
    "auth_captcha": "30/minute",     # 验证码：防高频刷（每次都要画 SVG，耗 CPU）
    "auth_login": "10/minute",       # 登录：防密码爆破
    "auth_register": "5/hour",       # 注册：防批量刷号
    "password_request": "5/hour",    # 改密申请：防被当邮件轰炸的跳板
    "task_create": "20/hour",        # 提交生成：防刷任务把本机 Worker 压满
    "public_read": "200/minute",     # 公开读接口：留给正常浏览与爬虫合理抓取
}


# ---------------------------------------------------------------- 429 处理
def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """统一 429 响应。

    返回 JSON 而非默认纯文本 —— 前端 axios 能直接读到 detail 并提示用户，
    否则会落到「请求失败」这种无信息的兜底分支。
    """
    retry_after = ""
    try:
        retry_after = getattr(exc, "retry_after", "") or ""
    except Exception:  # noqa: BLE001
        pass
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return JSONResponse(
        status_code=429,
        content={
            "detail": "请求过于频繁，请稍后再试",
            "limit": str(getattr(exc, "detail", "")),
        },
        headers=headers,
    )
