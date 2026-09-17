#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研报站点 · 主动告警（备份新鲜度 + 资源阈值）

为什么需要它：
    healthcheck.py 监测的是「站点服务是否活着」，但有两类故障它抓不到：
      ① 备份静默失败 —— 2026-09-17 真实发生过：服务器没装 sqlite3，
         backup.sh 连续两天失败而无人知晓，RPO 从 1 天静默退化为 ∞
      ② 资源耗尽 —— 磁盘满 / 内存耗尽，2C2G 小机器最常见的静默死法

设计要点：
    · 复用 deploy/backup-mail.env 的 SMTP 配置（与异地备份共用一条通道）
    · **只在异常时发信**；同一问题 REPEAT_HOURS 内不重复；恢复时补一封「已恢复」
    · 阈值集中在「阈值区」，改这里即可
    · 状态文件放 /var/lib/research-reports/（不是代码目录）

用法：
    python3 alert.py            # 正常检查
    python3 alert.py --dry-run  # 只打印，不发信
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUP_DIR = Path("/opt/research-reports-backups/data")
OFFSITE_LOG = Path("/var/log/research-reports/offsite.log")
CONF = HERE / "backup-mail.env"
STATE = Path("/var/lib/research-reports/alert-state.json")

# ---------------------------------------------------------------- 阈值区
MAX_BACKUP_AGE_H = 26      # 本地备份最大年龄（每天 03:15 跑 → 26h 留一天余量）
MAX_OFFSITE_AGE_H = 26     # 异地外发最大年龄
DISK_USED_PCT = 85         # 根分区使用率上限
MEM_AVAIL_PCT = 10         # 可用内存下限（%）
LOAD_PER_CPU = 2.0         # 1 分钟负载 / CPU 核数 上限
REPEAT_HOURS = 6           # 同一问题多久内不重复告警


def log(m: str) -> None:
    print("[alert] %s %s" % (time.strftime("%F %T"), m), flush=True)


def load_env(p: Path) -> dict:
    cfg: dict = {}
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def newest_age_h(pattern: str):
    hits = glob.glob(str(BACKUP_DIR / pattern))
    if not hits:
        return None
    newest = max(hits, key=os.path.getmtime)
    return (time.time() - os.path.getmtime(newest)) / 3600.0


def offsite_age_h():
    """从 offsite.log 里找最后一次「已发送至」的时间。"""
    if not OFFSITE_LOG.exists():
        return None
    last = None
    try:
        for line in OFFSITE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            if "已发送至" in line:
                parts = line.split()
                if len(parts) >= 3:            # [offsite] 2026-09-17 13:40:14 已发送至 ...
                    try:
                        last = time.mktime(time.strptime(parts[1] + " " + parts[2],
                                                          "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        pass
    except OSError:
        return None
    return None if last is None else (time.time() - last) / 3600.0


def mem_avail_pct():
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])   # kB
        total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        return (avail / total * 100.0) if total else None
    except (OSError, ValueError, IndexError):
        return None


def collect():
    """返回 [(key, 标题, 详情, 是否异常)]"""
    out = []

    age = newest_age_h("app-*.db.gz")
    if age is None:
        out.append(("backup_missing", "本地备份不存在",
                    "备份目录里找不到任何 app-*.db.gz —— 备份很可能从未成功过", True))
    else:
        out.append(("backup_stale", "本地备份过期",
                    "最新备份已 %.1f 小时未更新（阈值 %dh）" % (age, MAX_BACKUP_AGE_H),
                    age > MAX_BACKUP_AGE_H))

    oage = offsite_age_h()
    if oage is not None:
        out.append(("offsite_stale", "异地外发过期",
                    "最后一次成功外发距今 %.1f 小时（阈值 %dh）" % (oage, MAX_OFFSITE_AGE_H),
                    oage > MAX_OFFSITE_AGE_H))

    du = shutil.disk_usage("/")
    pct = du.used / du.total * 100.0
    out.append(("disk", "磁盘使用率过高",
                "根分区已用 %.1f%%（%.1f / %.1f GB，阈值 %d%%）"
                % (pct, du.used / 2**30, du.total / 2**30, DISK_USED_PCT),
                pct >= DISK_USED_PCT))

    mp = mem_avail_pct()
    if mp is not None:
        out.append(("memory", "可用内存过低",
                    "可用内存仅 %.1f%%（阈值 %d%%）" % (mp, MEM_AVAIL_PCT),
                    mp <= MEM_AVAIL_PCT))

    try:
        la = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        out.append(("load", "系统负载过高",
                    "1 分钟负载 %.2f（%d 核，阈值 %.1f / 核）" % (la, cpus, LOAD_PER_CPU),
                    la > cpus * LOAD_PER_CPU))
    except OSError:
        pass

    return out


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(st: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st), encoding="utf-8")
    except OSError as e:
        log("状态文件写入失败：%s" % e)


def send(cfg: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("MAIL_FROM") or cfg["MAIL_USER"]
    msg["To"] = cfg.get("ALERT_TO") or cfg["BACKUP_TO"]
    msg.set_content(body)
    host, port = cfg["MAIL_HOST"], int(cfg.get("MAIL_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
            s.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发信")
    args = ap.parse_args()

    checks = collect()
    bad = [c for c in checks if c[3]]
    log("检查 %d 项，异常 %d 项" % (len(checks), len(bad)))
    for key, title, detail, is_bad in checks:
        log("  [%s] %s" % ("异常" if is_bad else "正常", detail))

    now = time.time()
    st = read_state()
    bad_keys = {c[0] for c in bad}

    to_send = []
    for key, title, detail, _ in bad:
        last = st.get(key)
        if last is None or now - last > REPEAT_HOURS * 3600:
            to_send.append((key, title, detail))
            st[key] = now

    recovered = [k for k in list(st.keys()) if k not in bad_keys]
    for k in recovered:
        st.pop(k, None)

    if args.dry_run:
        log("--dry-run：跳过发信（待发 %d 条 / 恢复 %d 条）" % (len(to_send), len(recovered)))
        return 0

    cfg = load_env(CONF)
    miss = [k for k in ("MAIL_HOST", "MAIL_USER", "MAIL_PASSWORD", "BACKUP_TO") if not cfg.get(k)]
    if miss:
        log("!! 配置缺失：%s → 请补齐 %s" % (", ".join(miss), CONF))
        return 3

    host = os.uname().nodename
    if to_send:
        body = "研报站点 · 告警\n\n主机：%s\n时间：%s\n\n" % (host, time.strftime("%F %T"))
        for _, title, detail in to_send:
            body += "【%s】\n%s\n\n" % (title, detail)
        body += "——\n由 deploy/alert.py 自动发出；同一问题 %d 小时内不重复提醒。\n" % REPEAT_HOURS
        send(cfg, "[研报站点·告警] %s" % to_send[0][1], body)
        log("已发出告警邮件（%d 条）" % len(to_send))
    elif recovered:
        body = ("研报站点 · 告警已恢复\n\n主机：%s\n时间：%s\n已恢复正常：%s\n"
                % (host, time.strftime("%F %T"), "、".join(recovered)))
        send(cfg, "[研报站点·已恢复]", body)
        log("已发出恢复通知（%d 条）" % len(recovered))

    write_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
