# -*- coding: utf-8 -*-
"""本机 Worker —— 研报生成流水线（W1/W2/W3/W5）

职责分工（重要）：
    本脚本只做**机械部分**：轮询任务、抓取公告、下载 PDF、抽取全文、回传结果。
    **六维度评判由 WorkBuddy 的定时任务里的 AI 完成** —— 那一步需要理解财报，
    不是脚本能替代的。

典型用法（供 AI 或人工调用）：
    python worker.py check                          # 看队列有没有任务
    python worker.py claim                          # 拉取一个任务（JSON 输出）
    python worker.py fetch 600519 2026H1            # 抓公告+下载+抽取，输出索引
    python worker.py show 600519 2026H1 7,9-10      # 按页查看全文（供评判用）
    python worker.py submit 3 <report.json路径>      # 回传成功（扣次数）
    python worker.py fail 3 "抓不到半年报"            # 回传失败（不扣次数）

通知队列（服务器入队 → 本机发送，如飞书推送）：
    python worker.py notice peek                    # 看有没有待发通知
    python worker.py notice pull                    # 取一条（JSON：title/body/link）
    python worker.py notice done 1                  # 发送成功
    python worker.py notice fail 1 "webhook 超时"    # 发送失败（自动重试，上限 3 次）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 配置
def load_env(path: Path | None = None) -> dict:
    p = path or (HERE / "worker.env")
    cfg: dict[str, str] = {}
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    # ----------------------------------------------------------------
    # 缺省值：从「惯例位置」推导，**不写死任何绝对路径**。
    #
    # ⚠️ 早期版本这里硬编码了 C:\Users\<某人>\... 的绝对路径。问题有三层：
    #    1) 别人 clone 后拿到的是「一条指向别人电脑的路径」——必然跑不起来，
    #       而报错信息只会说「找不到目录」，不会提示该改这里；
    #    2) 路径里带用户名，属于无谓的信息暴露；
    #    3) 它掩盖了配置错误：worker.env 里忘填时不会报错，而是静默用错路径。
    #    → 现在改为按 HOME 推导，跨平台通用；填错时下方 _validate() 会明确报出来。
    # ----------------------------------------------------------------
    home = Path.home()
    cfg.setdefault("SERVER_URL", "http://127.0.0.1:8081")
    # 技能包：优先找 ~/.workbuddy/skills/<名字>，其次 ~/.workbuddy/skills 下的同名目录
    _skill_root = home / ".workbuddy" / "skills"
    _skill_guess = _skill_root / "cninfo-report-deep-dive"
    cfg.setdefault("SKILL_DIR", str(_skill_guess))
    # Python 解释器：优先当前正在跑 worker 的这个解释器（最不容易错）
    cfg.setdefault("VENV_PY", sys.executable)
    cfg.setdefault("WORKDIR", str(home / ".workbuddy-worker"))
    if not cfg.get("WORKER_TOKEN"):
        # 回退：从 backend/app.env 读，省得两份都填
        be = HERE.parent / "backend" / "app.env"
        if be.exists():
            for raw in be.read_text(encoding="utf-8").splitlines():
                if raw.strip().startswith("WORKER_TOKEN="):
                    cfg["WORKER_TOKEN"] = raw.split("=", 1)[1].strip()
    return cfg


def preflight(cfg: dict, *, need_skill: bool = True) -> list[str]:
    """开工前自检，返回问题列表（空 = 全部就绪）。

    为什么必须做：worker 的失败模式几乎全是「静默用错路径」——
      · SKILL_DIR 不存在 → 生成出来的研报内容是空的，但任务标记为 success；
      · VENV_PY 不存在 → 抽取 PDF 时崩在子进程里，报错原文很难懂；
      · WORKER_TOKEN 为空 → 服务端返回 401，看着像"没任务"。
    这些都会让人误判成"技能包不好用"或"服务端有问题"，而根因只是配置。

    因此这里把「路径是否存在」「令牌是否已填」显式查一遍，
    并给出**可操作的修复指引**，而不是等到深处才炸。
    """
    problems: list[str] = []

    url = cfg.get("SERVER_URL", "")
    if not url:
        problems.append("SERVER_URL 未填（示例：http://127.0.0.1:8081 或 https://<你的站点>）")

    if not cfg.get("WORKER_TOKEN"):
        problems.append(
            "WORKER_TOKEN 未填 —— 它是「服务器信任本机」的凭据，"
            "必须与服务器 app.env 里的值一致（生成：openssl rand -base64 32）"
        )

    if need_skill:
        skill = Path(cfg.get("SKILL_DIR") or "")
        if not str(skill) or not skill.is_dir():
            problems.append(
                f"SKILL_DIR 不存在或未填：{skill or '(空)'}\n"
                f"       → 六维度评判的技能包不在这个位置。请把 worker.env 里的 SKILL_DIR "
                f"改成你自己的技能包目录（本机默认 ~/.workbuddy/skills/cninfo-report-deep-dive）"
            )

        venv = Path(cfg.get("VENV_PY") or "")
        # 允许写成命令名（走 PATH 查找），也允许写绝对路径
        if not (venv.is_file() or shutil.which(cfg.get("VENV_PY") or "")):
            problems.append(
                f"VENV_PY 不是可执行文件：{venv or '(空)'}\n"
                f"       → 这是跑技能包脚本的 Python，需装 pdfplumber / pypdfium2。"
                f"可填当前解释器：{sys.executable}"
            )
    return problems


CFG = load_env()
TASK_DIR = Path(CFG["WORKDIR"])


# ---------------------------------------------------------------- HTTP
class _KeepMethodRedirect(urllib.request.HTTPRedirectHandler):
    """跳转时保留原请求方法与请求体。

    urllib 默认对 301/302 会把 POST 降级成 GET（沿袭旧浏览器行为）。
    本机 worker 的 SERVER_URL 若写成 http://，Caddy 会 301 跳到 https://，
    于是 submit 的 POST 变成 GET，服务端返回 **405 Method Not Allowed**，
    而 check/claim 因为是 GET 反而不受影响 —— 现象极易误判成服务端缺路由。
    实测踩坑：submit 405，改为 https:// 后立即恢复。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and code in (301, 302) and req.get_method() == "POST":
            new.data = req.data
            new.method = "POST"
            ct = req.get_header("Content-type")
            if ct:
                new.add_unredirected_header("Content-Type", ct)
        return new


