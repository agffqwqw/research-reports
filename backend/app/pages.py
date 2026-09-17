# -*- coding: utf-8 -*-
"""公共 HTML 结果页

邮件里的链接点开后需要在浏览器里看到一个像样的页面（而不是 JSON）。
此模块集中提供，供 permissions.py / me.py 等复用。
"""


def result_page(title: str, message: str, ok: bool = True) -> str:
    """生成一个居中的结果页。message 支持少量 HTML（如 <b>）。"""
    color = "#3b6d11" if ok else "#a32d2d"
    bg = "#eaf3de" if ok else "#fcebeb"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
             display:flex;align-items:center;justify-content:center;min-height:100vh;
             margin:0;background:#f7f8fa;color:#1f2329">
  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;
              padding:32px 40px;max-width:440px;text-align:center;
              box-shadow:0 2px 12px rgba(24,95,165,.08)">
    <div style="display:inline-block;width:48px;height:48px;line-height:48px;
                border-radius:50%;background:{bg};color:{color};
                font-size:24px;margin-bottom:14px">{'✓' if ok else '!'}</div>
    <h2 style="color:{color};margin:0 0 12px;font-size:19px">{title}</h2>
    <p style="color:#4b5563;line-height:1.7;margin:0;font-size:14px">{message}</p>
  </div>
</body></html>"""
