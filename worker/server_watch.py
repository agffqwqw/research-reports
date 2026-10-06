# -*- coding: utf-8 -*-
"""服务器健康监测（本机侧，经 SSH 探测）

为什么要放在**本机**而不是服务器上：
    服务器上的监测脚本（deploy/healthcheck.py）在「服务全挂」时同样发不出告警 ——
    它的通知通道（邮件 / 队列）都依赖服务本身活着。
    而「服务挂了」恰恰是最需要告警的时刻。
    放在本机、用 SSH 从外部探测，才能真正做到「服务挂了也能告诉你」。

检测五项：
    1. 三个 systemd 服务是否 active（research-reports / caddy / redis-server）
    2. 后端 /health（能抓「进程在但假死」）
    3. 经 Caddy 的首页（能抓「后端好但入口挂」）
    4. 根分区磁盘使用率
    5. SSH 本身是否可达（服务器关机/断网时最直接的信号）

**边沿触发**：状态写在 .server_watch_state.json，同一故障只报一次，
恢复时再报一次「已恢复」—— 否则每小时一封，很快就被当垃圾消息。

用法：
    python server_watch.py            # 有变化才有输出
    python server_watch.py --verbose  # 无论如何都打印详情

退出码（供 automation 判断是否发飞书）：
    0  无需通知（一切正常，或故障已通知过）
    1  **新发现故障** → 需要通知
    2  **已恢复**     → 需要通知
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_local_env(path: str) -> dict:
    """读取本地 .env 风格的配置文件（KEY=VALUE），文件不存在则返回空 dict。

    为什么需要它（2026-10-06 修）：
      本脚本此前把服务器地址**写成环境变量的默认值**，形如
      `os.environ.get("WATCH_HOST", "root@<真实IP>")`。那份默认值会随仓库
      公开而泄漏服务器坐标与登录名。
      改为「环境变量 → 本地 watch.env → 明确报错」三级之后：
        · 仓库里只剩占位符（别人 clone 看不到任何真实地址）
        · 本机把真实值放进 worker/watch.env（已被 .gitignore 排除），零改动继续跑
    """
    data: dict = {}
    if not os.path.isfile(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


_LOCAL = _load_local_env(os.path.join(HERE, "watch.env"))


def _cfg(key: str, default: str = "") -> str:
    """优先级：真实环境变量 > 本地 watch.env > default。"""
    return os.environ.get(key) or _LOCAL.get(key, default)


# ssh 可执行文件：Windows 下 OpenSSH 有固定路径，其它平台退回 PATH 上的 ssh
_win_ssh = r"C:\Windows\System32\OpenSSH\ssh.exe"
SSH = _cfg("WATCH_SSH") or (_win_ssh if os.path.exists(_win_ssh)
                            else (shutil.which("ssh") or "ssh"))

# 探测目标（**仓库内不写死任何真实地址**）
#   WATCH_HOST：ssh 目标，形如 user@your-host
#   WATCH_SITE：站点对外地址（证书 SAN / Caddy site 块用的就是它）。
#     探测时用 --resolve 把它指到 127.0.0.1，这样 SNI 与 Host 都是真实域名/IP，
#     才能命中 Caddy 的 site 块，而不是落到默认站点上。
HOST = _cfg("WATCH_HOST")
SITE = _cfg("WATCH_SITE")
if not HOST or not SITE:
    sys.stderr.write(
        "✗ 未配置探测目标，无法运行。任选一种方式：\n"
        "  1) 设环境变量：WATCH_HOST=user@your-host  WATCH_SITE=your-host\n"
        "  2) 复制模板后填写：cp worker/watch.env.example worker/watch.env\n"
        "  （watch.env 已被 .gitignore 排除，不会进版本库）\n"
    )
    sys.exit(3)

SERVICES = ["research-reports", "caddy", "redis-server"]
DISK_WARN_PCT = 85

STATE_FILE = os.path.join(HERE, ".server_watch_state.json")

REMOTE_SCRIPT = "\n".join([
    "for s in " + " ".join(SERVICES) + "; do",
    '  echo "SVC $s $(systemctl is-active $s 2>/dev/null || echo inactive)"',
    "done",
    'echo "HEALTH $(curl -s --max-time 6 -o /dev/null -w \'%{http_code}\' http://127.0.0.1:8081/health)"',
    'echo "HEALTH_BODY $(curl -s --max-time 6 http://127.0.0.1:8081/health)"',
    # ⚠️ 下面两条是「入口探活」，取样点必须区分协议：
    #   80  → 期望 3xx（HTTP 强制跳转 HTTPS，**跳转本身就是健康的表现**）
    #   443 → 期望 200（真正在服务首页）
    #   历史 bug：早期只用 http://127.0.0.1/ 探活并要求 == 200，
    #   但该请求被 Caddyfile 设计为跳转 → 常年误报「Caddy 异常」。
    'echo "HTTP_REDIR $(curl -s --max-time 6 -o /dev/null -w \'%%{http_code}\' '
    '--resolve %s:80:127.0.0.1 http://%s/)"' % (SITE, SITE),
    'echo "VIA_CADDY $(curl -sk --max-time 6 -o /dev/null -w \'%%{http_code}\' '
    '--resolve %s:443:127.0.0.1 https://%s/)"' % (SITE, SITE),
    "echo \"DISK $(df -P / | awk 'NR==2 {print $5}' | tr -d '%')\"",
    'echo "RESTARTS $(systemctl show research-reports -p NRestarts --value 2>/dev/null || echo 0)"',
])


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def run_ssh(cmd: str, timeout: int = 45):
    try:
        r = subprocess.run(
            [SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=10", HOST, cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        return r.returncode, r.stdout or "", (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "", "SSH 超时（%ds）" % timeout
    except Exception as exc:  # noqa: BLE001
        return -1, "", "%s: %s" % (type(exc).__name__, exc)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def collect_problems():
    """返回 (problems, details, extra_notes)"""
    code, out, err = run_ssh(REMOTE_SCRIPT)

    if code != 0:
        return (
            ["服务器不可达（SSH 探测失败）"],
            ["错误：%s" % (err.strip()[:200] or "退出码 %d" % code),
             "可能原因：实例已关机 / 网络故障 / SSH 服务异常 / IP 变更",
             "处理建议：登录腾讯云控制台查看实例状态"],
            [],
        )

    data = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        k, v = line.split(" ", 1)
        if k == "SVC":
            data.setdefault("svc", []).append(v)
        else:
            data[k] = v

    problems, notes, details = [], [], []

    svc_pairs = []
    for item in data.get("svc", []):
        parts = item.split(" ", 1)
        if len(parts) == 2:
            svc_pairs.append((parts[0], parts[1]))
    for name, state in svc_pairs:
        if state != "active":
            problems.append("服务未运行：%s（当前 %s）" % (name, state))

    health_code = data.get("HEALTH", "0")
    if health_code not in ("200", "503"):
        problems.append("后端 /health 无响应（HTTP %s）" % health_code)
    elif health_code == "503":
        notes.append("后端处于 degraded（Redis 可能未就绪），浏览功能不受影响")

    caddy_code = data.get("VIA_CADDY", "0")
    if caddy_code != "200":
        problems.append("经 Caddy 的 HTTPS 首页异常（HTTP %s）—— 可能是 Caddy 配置 / 证书 / 反代问题" % caddy_code)

    # 80 端口期望「跳转」（3xx）而不是 200 —— 跳转正是 HTTP→HTTPS 强制策略在生效
    redir_code = data.get("HTTP_REDIR", "0")
    if not redir_code.startswith("3"):
        problems.append("80 端口未按预期跳转 HTTPS（HTTP %s）—— 可能是 Caddyfile 少了 redir 段" % redir_code)

    disk = -1
    try:
        disk = int(data.get("DISK", "0"))
        if disk >= DISK_WARN_PCT:
            problems.append("根分区磁盘使用率 %d%%（超过 %d%% 阈值）" % (disk, DISK_WARN_PCT))
    except ValueError:
        pass

    try:
        restarts = int(data.get("RESTARTS", "0"))
    except ValueError:
        restarts = 0
    if restarts >= 5:
        notes.append("服务已重启 %d 次，可能存在反复崩溃" % restarts)

    for name, state in svc_pairs:
        details.append("  %-20s %s" % (name, state))
    details.append("  %-20s HTTP %s" % ("后端 /health", health_code))
    details.append("  %-20s HTTP %s" % ("经 Caddy 的 HTTPS 首页", caddy_code))
    details.append("  %-20s HTTP %s" % ("80 端口跳转", redir_code))
    details.append("  %-20s %s" % ("磁盘使用率", ("%d%%" % disk) if disk >= 0 else "?"))
    details.append("  %-20s %d 次" % ("服务重启次数", restarts))

    return problems, details, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true", help="正常时也打印详情")
    ap.add_argument("--reset", action="store_true", help="清除状态记录后重新判断")
    args = ap.parse_args()

    if args.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    problems, details, notes = collect_problems()
    st = load_state()
    was_down = bool(st.get("down"))

    if args.verbose:
        print("=== 服务器健康检查 ===")
        for d in details:
            print(d)
        for n in notes:
            print("  [提示] %s" % n)
        print()

    # ---------- 异常 ----------
    if problems:
        if was_down:
            # 同一故障已通知过，不再重复打扰
            if args.verbose:
                print("⚠️ 仍处于故障状态（已于 %s 通知过，本次不重复）" % st.get("since"))
                for i, p in enumerate(problems, 1):
                    print("  %d) %s" % (i, p))
            return 0

        save_state({"down": True, "since": now_iso(), "problems": problems})
        print("⚠️ 服务器异常（%d 项）：" % len(problems))
        for i, p in enumerate(problems, 1):
            print("  %d) %s" % (i, p))
        if notes:
            for n in notes:
                print("  [提示] %s" % n)
        print()
        print("--- 详细状态 ---")
        for d in details:
            print(d)
        return 1

    # ---------- 正常 ----------
    if was_down:
        save_state({"down": False, "recovered_at": now_iso()})
        print("✅ 服务器已恢复正常")
        print("  故障开始于：%s" % st.get("since", "未知"))
        print("  当时的问题：%s" % "；".join(st.get("problems", []) or ["未知"]))
        print()
        print("--- 当前状态 ---")
        for d in details:
            print(d)
        return 2

    save_state({"down": False, "checked_at": now_iso()})
    if args.verbose:
        print("✅ 全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
