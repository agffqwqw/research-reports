#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""研报站点 · 异地备份（打包 + 加密 + 邮件外发）

为什么需要这一步：
    backup.sh 把备份写在 /opt/research-reports-backups/data/，与本机同盘。
    它只防「误删」，不防「整机 / 磁盘损毁」。本脚本把最新一份备份加密后
    发到站外邮箱，这样即使服务器整机不可用，数据仍能从邮箱取回。

配置：
    backup-mail.env   同目录，600 root —— SMTP 与收件人
    .backup-pass      同目录，600 root —— 加密口令（单行）
    ⚠️ 口令随邮件正文一并发出：这是使用方明确选择的取舍，
       代价是「邮箱账号被攻破」时这层加密不再提供保护。

用法：
    python3 offsite-backup.py --dry-run   # 只打包加密，不发信（自测）
    python3 offsite-backup.py            # 完整执行
"""
from __future__ import annotations

import argparse
import glob
import smtplib
import subprocess
import sys
import tarfile
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKUP_DIR = Path("/opt/research-reports-backups/data")
CONF = HERE / "backup-mail.env"
PASS_FILE = HERE / ".backup-pass"


def log(m: str) -> None:
    print("[offsite] %s %s" % (time.strftime("%F %T"), m), flush=True)


def load_env(p: Path) -> dict:
    cfg = {}
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def newest(pat: str):
    hits = sorted(glob.glob(str(BACKUP_DIR / pat)))
    return Path(hits[-1]) if hits else None


def pack(dest: Path):
    db = newest("app-*.db.gz")
    if db is None:
        raise SystemExit("找不到 SQLite 备份（app-*.db.gz）—— 请先确认 backup.sh 正常")
    rdb = newest("dump-*.rdb")
    names = [db.name]
    # ⚠️ 必须用 Python 的 tarfile，**不要**改成 GNU tar 命令行。
    #    两者在本机 Windows 上有本质差别：
    #      tarfile.open(Windows绝对路径)      → 正常
    #      tar -czf C:/.../x.tar.gz ...        → "Cannot connect to C: resolve failed"（冒号被当成 host:path）
    #    2026-10-06 已在 pack.sh 上踩到这个坑（阻塞级），记录在该脚本第 3 步注释。
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(db, arcname=db.name)
        if rdb:
            tf.add(rdb, arcname=rdb.name)
            names.append(rdb.name)
    return names


def encrypt(src: Path, dest: Path) -> None:
    # Python 标准库没有 AES，装 cryptography 会引入依赖；openssl 系统自带且零成本
    subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
         "-in", str(src), "-out", str(dest), "-pass", "file:" + str(PASS_FILE)],
        check=True,
    )


def send_mail(cfg: dict, subject: str, body: str, att: Path) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("MAIL_FROM") or cfg["MAIL_USER"]
    msg["To"] = cfg["BACKUP_TO"]
    msg.set_content(body)
    msg.add_attachment(att.read_bytes(), maintype="application",
                       subtype="octet-stream", filename=att.name)
    host, port = cfg["MAIL_HOST"], int(cfg.get("MAIL_PORT", "465"))
    if port == 465:                      # 465 = 隐式 SSL
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
            s.send_message(msg)
    else:                                # 587 = STARTTLS
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(cfg["MAIL_USER"], cfg["MAIL_PASSWORD"])
            s.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打包加密，不发信")
    args = ap.parse_args()

    cfg = load_env(CONF)
    if not PASS_FILE.exists():
        log("!! 缺少口令文件 %s" % PASS_FILE)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="offsite-") as td:
        tar_p = Path(td) / ("research-reports-%s.tar.gz" % stamp)
        enc_p = Path(td) / ("research-reports-%s.tar.gz.enc" % stamp)
        names = pack(tar_p)
        encrypt(tar_p, enc_p)
        kb = enc_p.stat().st_size / 1024
        log("已打包 %s；加密后 %.1f KB" % (", ".join(names), kb))

        if args.dry_run:
            log("--dry-run：跳过发信")
            return 0

        miss = [k for k in ("MAIL_HOST", "MAIL_USER", "MAIL_PASSWORD", "BACKUP_TO")
                if not cfg.get(k)]
        if miss:
            log("!! 配置缺失：%s → 请补齐 %s" % (", ".join(miss), CONF))
            return 3

        body = (
            "研报站点 · 异地备份\n\n"
            "时间：%s\n内容：%s\n附件：%s（AES-256-CBC / PBKDF2-200000，%.1f KB）\n\n"
            "解密：\n"
            "  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in %s \\\n"
            "    -out rr.tar.gz -pass pass:'<下方口令>'\n"
            "  tar -xzvf rr.tar.gz\n\n"
            "恢复：\n"
            "  1) gunzip -c app-<时间戳>.db.gz > app.db\n"
            "  2) systemctl stop research-reports\n"
            "  3) 覆盖 /opt/research-reports/backend/data/app.db（先备份原文件）\n"
            "  4) systemctl start research-reports\n\n"
            "口令：%s\n"
        ) % (stamp, ", ".join(names), enc_p.name, kb, enc_p.name,
             PASS_FILE.read_text(encoding="utf-8").strip())

        send_mail(cfg, "[研报站点·备份] %s" % stamp, body, enc_p)
        log("已发送至 %s" % cfg["BACKUP_TO"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