_OPENER = urllib.request.build_opener(
    _KeepMethodRedirect,
    # ⚠️ 显式禁用代理。
    #    urllib 默认会读 HTTP_PROXY / HTTPS_PROXY 环境变量并走系统代理。
    #    在 Windows 上这会导致连自己的站点也绕一圈代理 → 典型报错：
    #        URLError: <urlopen error Tunnel connection failed: 502 Bad Gateway>
    #    现象极具迷惑性：浏览器/curl 打得开，正是 worker 打不开，
    #    容易误判成"服务端挂了"或"令牌不对"。
    #    worker 的目标只有自己的站点，直连永远是对的。
    urllib.request.ProxyHandler({}),
)


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = CFG["SERVER_URL"].rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Worker-Token", CFG.get("WORKER_TOKEN", ""))
    try:
        with _OPENER.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {"detail": "HTTP %d" % e.code}
    except Exception as e:  # noqa: BLE001
        return 0, {"detail": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- 动作
def cmd_check(_args) -> int:
    """只读查看队列 —— 用 peek 接口，不会把任务改成 processing。"""
    code, data = _call("GET", "/api/worker/tasks/peek")
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    items = data.get("items") or []
    if not items:
        print("队列为空：没有待处理任务")
        return 0
    pending = [x for x in items if x.get("status") == "pending"]
    print("队列中 %d 个任务（待处理 %d / 进行中 %d）：" % (len(items), len(pending), len(items) - len(pending)))
    for x in items:
        print("  #%-3s %-8s %-8s %-10s %s" % (
            x.get("id"), x.get("company_code"), x.get("company_name") or "",
            x.get("status"), x.get("created_at", "")))
    if pending:
        print()
        print("提示：用 `python worker.py claim` 正式拉取队首任务")
    return 0


def cmd_claim(_args) -> int:
    code, data = _call("GET", "/api/worker/tasks/next")
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    task = data.get("task")
    if not task:
        print("EMPTY")
        return 0
    print(json.dumps(task, ensure_ascii=False))
    return 0


def _task_dir(code: str, period: str) -> Path:
    d = TASK_DIR / ("%s_%s" % (code, period))
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_fetch(args) -> int:
    """抓公告 → 下载 PDF → 抽取全文 + 建索引（W2 的机械部分）"""
    code, period = args.code, args.period
    year = period[:4]
    d = _task_dir(code, period)
    venv = CFG["VENV_PY"]
    skill = Path(CFG["SKILL_DIR"])

    # 1) 查 orgId
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    HDR = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
           "Referer": "http://www.cninfo.com.cn/"}

    def post(url, payload):
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=HDR, method="POST")
        with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
            return r.read().decode("utf-8", "replace")

    hits = json.loads(post("http://www.cninfo.com.cn/new/information/topSearch/query",
                           {"keyWord": code, "maxSecNum": "10"}))
    org = next((h["orgId"] for h in hits if h.get("category") == "A股"), None)
    name = next((h.get("zwjc", "") for h in hits if h.get("category") == "A股"), "")
    if not org:
        print(json.dumps({"ok": False, "error": "未找到该 A 股"}, ensure_ascii=False))
        return 1

    # 2) 找定期报告（标题必须以「年半年度报告」/「年年度报告」结尾）
    suffix = "年半年度报告" if period.endswith("H1") else "年年度报告"
    obj = json.loads(post("http://www.cninfo.com.cn/new/hisAnnouncement/query", {
        "stock": "%s,%s" % (code, org), "tabName": "fulltext", "pageSize": "60",
        "pageNum": "1", "column": "szse", "category": "", "plate": "",
        "seDate": "%s-01-01~%s-12-31" % (year, year),
        "searchkey": "年度报告" if suffix.endswith("年度报告") else "半年度报告",
        "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true"}))
    picked = None
    for x in obj.get("announcements") or []:
        t = x.get("announcementTitle", "").replace("<em>", "").replace("</em>", "").strip()
        if t.endswith(suffix) and "摘要" not in t and "英文" not in t:
            picked = x
            break
    if not picked:
        print(json.dumps({"ok": False, "error": "未找到 %s 的%s正文" % (code, suffix)},
                         ensure_ascii=False))
        return 1

    # ⚠️ 披露日期按**北京时间**取：巨潮时间戳用 UTC 解析会少一天
    #    （北京时间 08-29 00:00 = UTC 08-28 16:00），
    #    而服务端防重复生成要靠这个日期比对，差一天就会被误判成「新报告」。
    import datetime
    CN_TZ = datetime.timezone(datetime.timedelta(hours=8))
    disc = datetime.datetime.fromtimestamp(
        picked["announcementTime"] / 1000, CN_TZ).strftime("%Y-%m-%d")
    url = "http://static.cninfo.com.cn/" + picked["adjunctUrl"]

    # 3) 下载
    pdf = d / ("%s.pdf" % code)
    if not pdf.exists():
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=180, context=ctx) as r:
            pdf.write_bytes(r.read())

    # 4) 抽取（复用技能包脚本）
    t0 = __import__("time").time()
    subprocess.run([venv, str(skill / "scripts" / "extract_report.py"),
                    str(pdf), str(d / code)], capture_output=True, timeout=900)

    fulltext = d / ("%s_fulltext.txt" % code)
    index = d / ("%s_index.json" % code)
    result = {
        "ok": True,
        "code": code, "name": name, "period": period,
        "disclosure_date": disc, "source_url": url,
        "pdf_mb": round(pdf.stat().st_size / 1048576, 2),
        "fulltext_chars": fulltext.stat().st_size if fulltext.exists() else 0,
        "index_json": str(index) if index.exists() else "",
        "fulltext_txt": str(fulltext),
        "dir": str(d),
        "extract_seconds": round(__import__("time").time() - t0, 1),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if index.exists():
        idx = json.loads(index.read_text(encoding="utf-8"))
        hits_kw = {k: v for k, v in idx.items() if v}
        print("\n关键词页码索引（供按页取数）：")
        for k, v in sorted(hits_kw.items(), key=lambda x: -(len(x[1]) if isinstance(x[1], list) else 0))[:25]:
            print("  %-20s %s" % (k, v[:10]))
    return 0


def cmd_show(args) -> int:
    """按页查看已抽取的全文（供 AI 做六维度评判）"""
    d = _task_dir(args.code, args.period)
    txt = d / ("%s_fulltext.txt" % args.code)
    if not txt.exists():
        print("未找到全文，请先执行 fetch")
        return 1
    venv = CFG["VENV_PY"]
    skill = Path(CFG["SKILL_DIR"])
    r = subprocess.run([venv, str(skill / "scripts" / "show_pages.py"), str(txt), args.pages],
                       capture_output=True, timeout=120)
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    return 0


def cmd_submit(args) -> int:
    rep_path = Path(args.report)
    if not rep_path.exists():
        print("report 文件不存在:", rep_path)
        return 1
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    code, data = _call("POST", "/api/worker/tasks/%s/result" % args.task_id,
                       {"status": "success", "report": rep})
    print("[%s] %s" % (code, json.dumps(data, ensure_ascii=False)))
    return 0 if code == 200 else 1


def cmd_fail(args) -> int:
    code, data = _call("POST", "/api/worker/tasks/%s/result" % args.task_id,
                       {"status": "failed", "error_msg": args.reason})
    print("[%s] %s" % (code, json.dumps(data, ensure_ascii=False)))
    return 0 if code == 200 else 1


# ---------------------------------------------------------------- 通知队列
# 服务器只入队，发送动作由本机完成（服务器调不到飞书 / WorkBuddy 的 MCP 工具）。
# 典型用法：
#   python worker.py notice peek            # 看有没有待发通知（只读）
#   python worker.py notice pull            # 取一条（返回 JSON，含 title/body/link）
#   python worker.py notice done 1          # 发成功
#   python worker.py notice fail 1 "原因"    # 发失败（会自动重试，上限 3 次）
def cmd_notice_peek(_args) -> int:
    code, data = _call("GET", "/api/worker/notifications/peek")
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    items = data.get("items") or []
    if not items:
        print("通知队列为空")
        return 0
    print("待发/发送中 %d 条：" % len(items))
    for x in items:
        print("  #%-4s %-18s %-9s 尝试%s  %s" % (
            x.get("id"), x.get("kind"), x.get("status"),
            x.get("attempts", 0), x.get("title", "")[:46]))
    return 0


def cmd_notice_pull(_args) -> int:
    code, data = _call("GET", "/api/worker/notifications/next")
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    n = data.get("notification")
    if not n:
        print("EMPTY")
        return 0
    print(json.dumps(n, ensure_ascii=False))
    return 0


def cmd_notice_done(args) -> int:
    code, data = _call("POST", "/api/worker/notifications/%s/result" % args.notif_id,
                       {"status": "sent"})
    print("[%s] %s" % (code, json.dumps(data, ensure_ascii=False)))
    return 0 if code == 200 else 1


def cmd_notice_fail(args) -> int:
    code, data = _call("POST", "/api/worker/notifications/%s/result" % args.notif_id,
                       {"status": "failed", "error_msg": args.reason})
    print("[%s] %s" % (code, json.dumps(data, ensure_ascii=False)))
    return 0 if code == 200 else 1


# ---------------------------------------------------------------- 权限批准（代管理员执行）
# 为什么需要：邮件/飞书里的批准链接指向服务器地址，**服务未部署时打不开**。
# 本机 Worker 持有受信令牌，可以直接执行批准 —— 于是「在对话里说一句批准」
# 就能完成，不依赖任何链接可达性。部署上线后链接自然也能用，两条路并存。
#   python worker.py perm list              # 列出待批准的申请
#   python worker.py perm approve <token>   # 按 token 批准
#   python worker.py perm approve --id 3    # 或按申请 id 批准
def cmd_perm_list(_args) -> int:
    code, data = _call("GET", "/api/permissions/pending")
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    items = data.get("items") or []
    if not items:
        print("没有待批准的权限申请")
        return 0
    print("待批准 %d 条：" % len(items))
    for x in items:
        print("  #%-4s %-28s %-10s %s" % (
            x.get("id"), x.get("email"), x.get("scope_label"), x.get("created_at", "")))
    print()
    print("批准：python worker.py perm approve --id <id>")
    return 0


def cmd_perm_approve(args) -> int:
    body: dict = {}
    if getattr(args, "token", None):
        body["token"] = args.token
    elif getattr(args, "id", None):
        body["id"] = args.id
    else:
        print("请提供 token 或 --id")
        return 1
    code, data = _call("POST", "/api/permissions/approve", body)
    if code != 200:
        print("调用失败 [%s]: %s" % (code, data.get("detail")))
        return 1
    if data.get("already"):
        # 按 id 传时接口会返回 message（如「该申请已批准，无需重复处理」）
        msg = data.get("message")
        if msg:
            print(msg)
        else:
            print("该申请此前已批准过：%s（%s）" % (
                data.get("email") or "?", data.get("granted_label") or "?"))
        return 0
    print("已批准：%s 获得「%s」" % (data.get("email"), data.get("granted_label")))
    return 0


# ---------------------------------------------------------------- 入口
def cmd_doctor(args) -> int:
    """配置自检：把 worker 依赖的每一项逐条验一遍，人工可读地报出来。

    设计意图：worker 的坑基本都在「配置错但不报错」。
    本命令不写任何服务端状态（只对 /health 做一次只读请求）。
    """
    print("== Worker 配置自检 ==")
    ok = True

    def line(label: str, value: str, good: bool, hint: str = ""):
        nonlocal ok
        mark = "OK  " if good else "FAIL"
        print(f" [{mark}] {label}: {value}")
        if not good:
            ok = False
            if hint:
                print(f"         → {hint}")

    # 1) 配置文件
    env_path = HERE / "worker.env"
    line("worker.env", str(env_path), env_path.exists(),
         "复制模板：cp worker.env.example worker.env，然后填值")

    # 2) 服务端地址
    url = CFG.get("SERVER_URL", "")
    line("SERVER_URL", url or "(空)", bool(url),
         "本机开发填 http://127.0.0.1:8081；驱动线上填 https://<你的站点>")

    # 3) 令牌（不回显内容，只报长度）
    tok = CFG.get("WORKER_TOKEN", "")
    line("WORKER_TOKEN", f"已填（{len(tok)} 字符）" if tok else "(空)", bool(tok),
         "须与服务器 app.env 里的 WORKER_TOKEN 一致")

    # 4) 技能包目录
    skill = Path(CFG.get("SKILL_DIR") or "")
    line("SKILL_DIR", str(skill) or "(空)", bool(str(skill)) and skill.is_dir(),
         "六维度评判技能包所在目录（本机默认 ~/.workbuddy/skills/cninfo-report-deep-dive）")

    # 5) 解释器
    venv_raw = CFG.get("VENV_PY") or ""
    venv_ok = bool(venv_raw) and (Path(venv_raw).is_file() or shutil.which(venv_raw) is not None)
    line("VENV_PY", venv_raw or "(空)", venv_ok,
         f"需装 pdfplumber / pypdfium2；可直接用当前解释器：{sys.executable}")

    # 6) 工作目录（不存在不算错，会自动创建）
    wd = Path(CFG.get("WORKDIR") or "")
    print(f" [info] WORKDIR: {wd}  （{'已存在' if wd.is_dir() else '尚不存在，首次运行会自动创建'}）")

    # 7) 服务端连通性（只读）
    if url:
        try:
            st, r = _call("GET", "/health")
            if st == 200:
                print(f" [OK  ] 服务端 /health: {st} {json.dumps(r, ensure_ascii=False)[:110]}")
            else:
                ok = False
                print(f" [FAIL] 服务端 /health 返回 {st}: {json.dumps(r, ensure_ascii=False)[:110]}")
                if st == 0:
                    print("         → 连接没有建立（0 = 本地异常，非服务端返回）。常见原因：")
                    print("           1) 云防火墙未放行 443 —— 症状是**连接超时**（WinError 10060）。")
                    print("              服务器上自证：ssh 进去跑 curl -sk https://127.0.0.1/health")
                    print("              返回 200 就说明服务是好的，纯粹是端口没放行。")
                    print("           2) SERVER_URL 写成 http:// —— Caddy 会 301，POST 变 GET（405）。")
                    print("           3) 被系统代理拦（报 502 Tunnel failed）—— 本工具已禁用代理。")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f" [FAIL] 服务端 /health 不可达: {e}")
            print("         → 常见原因（按可能性排序）：")
            print("           1) 云防火墙未放行 443 —— 症状是**连接超时**（WinError 10060）。")
            print("              可在服务器上自证：ssh 进去跑 curl -sk https://127.0.0.1/health")
            print("              若返回 200，说明服务是好的，纯粹是端口没放行。")
            print("           2) SERVER_URL 写成 http:// —— Caddy 会 301，POST 变 GET（报 405）。")
            print("           3) 走系统代理被拦 —— worker 已显式禁用代理，若仍报 502 请查系统代理设置。")

        try:
            st, r = _call("GET", "/api/worker/tasks/peek")
            n = r.get("pending_count")
            if st == 200:
                print(f" [OK  ] 队列 peek: {st} pending_count={n}"
                      f"{'（队列为空）' if n == 0 else ''}")
            else:
                ok = False
                print(f" [FAIL] 队列 peek 返回 {st}: {json.dumps(r, ensure_ascii=False)[:110]}")
                print("         → 多为 WORKER_TOKEN 与服务器不一致（服务端 401）")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f" [FAIL] 队列 peek 失败: {e}")
            print("         → 多为 WORKER_TOKEN 与服务器不一致（服务端 401）")

    print()
    print("结论：" + ("全部就绪 ✅" if ok else "存在未通过项，请按上面提示修正 ❌"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="研报站点 · 本机 Worker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="查看队列（不改变任务状态）").set_defaults(fn=cmd_check)
    sub.add_parser("claim", help="拉取一个任务（状态改为 processing）").set_defaults(fn=cmd_claim)

    p = sub.add_parser("fetch", help="抓公告+下载+抽取")
    p.add_argument("code")
    p.add_argument("period", help="如 2026H1")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("show", help="按页查看全文")
    p.add_argument("code")
    p.add_argument("period")
    p.add_argument("pages", help='如 "7,9-10"')
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("submit", help="回传成功结果")
    p.add_argument("task_id")
    p.add_argument("report", help="report.json 路径")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("fail", help="回传失败（不扣次数）")
    p.add_argument("task_id")
    p.add_argument("reason")
    p.set_defaults(fn=cmd_fail)

    # ---------- 通知队列（供飞书推送） ----------
    p = sub.add_parser("notice", help="通知队列：peek / pull / done / fail")
    nsub = p.add_subparsers(dest="action", required=True)
    nsub.add_parser("peek", help="查看待发通知（只读）").set_defaults(fn=cmd_notice_peek)
    nsub.add_parser("pull", help="取出一条待发通知（JSON 输出）").set_defaults(fn=cmd_notice_pull)
    n = nsub.add_parser("done", help="标记已发送")
    n.add_argument("notif_id")
    n.set_defaults(fn=cmd_notice_done)
    n = nsub.add_parser("fail", help="标记发送失败（自动重试，上限 3 次）")
    n.add_argument("notif_id")
    n.add_argument("reason")
    n.set_defaults(fn=cmd_notice_fail)

    # ---------- 权限批准（代管理员执行，解决链接打不开） ----------
    p = sub.add_parser("perm", help="权限申请：list / approve")
    psub = p.add_subparsers(dest="action", required=True)
    psub.add_parser("list", help="列出待批准的申请").set_defaults(fn=cmd_perm_list)
    a = psub.add_parser("approve", help="批准（按 token 或 --id）")
    a.add_argument("token", nargs="?", default=None)
    a.add_argument("--id", type=int, default=None, dest="id")
    a.set_defaults(fn=cmd_perm_approve)

    sub.add_parser("doctor", help="自检配置（路径/令牌/连通性），不改变任何状态").set_defaults(fn=cmd_doctor)

    args = ap.parse_args()

    # 除了 doctor 自身，其余命令都先过一遍自检 ——
    # 宁可在这里明确拦住，也不要让它跑到一半才炸出费解的报错。
    if args.fn is not cmd_doctor:
        need_skill = args.fn in (cmd_fetch, cmd_submit)
        problems = preflight(CFG, need_skill=need_skill)
        if problems:
            print("!! 配置自检未通过，已中止：", file=sys.stderr)
            for i, p in enumerate(problems, 1):
                print(f"   {i}) {p}", file=sys.stderr)
            print("\n   修好 worker.env 后重试；或先跑：python worker.py doctor", file=sys.stderr)
            return 2

    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
