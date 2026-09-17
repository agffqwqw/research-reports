# -*- coding: utf-8 -*-
"""SMTP 发信（B5 注册验证 / 权限批准）

说明：服务器上无法调用 WorkBuddy 的 Agent Mail（那是 MCP 工具），
所以这里走标准 SMTP。配置来自 app.env（MAIL_* 项）。
"""
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from app.config import config


def send_mail(to: str, subject: str, html_body: str) -> tuple[bool, str]:
    """发送 HTML 邮件。返回 (是否成功, 说明)。"""
    if not config.MAIL_ENABLED:
        return False, "邮件未启用（MAIL_ENABLED=false）"
    if not config.MAIL_USER or not config.MAIL_PASSWORD:
        return False, "SMTP 未配置（缺 MAIL_USER / MAIL_PASSWORD）"

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"{Header(config.MAIL_FROM_NAME, 'utf-8').encode()} <{config.MAIL_FROM}>"
    msg["To"] = to

    try:
        # 163 等国内邮箱用 SSL（465 端口）
        with smtplib.SMTP_SSL(config.MAIL_HOST, config.MAIL_PORT, timeout=20) as server:
            server.login(config.MAIL_USER, config.MAIL_PASSWORD)
            server.sendmail(config.MAIL_FROM, [to], msg.as_string())
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def send_verify_mail(to: str, verify_link: str) -> tuple[bool, str]:
    subject = "验证你的研报站点账号"
    body = (
        "<p>你好，</p>"
        "<p>你正在注册研报站点账号。请点击下面的链接完成邮箱验证（24 小时内有效）：</p>"
        f'<p><a href="{verify_link}">{verify_link}</a></p>'
        "<p>如果不是你本人操作，请忽略此邮件。</p>"
    )
    return send_mail(to, subject, body)


def send_permission_mail(to: str, applicant_email: str, reason: str,
                         approve_link: str) -> tuple[bool, str]:
    """向管理员发送「权限申请」，含一键批准链接。"""
    subject = f"[研报站点] 权限申请：{applicant_email}"
    reason_html = f"<p><b>申请理由：</b>{reason}</p>" if reason else "<p>（未填写申请理由）</p>"
    body = (
        "<p>有用户申请权限：</p>"
        f"<p><b>申请人：</b>{applicant_email}</p>"
        f"{reason_html}"
        f'<p><a href="{approve_link}" '
        'style="display:inline-block;padding:10px 20px;background:#185fa5;color:#fff;'
        'border-radius:6px;text-decoration:none">点此批准</a></p>'
        f'<p style="color:#8a919f;font-size:12px">如非本人预期，可忽略此邮件，申请将保持待处理状态。</p>'
    )
    return send_mail(to, subject, body)


def send_permission_mail_scoped(to: str, applicant_email: str, reason: str,
                                approve_link: str, scope_label: str) -> tuple[bool, str]:
    """权限申请邮件（带申请范围）。scope_label 如「全部权限」或「修改研报」。"""
    subject = f"[研报站点] {scope_label}申请：{applicant_email}"
    reason_html = f"<p><b>申请理由：</b>{reason}</p>" if reason else "<p>（未填写申请理由）</p>"
    body = (
        f"<p>有用户申请「<b>{scope_label}</b>」：</p>"
        f"<p><b>申请人：</b>{applicant_email}</p>"
        f"{reason_html}"
        f'<p><a href="{approve_link}" '
        'style="display:inline-block;padding:10px 20px;background:#185fa5;color:#fff;'
        'border-radius:6px;text-decoration:none">点此批准</a></p>'
        f'<p style="color:#8a919f;font-size:12px">如非本人预期，可忽略此邮件，申请将保持待处理状态。</p>'
    )
    return send_mail(to, subject, body)


def send_password_change_mail(to: str, confirm_link: str) -> tuple[bool, str]:
    """修改密码确认邮件 —— 发到**用户本人邮箱**，点链接后才真正生效。

    为什么走「邮件确认」而不是直接改：这样即使登录态被短暂窃取，
    攻击者也无法在不控制邮箱的情况下改掉密码（邮箱是最后一道防线）。
    """
    subject = "确认修改研报站点密码"
    body = (
        "<p>你好，</p>"
        "<p>我们收到了修改该账号密码的请求。请点击下面的链接确认修改"
        "（链接 30 分钟内有效）：</p>"
        f'<p><a href="{confirm_link}" '
        'style="display:inline-block;padding:10px 20px;background:#185fa5;color:#fff;'
        'border-radius:6px;text-decoration:none">确认修改密码</a></p>'
        f'<p style="color:#8a919f;font-size:12px">'
        "如果这不是你本人的操作，请忽略此邮件 —— 你的密码不会发生任何变化。</p>"
    )
    return send_mail(to, subject, body)

