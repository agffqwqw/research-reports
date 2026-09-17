# -*- coding: utf-8 -*-
"""通知队列的 Worker 接口（供本机取走并发送）

分工：
    服务器 → 只负责入队（app/notify.py 的 enqueue）
    本机   → 取走并用飞书 / 邮件发送，再回传结果

所以发送渠道（飞书、邮件、短信）的代码都跑在本机 WorkBuddy，
服务器侧不需要任何第三方凭证。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import db, query_all, query_one
from app.deps import require_worker
from app.notify import pending_count

router = APIRouter(prefix="/api/worker/notifications", tags=["worker-notifications"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# 超过这个秒数仍是 sending，视为「Worker 中途挂了」，重新放回队列
STALE_SENDING_SECS = 900
# 最多尝试次数：避免消息永久发不出去时在队列里死循环
MAX_ATTEMPTS = 3


class NotifyResultIn(BaseModel):
    status: str = Field(pattern="^(sent|failed)$")
    error_msg: str | None = None


def _release_stale(conn) -> int:
    """把卡住的 sending 放回 pending，避免通知因 WorkBuddy 中断而永久搁死。"""
    rows = query_all(
        conn, "SELECT id, created_at FROM notification WHERE status = 'sending'")
    released = 0
    t = datetime.now(timezone.utc)
    for r in rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (t - created).total_seconds() > STALE_SENDING_SECS:
                conn.execute("UPDATE notification SET status='pending' WHERE id=?", (r["id"],))
                released += 1
        except Exception:  # noqa: BLE001
            continue
    return released


@router.get("/peek", summary="[worker] 只读查看通知队列（不改变状态）")
def peek(_: bool = Depends(require_worker)):
    with db() as conn:
        items = query_all(
            conn,
            """SELECT id, kind, title, status, attempts, created_at, sent_at
               FROM notification
               WHERE status IN ('pending','sending')
               ORDER BY id ASC LIMIT 30""",
        )
        cnt = pending_count(conn)
    return {"ok": True, "pending_count": cnt, "items": items}


@router.get("/next", summary="[worker] 取出一条待发通知（标记为 sending）")
def next_one(_: bool = Depends(require_worker)):
    with db() as conn:
        released = _release_stale(conn)
        row = query_one(
            conn,
            """SELECT id, kind, title, body, link, ref_id, attempts, created_at
               FROM notification WHERE status = 'pending'
               ORDER BY id ASC LIMIT 1""",
        )
        if not row:
            return {"ok": True, "notification": None, "released_stale": released}
        conn.execute(
            """UPDATE notification
               SET status='sending', attempts = COALESCE(attempts, 0) + 1
               WHERE id = ?""",
            (row["id"],),
        )
    row["released_stale"] = released
    return {"ok": True, "notification": row}


@router.post("/{notif_id}/result", summary="[worker] 回传发送结果")
def report_result(notif_id: int, data: NotifyResultIn,
                  _: bool = Depends(require_worker)):
    with db() as conn:
        row = query_one(
            conn, "SELECT id, attempts FROM notification WHERE id = ?", (notif_id,))
        if not row:
            raise HTTPException(404, "通知不存在")

        if data.status == "sent":
            conn.execute(
                """UPDATE notification SET status='sent', sent_at=?, error_msg=NULL
                   WHERE id=?""",
                (now(), notif_id),
            )
            return {"ok": True, "status": "sent"}

        # failed：未超上限就放回队列待重试；超了就终结，避免死循环刷屏。
        attempts = row["attempts"] or 0
        msg = (data.error_msg or "发送失败")[:500]
        if attempts < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE notification SET status='pending', error_msg=? WHERE id=?",
                (msg, notif_id),
            )
            return {"ok": True, "status": "failed", "requeued": True,
                    "attempts": attempts, "max_attempts": MAX_ATTEMPTS}

        conn.execute(
            "UPDATE notification SET status='failed', error_msg=? WHERE id=?",
            ("已重试 %d 次仍失败，终止：%s" % (attempts, msg), notif_id),
        )
        return {"ok": True, "status": "failed", "requeued": False,
                "attempts": attempts, "max_attempts": MAX_ATTEMPTS,
                "message": "已达重试上限，通知标记为 failed"}
