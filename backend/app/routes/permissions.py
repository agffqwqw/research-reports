# -*- coding: utf-8 -*-
"""权限申请 / 邮箱批准（B8）

权限范围（scope）：
    unlimited → 无限次生成
    edit      → 修改研报
    delete    → 删除研报
    all       → 以上三项（「申请全部权限」）

流程：
    用户 POST /request 或 /request-all
        → 建 permission_request(pending, scope) + 生成 approve_token
        → 发邮件到 ADMIN_EMAIL（含一键批准链接）
        → 同时写一条 notification 入队（本机 WorkBuddy 会用飞书推送）

    批准有**两条路**（因为链接会因服务未部署而打不开）：
      a) 点邮件 / 飞书里的链接 → GET /approve?token=… → 返回 HTML 结果页
      b) 本机代执行 → GET /pending 看队列 + POST /approve（需 WORKER_TOKEN）
         对应 `python worker.py perm list` / `perm approve --id N`

安全：approve_token 用 secrets.token_urlsafe(32) 生成，无法猜测；
     批准接口本身不需要登录（token 即凭证）；
     /pending 与 POST /approve 会接触 token，因此**只对 WORKER_TOKEN 开放**。
"""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import config
from app.db import db, query_all, query_one
from app.deps import PERMISSIONS, get_current_user, permission_codes, require_worker
from app.mailer import send_permission_mail_scoped
from app.notify import KIND_PERMISSION_APPLY, enqueue
from app.pages import result_page

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# scope → 要授予的权限编码
SCOPE_MAP = {
    "unlimited": ["unlimited"],
    "edit": ["edit"],
    "delete": ["delete"],
    "all": ["unlimited", "edit", "delete"],
}

SCOPE_LABEL = {
    "unlimited": "无限次生成",
    "edit": "修改研报",
    "delete": "删除研报",
    "all": "全部权限",
}


class ApplyIn(BaseModel):
    reason: str = Field(default="", max_length=300)


def _scope_label(scope: str) -> str:
    return SCOPE_LABEL.get(scope, scope or "权限")


