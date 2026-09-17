#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务存活监测 + 邮件告警（O5）

为什么需要它：
    错误页上写着「已自动邮件通知，马上修复」—— 如果没人通知，
    这句话就是假的。这个脚本让那句话成立。

它检测三层，任一异常即发邮件给 ADMIN_EMAIL：
    1. systemd 服务是否 active（research-reports / caddy）
    2. 后端 /health 是否可访问（能抓住「进程在但假死」）
    3. 经 Caddy 的 80 端口能否访问（能抓住「后端好但入口挂」）

防骚扰：同一故障状态只发一次邮件，恢复时发一封「已恢复」。
        否则每 5 分钟一封，很快就被邮箱当垃圾邮件了。

部署：
    cp healthcheck.py /opt/research-reports/deploy/
    sudo crontab -e
    */5 * * * * /opt/venv/bin/python /opt/research-reports/deploy/healthcheck.py \\
                >> /var/log/research-reports/healthcheck.log 2>&1
"""
import json
import os
import smtplib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

APP_ROOT = "/opt/research-reports"
ENV_FILE = os.path.join(APP_ROOT, "backend", "app.env")
STATE_FILE = "/var/lib/research-reports/health_state.json"
LOG_PREFIX = "[healthcheck]"

SERVICE = "research-reports"
BACKEND_HEALTH = "http://127.0.0.1:8081/health"
PUBLIC_PROBE = "http://127.0.0.1/"


def log(msg: str) -> None:
    print("%s %s %s" % (LOG_PREFIX, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_env(path: str) -> dict:
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def send_mail(cfg: dict, subject: str, body_html: str) -> tuple[bool, str]:
    if (cfg.get("MAIL_ENABLED", "").lower() not in ("1", "true", "yes", "on")):
        return False, "MAIL_ENABLED 未开启"
    to = cfg.get("ADMIN_EMAIL", "")
    if not to:
        return False, "未配置 ADMIN_EMAIL"
    if not cfg.get("MAIL_USER") or not cfg.get("MAIL_PASSWORD"):
        return False, "未配置 MAIL_USER / MAIL_PASSWORD"

    msg = MIMEText(body_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = "%s <%s>" % (
        Header(cfg.get("MAIL_FROM_NAME", "研报站点"), "utf-8").encode(),
        cfg.get("MAIL_FROM") or cfg["MAIL_USER"])
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(cfg.get("MAIL_HOST", "smtp.163.com"),
                              int(cfg.get("MAIL_PORT", "465")), timeout=20) as s:
            s.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
            s.sendmail(cfg.get("MAIL_FROM") or cfg["MAIL_USER"], [to], msg.as_string())
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def probe_http(url: str, timeout: int = 6) -> tuple[bool, str]:
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(url, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            if "health" in url:
                try:
                    d = json.loads(body)
                    if d.get("status") == "ok":
                        return True, "ok"
                    # degraded：Redis 挂了但站点仍可浏览，不算故障
                    return True, "degraded(%s)" % d.get("status")
                except Exception:
                    return True, "非 JSON 但可访问"
            return True, "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        # 5xx 才算故障；4xx 说明服务活着
        if e.code >= 500:
            return False, "HTTP %d" % e.code
        return True, "HTTP %d" % e.code
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def systemd_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:  # noqa: BLE001
        return False


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as exc:  # noqa: BLE001
        log("状态文件写入失败: %s" % exc)


def main() -> int:
    cfg = load_env(ENV_FILE)

    checks = []
    svc_ok = systemd_active(SERVICE)
    checks.append(("应用服务 systemd", svc_ok, "active" if svc_ok else "非 active"))
    caddy_ok = systemd_active("caddy")
    checks.append(("Caddy 网关", caddy_ok, "active" if caddy_ok else "非 active"))

    be_ok, be_msg = probe_http(BACKEND_HEALTH)
    checks.append(("后端 /health", be_ok, be_msg))

    pub_ok, pub_msg = probe_http(PUBLIC_PROBE)
    checks.append(("公网入口 :80", pub_ok, pub_msg))

    failed = [(name, msg) for name, ok, msg in checks if not ok]
    for name, ok, msg in checks:
        log("  %-18s %s  (%s)" % (name, "OK" if ok else "FAIL", msg))

    st = load_state()
    was_down = bool(st.get("down"))

    if failed:
        detail = "".join("<li><b>%s</b>：%s</li>" % (n, m) for n, m in failed)
        if not was_down:
            body = (
                "<p>监测发现研报站点异常：</p>"
                "<ul>%s</ul>"
                "<p>请检查：</p>"
                "<pre style='background:#f5f5f5;padding:10px;border-radius:6px'>"
                "systemctl status %s\n"
                "journalctl -u %s -n 50 --no-pager\n"
                "tail -50 /var/log/caddy/research-reports.access.log</pre>"
                "<p style='color:#8a919f;font-size:12px'>"
                "本邮件由服务监测脚本自动发出；同一故障只通知一次。</p>" % (detail, SERVICE, SERVICE)
            )
            ok, msg = send_mail(cfg, "[研报站点] 服务异常告警", body)
            log("告警邮件: %s" % ("已发送" if ok else "发送失败(%s)" % msg))
            save_state({"down": True, "since": datetime.now().isoformat(timespec="seconds"),
                        "detail": [n for n, _ in failed]})
        else:
            log("仍处于故障状态，不重复发信（上次故障时间 %s）" % st.get("since"))
        return 1

    # 全部正常
    if was_down:
        body = (
            "<p>研报站点已恢复正常。</p>"
            "<p>故障开始时间：%s</p>" % st.get("since", "未知")
        )
        ok, msg = send_mail(cfg, "[研报站点] 服务已恢复", body)
        log("恢复邮件: %s" % ("已发送" if ok else "发送失败(%s)" % msg))
        save_state({"down": False, "recovered_at": datetime.now().isoformat(timespec="seconds")})
    else:
        save_state({"down": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
