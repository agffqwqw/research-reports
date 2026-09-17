# -*- coding: utf-8 -*-
"""配置读取

从 app.env 加载环境变量。刻意不引入 python-dotenv，用自己的极简解析，
减少依赖面（2G 机器上少一个包就少一份负担）。
"""
import os
from pathlib import Path


def _load_env(path: str | None = None) -> None:
    """极简 .env 解析：逐行读 KEY=VALUE，已存在的环境变量不覆盖。"""
    p = Path(path or os.environ.get("APP_ENV_FILE", "app.env"))
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


_load_env()


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ---------- 服务 ----------
    HOST: str = os.environ.get("APP_HOST", "127.0.0.1")
    PORT: int = int(os.environ.get("APP_PORT", "8080"))
    BASE_URL: str = os.environ.get("APP_BASE_URL", "")

    # ---------- 存储 ----------
    DB_PATH: str = os.environ.get("DB_PATH", "data/app.db")
    REDIS_HOST: str = os.environ.get("REDIS_HOST", "127.0.0.1")
    REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.environ.get("REDIS_DB", "1"))

    # ---------- 安全 ----------
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "")
    JWT_EXPIRE: int = int(os.environ.get("JWT_EXPIRE", "86400"))

    # ---------- 业务 ----------
    ADMIN_EMAIL: str = os.environ.get("ADMIN_EMAIL", "")
    DEFAULT_QUOTA: int = int(os.environ.get("DEFAULT_QUOTA", "2"))
    TASK_TIMEOUT: int = int(os.environ.get("TASK_TIMEOUT", "1800"))
    POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "300"))

    # ---------- Worker（本机生成端）鉴权 ----------
    # 本机轮询/回传任务时携带，避免任务队列接口对外暴露
    WORKER_TOKEN: str = os.environ.get("WORKER_TOKEN", "")

    # ---------- 接口文档（/docs /redoc /openapi.json）----------
    # 这三个端点会完整暴露所有接口的定义、参数与权限要求 ——
    # 对扫描者而言等于一张现成的 API 地图，生产环境应关闭。
    #
    # 默认 **False**（安全默认值）：忘记配置时宁可少一个调试入口，
    # 也不要意外把接口地图暴露到公网。本机开发在 app.env 里显式打开即可。
    ENABLE_DOCS: bool = _bool("ENABLE_DOCS", False)

    # ---------- 邮件 ----------
    MAIL_ENABLED: bool = _bool("MAIL_ENABLED", False)
    MAIL_HOST: str = os.environ.get("MAIL_HOST", "smtp.163.com")
    MAIL_PORT: int = int(os.environ.get("MAIL_PORT", "465"))
    MAIL_USER: str = os.environ.get("MAIL_USER", "")
    MAIL_PASSWORD: str = os.environ.get("MAIL_PASSWORD", "")
    MAIL_FROM: str = os.environ.get("MAIL_FROM", "")
    MAIL_FROM_NAME: str = os.environ.get("MAIL_FROM_NAME", "研报站点")

    @classmethod
    def check(cls) -> list[str]:
        """自检：返回配置问题列表（空列表表示无问题）。"""
        problems: list[str] = []
        if not cls.JWT_SECRET or len(cls.JWT_SECRET) < 32:
            problems.append("JWT_SECRET 未设置或过短（建议 openssl rand -hex 32）")
        if cls.REDIS_DB == 0:
            problems.append("REDIS_DB 建议不要用 0（redis-demo 在用），改用 1")
        if not cls.ADMIN_EMAIL:
            problems.append("ADMIN_EMAIL 未设置，权限批准邮件将无处发送")
        if not cls.MAIL_ENABLED:
            problems.append("MAIL_ENABLED=false：注册邮箱验证与权限批准邮件将不会真正发出")
        # 防「配置漂移」：如果 app.env 里没写 ENABLE_DOCS，则行为完全由代码默认值决定。
        # 这里额外拦一道 —— 只要是开启状态就提醒，避免误把接口地图暴露到公网。
        if cls.ENABLE_DOCS:
            problems.append(
                "ENABLE_DOCS=true：接口文档（/docs、/redoc、/openapi.json）处于开放状态，"
                "会暴露全部接口定义；仅本机开发可开，生产环境请关闭")
        return problems


config = Config()
