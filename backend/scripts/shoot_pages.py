# -*- coding: utf-8 -*-
"""用 Playwright + 系统 Edge 截取各页面（仅本机预览用）"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright  # noqa: E402

# 截图输出目录：以**本脚本位置**为基准推导（脚本在 backend/scripts/ 下）。
# ⚠️ 不要写死绝对路径 —— 那会让别人 clone 后直接跑不起来，也把本机目录结构暴露出去。
OUT = str(Path(__file__).resolve().parents[2] / "docs" / "screenshots")
BASE = "http://127.0.0.1:5173"
os.makedirs(OUT, exist_ok=True)

# 可选：独立 Chromium（可移植、不依赖系统 Edge）。
# 优先取环境变量 CHROMIUM_EXE；否则探测常见位置；都没有就回退系统 Edge。
CHROMIUM_EXE = os.environ.get("CHROMIUM_EXE", "").strip()
if not CHROMIUM_EXE:
    _guess = Path.home() / ".workbuddy" / "binaries" / "chromium" / "chrome-win" / "chrome.exe"
    if _guess.exists():
        CHROMIUM_EXE = str(_guess)


def launch(p):
    if os.path.exists(CHROMIUM_EXE):
        print("浏览器: 独立 Chromium (npmmirror 源)")
        return p.chromium.launch(executable_path=CHROMIUM_EXE, headless=True,
                                 args=["--no-sandbox", "--disable-gpu"])
    print("浏览器: 系统 Edge (channel=msedge)")
    return p.chromium.launch(channel="msedge", headless=True)


def main() -> int:
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  device_scale_factor=2)  # 2x 便于看清细节
        page = ctx.new_page()

        def shot(url, name, full=True, wait=1500):
            page.goto(BASE + url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(wait)  # 给 Vue 渲染留时间
            png = os.path.join(OUT, name)
            page.screenshot(path=png, full_page=full)
            print("  %-26s -> %6d 字节" % (name, os.path.getsize(png)))

        print("截图中：")
        shot("/", "01-list.png")
        shot("/report/300502/2026H1", "02-detail.png")
        shot("/report/002594/2026H1", "03-detail-byd.png")

        # 登录页
        page.goto(BASE + "/auth", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1800)
        p1 = os.path.join(OUT, "04-login.png")
        page.screenshot(path=p1, full_page=True)
        print("  %-26s -> %6d 字节" % ("04-login.png", os.path.getsize(p1)))

        # 注册页（点击 Tab 切换）
        try:
            page.locator(".tabs button").nth(1).click(timeout=5000)
            page.wait_for_timeout(1800)
            # 确认确实切到了注册（注册表单里有"注册"提交按钮且提示文案含"无需邮箱验证"）
            body = page.content()
            switched = "无需邮箱验证" in body
            p2 = os.path.join(OUT, "05-register.png")
            page.screenshot(path=p2, full_page=True)
            print("  %-26s -> %6d 字节  (切换成功=%s)"
                  % ("05-register.png", os.path.getsize(p2), switched))
        except Exception as e:  # noqa: BLE001
            print("  注册 Tab 切换失败:", str(e)[:160])

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