def _create_request(user: dict, scope: str, reason: str) -> dict:
    """建申请 + 发管理员邮件。返回给前端的响应。"""
    codes = SCOPE_MAP.get(scope)
    if not codes:
        raise HTTPException(400, "不支持的申请范围")

    owned = set(permission_codes(user))
    lacking = [c for c in codes if c not in owned]
    if not lacking:
        return {
            "ok": True,
            "already": True,
            "message": f"你已拥有「{_scope_label(scope)}」，无需重复申请",
        }

    with db() as conn:
        pending = query_one(
            conn,
            """SELECT id, scope FROM permission_request
               WHERE user_id = ? AND status = 'pending' LIMIT 1""",
            (user["id"],),
        )
        if pending:
            return {
                "ok": True,
                "already": True,
                "pending": True,
                "message": f"已有一条待处理的申请（{_scope_label(pending.get('scope'))}），"
                           "请耐心等待管理员批准",
            }

        token = secrets.token_urlsafe(32)
        cur = conn.execute(
            """INSERT INTO permission_request
                 (user_id, request_type, scope, status, approve_token, created_at)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (user["id"], scope if scope != "all" else "all", scope, token, now()),
        )
        req_id = cur.lastrowid

        # 同时写一条通知入队 —— 本机 Worker 会取走并用飞书推给管理员。
        # 邮件照发（保留原渠道），这条是**增量**：邮件往往要等你去翻，
        # 飞书能即时推送到手机上。
        label_for_notice = _scope_label(scope)
        approve_link = f"{config.BASE_URL}/api/permissions/approve?token={token}"
        body_lines = [
            "申请人：%s" % user.get("email", ""),
            "申请范围：%s" % label_for_notice,
        ]
        if reason:
            body_lines.append("申请理由：%s" % reason)
        body_lines.append("")
        body_lines.append("【批准方式，任选其一】")
        body_lines.append("① 在 WorkBuddy 对话里说一句「批准权限申请」，由它代为执行"
                          "（推荐，现在就能用）")
        body_lines.append("② 或点下面的链接一键批准（需要服务已部署到公网）：")
        body_lines.append(approve_link)
        notif_id = enqueue(
            conn,
            kind=KIND_PERMISSION_APPLY,
            title="权限申请：%s（%s）" % (user.get("email", ""), label_for_notice),
            body="\n".join(body_lines),
            link=approve_link,
            ref_id=req_id,
        )

    label = _scope_label(scope)
    if not config.ADMIN_EMAIL:
        return {
            "ok": True,
            "mail_sent": False,
            "feishu_queued": True,
            "notification_id": notif_id,
            "message": f"「{label}」申请已记录并进入飞书通知队列，"
                       "但服务端未配置管理员邮箱（ADMIN_EMAIL），邮件未发出",
        }

    sent_ok, sent_msg = send_permission_mail_scoped(
        config.ADMIN_EMAIL, user.get("email", ""), reason, approve_link, label
    )

    mail_part = "邮件已发送" if sent_ok else f"邮件发送失败（{sent_msg}）"
    return {
        "ok": True,
        "mail_sent": sent_ok,
        "mail_note": "" if sent_ok else sent_msg,
        "feishu_queued": True,
        "notification_id": notif_id,
        "message": f"「{label}」申请已提交。{mail_part}；"
                   "飞书通知已入队，本机 WorkBuddy 会在下次轮询时推送",
    }


@router.get("/mine", summary="我的权限申请记录 + 已拥有权限")
def my_requests(user: dict = Depends(get_current_user)):
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT id, request_type, scope, status, created_at, decided_at
               FROM permission_request WHERE user_id = ?
               ORDER BY id DESC LIMIT 20""",
            (user["id"],),
        )
    for r in rows:
        r["scope_label"] = _scope_label(r.get("scope"))

    owned = set(permission_codes(user))
    return {
        "items": rows,
        "is_unlimited": bool(user.get("is_unlimited")),
        "quota_left": user.get("quota_left"),
        "owned": sorted(owned),
        "permissions": [
            {"code": code, "label": label, "granted": code in owned}
            for code, (_f, label) in PERMISSIONS.items()
        ],
    }


@router.post("/request", summary="申请单项权限（scope 默认 unlimited）")
def apply_one(data: ApplyIn, scope: str = Query(default="unlimited"), user: dict = Depends(get_current_user)):
    return _create_request(user, scope, data.reason)


@router.post("/request-all", summary="申请全部权限（无限次生成 + 修改 + 删除）")
def apply_all(data: ApplyIn, user: dict = Depends(get_current_user)):
    return _create_request(user, "all", data.reason)


@router.get("/approve", summary="按申请范围批准（邮件链接，无需登录）",
            response_class=HTMLResponse)
def approve(token: str = Query(min_length=8)):
    """邮件/飞书里点链接走这里，返回一个 HTML 结果页。"""
    with db() as conn:
        result = _do_approve(conn, token)

    if not result["ok"]:
        if result["reason"] == "not_found":
            return result_page("链接无效", "未找到对应的申请记录，可能已被处理或链接有误。",
                               ok=False)
        return result_page("无法批准", result.get("message", "申请范围无法识别，请手工处理。"),
                           ok=False)

    if result.get("already"):
        return result_page("已批准过", f"{result['email']} 的权限已生效。", ok=True)

    return result_page(
        "已批准",
        f"用户 <b>{result['email']}</b> 已获得：<b>{result['granted_label']}</b>。",
        ok=True,
    )


