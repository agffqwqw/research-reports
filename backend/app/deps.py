# -*- coding: utf-8 -*-
"""依赖注入：JWT 用户鉴权 + Worker 令牌校验"""
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import config
from app.db import db, query_one
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

USER_FIELDS = ("id", "email", "role", "email_verified",
               "can_generate", "quota_left", "is_unlimited",
               "can_edit_report", "can_delete_report")

# 权限编码 → (user 表字段, 中文名)
# 三项权限合起来即「全部权限」，供个人中心展示与「申请全部权限」使用
PERMISSIONS = {
    "unlimited": ("is_unlimited", "无限次生成"),
    "edit": ("can_edit_report", "修改研报"),
    "delete": ("can_delete_report", "删除研报"),
}


def permission_codes(user: dict) -> list[str]:
    """当前用户已拥有的权限编码列表。"""
    return [code for code, (field, _) in PERMISSIONS.items() if user.get(field)]


def has_permission(user: dict, code: str) -> bool:
    field = PERMISSIONS.get(code, (None, None))[0]
    return bool(field and user.get(field))


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """从 Bearer JWT 解析当前用户。"""
    if cred is None:
        raise HTTPException(401, "未登录")
    payload = decode_access_token(cred.credentials)
    if not payload:
        raise HTTPException(401, "登录已过期，请重新登录")

    with db() as conn:
        user = query_one(
            conn,
            f"SELECT {', '.join(USER_FIELDS)} FROM user WHERE id = ?",
            (int(payload.get("sub", 0)),),
        )
    if not user:
        raise HTTPException(401, "用户不存在")
    return user


def get_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def require_permission(code: str):
    """依赖工厂：要求当前用户拥有某项权限，否则 403 并给出可操作提示。

    用法：
        @router.delete("...", dependencies=[Depends(require_permission("delete"))])
        或  user: dict = Depends(require_permission("delete"))
    """
    field, label = PERMISSIONS[code]

    def _check(user: dict = Depends(get_current_user)) -> dict:
        if not user.get(field):
            raise HTTPException(
                403,
                f"你没有「{label}」权限。可在「个人中心」申请全部权限，"
                "管理员批准后即可使用。",
            )
        return user

    return _check


def require_worker(x_worker_token: str | None = Header(default=None)) -> bool:
    """校验本机 worker 的令牌（放在 X-Worker-Token 请求头）。"""
    if not config.WORKER_TOKEN:
        raise HTTPException(503, "服务端未配置 WORKER_TOKEN，worker 接口不可用")
    if x_worker_token != config.WORKER_TOKEN:
        raise HTTPException(403, "worker 鉴权失败")
    return True


def remaining_capacity(user: dict, pending: int) -> int:
    """可用生成次数 = 剩余配额（或无限）- 进行中的任务数。

    这样可以在**成功时才扣次数**（失败不消耗）的同时，防止并发超额提交。
    """
    if user.get("is_unlimited"):
        return 10 ** 6
    return max(0, int(user.get("quota_left") or 0) - pending)
