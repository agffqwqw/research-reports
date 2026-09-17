# -*- coding: utf-8 -*-
"""个人中心（账号信息 / 修改密码）

三个能力：
  1. GET  /api/me/profile          → 账号信息 + 已有权限清单 + 待处理申请
  2. POST /api/me/password/request → 提交新密码，发确认邮件到**本人邮箱**
  3. GET  /api/me/password/confirm → 点邮件链接，密码才真正生效

为什么改密要走「邮件确认」：
    直接改密意味着「谁拿到登录态谁就能锁死原主人」。
    加一道邮箱确认后，邮箱成为最后一道防线 —— 攻击者需同时控制邮箱才行。
    新密码**先哈希再暂存**，确认前不会写入 password_hash，因此不会覆盖旧密码。
"""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import config
from app.db import db, query_all, query_one
from app.deps import PERMISSIONS, get_current_user, permission_codes
from app.mailer import send_password_change_mail
from app.pages import result_page
from app.ratelimit import LIMITS, limiter
from app.security import hash_password

router = APIRouter(prefix="/api/me", tags=["me"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# 改密链接有效期（秒），与邮件里的说明保持一致
PWD_TOKEN_MAX_AGE = 1800


class PasswordChangeIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=72)
    confirm_password: str = Field(min_length=8, max_length=72)


def _secs_since(iso: str | None) -> float | None:
    """返回某 ISO 时间距今的秒数；解析失败返回 None。"""
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:  # noqa: BLE001
        return None


@router.get("/profile", summary="账号信息 + 已有权限 + 待处理申请")
def profile(user: dict = Depends(get_current_user)):
    with db() as conn:
        account = query_one(
            conn,
            """SELECT email, role, email_verified, created_at, last_login_at,
                      quota_left, is_unlimited, can_edit_report, can_delete_report
               FROM user WHERE id = ?""",
            (user["id"],),
        ) or {}

        # 待处理申请（含范围）
        pending = query_one(
            conn,
            """SELECT id, request_type, scope, status, created_at
               FROM permission_request
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id DESC LIMIT 1""",
            (user["id"],),
        )

        # 历史申请（最近 10 条）
        history = query_all(
            conn,
            """SELECT id, request_type, status, created_at, decided_at
               FROM permission_request WHERE user_id = ?
               ORDER BY id DESC LIMIT 10""",
            (user["id"],),
        )

        # 该用户的生成任务统计
        stats = query_one(
            conn,
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                      SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN status IN ('pending','processing') THEN 1 ELSE 0 END) AS running
               FROM generation_task WHERE user_id = ?""",
            (user["id"],),
        ) or {}

    owned = set(permission_codes(user))
    permissions = [
        {"code": code, "label": label, "granted": code in owned}
        for code, (_field, label) in PERMISSIONS.items()
    ]

    return {
        "account": {
            "email": account.get("email", ""),
            "role": account.get("role", "user"),
            "email_verified": bool(account.get("email_verified")),
            "created_at": account.get("created_at", ""),
            "last_login_at": account.get("last_login_at", ""),
        },
        "permissions": permissions,
        "granted_count": len(owned),
        "total_permissions": len(PERMISSIONS),
        "quota": {
            "left": account.get("quota_left"),
            "unlimited": bool(account.get("is_unlimited")),
        },
        "pending_request": pending,
        "history": history,
        "task_stats": {
            "total": stats.get("total") or 0,
            "success": stats.get("success") or 0,
            "failed": stats.get("failed") or 0,
            "running": stats.get("running") or 0,
        },
    }


@router.post("/password/request", summary="提交修改密码（发确认邮件到本人邮箱）")
@limiter.limit(LIMITS["password_request"])
def request_password_change(request: Request, data: PasswordChangeIn,
                            user: dict = Depends(get_current_user)):
    """限流按 IP —— 这条会触发外发邮件，不限流等于送人一个邮件轰炸跳板，
    发件邮箱很快会被收件方拉黑。"""
    if data.new_password != data.confirm_password:
        raise HTTPException(400, "两次输入的密码不一致")
    if len(data.new_password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if data.new_password == user.get("email"):
        raise HTTPException(400, "密码不能与邮箱相同")

    email = user.get("email", "")
    if not email:
        raise HTTPException(400, "账号缺少邮箱，无法发送确认邮件")

    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            """UPDATE user
               SET pwd_change_token = ?, pwd_change_hash = ?, pwd_change_at = ?
               WHERE id = ?""",
            (token, hash_password(data.new_password), now(), user["id"]),
        )

    link = f"{config.BASE_URL}/api/me/password/confirm?token={token}"
    sent_ok, sent_msg = send_password_change_mail(email, link)

    if not sent_ok:
        # 发信失败：把暂存清掉，避免留下一个永远无法确认的待改密状态
        with db() as conn:
            conn.execute(
                """UPDATE user
                   SET pwd_change_token = NULL, pwd_change_hash = NULL, pwd_change_at = NULL
                   WHERE id = ?""",
                (user["id"],),
            )
        return {
            "ok": False,
            "mail_sent": False,
            "message": f"确认邮件发送失败（{sent_msg}），密码未做任何修改",
        }

    # 邮箱做打码，避免在响应里回显完整地址
    masked = email
    if "@" in email:
        name, domain = email.split("@", 1)
        show = name[:2] if len(name) > 2 else name[:1]
        masked = f"{show}{'*' * max(1, len(name) - len(show))}@{domain}"

    return {
        "ok": True,
        "mail_sent": True,
        "message": f"确认邮件已发送到 {masked}，请在 30 分钟内点击邮件里的链接完成修改",
    }


@router.get("/password/confirm", summary="确认修改密码（邮件链接，返回 HTML 结果页）",
            response_class=HTMLResponse)
def confirm_password_change(token: str = Query(min_length=8)):
    with db() as conn:
        row = query_one(
            conn,
            """SELECT id, email, pwd_change_token, pwd_change_hash, pwd_change_at
               FROM user WHERE pwd_change_token = ?""",
            (token,),
        )
        if not row:
            return result_page(
                "链接无效",
                "这个改密链接不存在或已被使用过。<br>如果密码还没改成功，请回个人中心重新发起。",
                ok=False,
            )

        # 有效期检查：与邮件里承诺的 30 分钟一致
        age = _secs_since(row.get("pwd_change_at"))
        if age is not None and age > PWD_TOKEN_MAX_AGE:
            conn.execute(
                """UPDATE user
                   SET pwd_change_token = NULL, pwd_change_hash = NULL, pwd_change_at = NULL
                   WHERE id = ?""",
                (row["id"],),
            )
            return result_page(
                "链接已过期",
                "改密链接已超过 30 分钟有效期。<br>出于安全考虑它已失效，请回个人中心重新发起修改。",
                ok=False,
            )

        if not row.get("pwd_change_hash"):
            return result_page("链接无效", "该请求缺少有效的新密码，请重新发起。", ok=False)

        conn.execute(
            """UPDATE user
               SET password_hash = ?, pwd_change_token = NULL,
                   pwd_change_hash = NULL, pwd_change_at = NULL
               WHERE id = ?""",
            (row["pwd_change_hash"], row["id"]),
        )

    return result_page(
        "密码已修改",
        f"账号 <b>{row['email']}</b> 的密码已更新。<br>请使用新密码重新登录。",
        ok=True,
    )


@router.post("/password/cancel", summary="撤销待确认的改密请求")
def cancel_password_change(user: dict = Depends(get_current_user)):
    with db() as conn:
        conn.execute(
            """UPDATE user
               SET pwd_change_token = NULL, pwd_change_hash = NULL, pwd_change_at = NULL
               WHERE id = ?""",
            (user["id"],),
        )
    return {"ok": True, "message": "已撤销待确认的改密请求"}