# ============================================================
# Worker 侧（本机代管理员执行批准）
#
# 存在的理由：邮件/飞书里的链接指向服务器地址，在**服务尚未部署**时打不开。
# 本机 Worker 已有 WORKER_TOKEN 受信，可以直接读队列并执行批准，
# 于是「在 WorkBuddy 对话里说一句『批准』」就能完成，不依赖任何链接可达性。
#
# ⚠️ 这两个接口会返回/使用 approve_token —— 它等同于批准权，
#    所以只对持有 WORKER_TOKEN 的本机开放，不得暴露给浏览器前端。
# ============================================================
@router.get("/pending", summary="[worker] 列出待批准的权限申请（含批准 token）")
def list_pending(_: bool = Depends(require_worker)):
    with db() as conn:
        rows = query_all(
            conn,
            """SELECT p.id, p.scope, p.created_at, p.approve_token,
                      u.email, u.id AS user_id
               FROM permission_request p JOIN user u ON u.id = p.user_id
               WHERE p.status = 'pending' AND p.approve_token IS NOT NULL
               ORDER BY p.id ASC""",
        )
    for r in rows:
        r["scope_label"] = _scope_label(r.get("scope"))
        r["approve_url"] = "%s/api/permissions/approve?token=%s" % (
            config.BASE_URL, r.get("approve_token"))
    return {"ok": True, "count": len(rows), "items": rows}


class ApproveIn(BaseModel):
    token: str | None = Field(default=None, description="批准 token")
    id: int | None = Field(default=None, description="或直接给申请 id")


@router.post("/approve", summary="[worker] 代管理员批准（返回 JSON）")
def approve_by_worker(data: ApproveIn, _: bool = Depends(require_worker)):
    token = data.token
    with db() as conn:
        if not token and data.id:
            row = query_one(
                conn,
                "SELECT approve_token, status FROM permission_request WHERE id = ?",
                (data.id,),
            )
            if not row:
                raise HTTPException(404, "申请不存在")
            # 已处理过的申请，approve_token 会被清空 —— 这时给准确的提示，
            # 而不是含糊的「请提供 token 或 id」
            if row["status"] != "pending":
                label = {"approved": "已批准", "rejected": "已拒绝"}.get(
                    row["status"], "已%s" % row["status"])
                return {"ok": True, "already": True, "status": row["status"],
                        "message": "该申请%s，无需重复处理" % label}
            token = row["approve_token"]

        if not token:
            raise HTTPException(400, "请提供 token 或 id")
        result = _do_approve(conn, token)

    if not result["ok"]:
        raise HTTPException(404 if result["reason"] == "not_found" else 400,
                            result.get("message", "批准失败"))
    return result


def _do_approve(conn, token: str) -> dict:
    """批准的核心逻辑（GET 链接与 worker 代执行共用一条实现）。

    返回 {ok: True, email, granted_label, already} 或 {ok: False, reason, message}
    """
    req = query_one(
        conn,
        """SELECT p.id, p.user_id, p.status, p.scope, u.email
           FROM permission_request p JOIN user u ON u.id = p.user_id
           WHERE p.approve_token = ?""",
        (token,),
    )
    if not req:
        return {"ok": False, "reason": "not_found"}

    scope = req.get("scope") or "unlimited"
    codes = SCOPE_MAP.get(scope, ["unlimited"])
    fields = [PERMISSIONS[c][0] for c in codes if c in PERMISSIONS]
    if not fields:
        return {"ok": False, "reason": "bad_scope", "message": "申请范围无法识别"}

    if req["status"] == "approved":
        return {"ok": True, "already": True, "email": req["email"],
                "granted_label": "、".join(SCOPE_LABEL.get(c, c) for c in codes)}

    conn.execute(
        "UPDATE permission_request SET status='approved', decided_at=?, approve_token=NULL WHERE id=?",
        (now(), req["id"]),
    )
    set_clause = ", ".join("%s = 1" % f for f in fields)
    conn.execute("UPDATE user SET %s WHERE id = ?" % set_clause, (req["user_id"],))

    return {
        "ok": True,
        "already": False,
        "email": req["email"],
        "scope": scope,
        "granted": codes,
        "granted_label": "、".join(SCOPE_LABEL.get(c, c) for c in codes),
    }
