# -*- coding: utf-8 -*-
"""认证路由（B6 登录+图形验证码 / 注册即登录）

权限模型（来自需求 v4）：
- 注册 → **直接签发令牌（注册即登录）**，默认 can_generate=1、quota_left=2
- 无限次 / 修改 / 删除权限在 B8 实现（个人中心申请 → 管理员批准）

⚠️ 邮箱验证已取消（2026-09-15 用户决定）：
    邮箱验证既不阻断生成、也不影响任何功能，发那封邮件纯属打扰。
    邮箱现在的唯一作用是「账号标识」。
    `email_verified` / `verify_token` 字段与 `/verify` 接口保留，
    以便将来如需找回密码等功能时可以直接启用。
"""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.config import config
from app.db import db, query_one
from app.ratelimit import LIMITS, limiter
from app.security import (
    create_access_token,
    gen_captcha,
    hash_password,
    verify_captcha,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=72)  # bcrypt 上限 72 字节
    captcha_token: str = ""
    captcha: str = ""


class LoginIn(BaseModel):
    email: str
    password: str
    captcha_token: str
    captcha: str


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


@router.get("/captcha", summary="获取图形验证码（返回 SVG + token）")
@limiter.limit(LIMITS["auth_captcha"])
def captcha(request: Request):
    """每次调用都要渲染一张 SVG，是纯 CPU 开销 —— 不限流会被高频刷爆。"""
    token, svg = gen_captcha()
    return {"token": token, "svg": svg}


@router.post("/register", summary="注册（注册成功直接返回登录令牌，免去二次登录）")
@limiter.limit(LIMITS["auth_register"])
def register(request: Request, data: RegisterIn):
    """限流按 IP —— 防批量注册刷号；注册成功即签发令牌，故也要防刷。"""
    if not verify_captcha(data.captcha_token, data.captcha):
        raise HTTPException(400, "验证码错误或已过期")

    email = data.email.strip().lower()
    if not _valid_email(email):
        raise HTTPException(400, "邮箱格式不正确")
    if len(data.password) < 8:
        raise HTTPException(400, "密码至少 8 位")

    with db() as conn:
        if query_one(conn, "SELECT id FROM user WHERE email = ?", (email,)):
            raise HTTPException(409, "该邮箱已注册")

        # email_verified 保留字段但**不再走验证流程**：
        # 邮箱验证既不阻断生成、也不影响任何功能，发那封邮件纯属打扰用户。
        # 邮箱现在的唯一作用是「账号标识 + 将来找回密码的凭证」。
        # last_login_at 一并写入 —— 注册即登录，个人中心不该显示「上次登录 —」
        cur = conn.execute(
            """INSERT INTO user (email, password_hash, role, email_verified,
                                 verify_token, can_generate, quota_left,
                                 is_unlimited, created_at, last_login_at)
               VALUES (?, ?, 'user', 0, NULL, 1, ?, 0, ?, ?)""",
            (email, hash_password(data.password), config.DEFAULT_QUOTA, now(), now()),
        )
        user_id = cur.lastrowid

    # 直接签发令牌 —— 注册即登录，避免「注册完还要再登一次」的断点
    token = create_access_token(user_id, "user", email)

    return {
        "ok": True,
        "email": email,
        "quota": config.DEFAULT_QUOTA,
        "access_token": token,
        "token_type": "bearer",
        "role": "user",
        "hint": f"注册成功，已获得 {config.DEFAULT_QUOTA} 次生成机会，已自动登录。",
    }


@router.get("/verify", summary="邮箱验证（点击邮件里的链接）")
def verify(token: str = Query(min_length=8)):
    with db() as conn:
        row = query_one(conn, "SELECT id, email_verified FROM user WHERE verify_token = ?", (token,))
        if not row:
            raise HTTPException(404, "验证链接无效或已过期")
        if row["email_verified"]:
            return {"ok": True, "already": True, "message": "邮箱已验证过"}
        conn.execute(
            "UPDATE user SET email_verified = 1, verify_token = NULL WHERE id = ?",
            (row["id"],),
        )
    return {"ok": True, "message": "邮箱验证成功，现在可以登录了"}


@router.post("/login", summary="登录（验证码 + 密码）")
@limiter.limit(LIMITS["auth_login"])
def login(request: Request, data: LoginIn):
    """限流按 IP —— 防密码爆破。

    为什么是 10/分钟而不是更严：
      登录已叠加图形验证码，爆破成本本已很高；而 NAT 场景（学校/公司几十人
      共用一个出口 IP）阈值过严会大面积误伤真实用户。
      「慢速爆破」（每分钟几次、永不触发阈值）由 fail2ban 在更长周期上兜底。
    """
    # 1) 图形验证码（失败率高，先校验，避免无谓查库）
    if not verify_captcha(data.captcha_token, data.captcha):
        raise HTTPException(400, "验证码错误或已过期")

    email = data.email.strip().lower()
    with db() as conn:
        row = query_one(
            conn,
            "SELECT id, email, password_hash, role, email_verified FROM user WHERE email = ?",
            (email,),
        )
    if not row or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(401, "邮箱或密码错误")

    # 不再要求邮箱验证 —— 注册后即可登录使用；邮箱验证改为可选（提升账号安全性）

    token = create_access_token(row["id"], row["role"], row["email"])
    with db() as conn:
        conn.execute("UPDATE user SET last_login_at = ? WHERE id = ?", (now(), row["id"]))

    return {
        "ok": True,
        "access_token": token,
        "token_type": "bearer",
        "role": row["role"],
        "email": row["email"],
    }
