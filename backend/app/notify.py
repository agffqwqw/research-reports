# -*- coding: utf-8 -*-
"""通知队列（服务器入队 → 本机 Worker 取走发送）

为什么要有这一层：
    服务器上的后端**无法直接调飞书 / WorkBuddy 的 MCP 工具** —— 那些只在
    WorkBuddy 运行时可用，服务器上只是个普通 Python 进程。
    所以改成「服务器只管记录下来，本机来发」：

        后端需要通知  → enqueue() 写进 notification 表（pending）
                              ↓
        本机 WorkBuddy 定时任务 → 拉取 → 用飞书发送 → 回传 sent/failed

好处：
    - 服务器不需要 SMTP 配置、不需要飞书凭证
    - 换渠道（飞书 / 邮件 / 短信）只改本机侧，服务器不用动
    - 发送失败会留在队列里，可以重试，不像 SMTP 一失败就丢
"""
from datetime import datetime, timezone

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# 通知类型
KIND_PERMISSION_APPLY = "permission_apply"
KIND_SERVICE_ALERT = "service_alert"


def enqueue(conn, kind: str, title: str, body: str = "", link: str = "",
            ref_id: int | None = None) -> int:
    """写一条待发通知，返回 notification.id。

    注意：传入的 conn 由调用方管理事务，本函数不 commit，
    这样「入队」与「业务写入」要么一起成功、要么一起回滚。
    """
    cur = conn.execute(
        """INSERT INTO notification (kind, title, body, link, ref_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (kind, title, body or "", link or "", ref_id, now()),
    )
    return cur.lastrowid


def pending_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM notification WHERE status IN ('pending','sending')"
    ).fetchone()[0]
