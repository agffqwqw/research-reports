# -*- coding: utf-8 -*-
"""巨潮资讯网客户端（B4 证券类型判断 + 定期报告定位）

依据《研报站点-需求评审与设计.md》1.3 节：
- 过滤 category == "A股"，必须恰好 1 条
- 名称含「银行」或「证券」的直接剔除

另提供 latest_periodic_report()：定位最新一期定期报告，
供提交生成时判断「这一期是否已经生成过」。
"""
import datetime
import json
import re
import ssl
import urllib.parse
import urllib.request

HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

TOP_SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
HIS_ANN = "http://www.cninfo.com.cn/new/hisAnnouncement/query"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# 需剔除的行业关键词（用户决策：只剔银行与券商）
BLOCK_KEYWORDS = ("银行", "证券")

# 定期报告标题形如「2026年半年度报告」「2025年年度报告」
_RE_PERIODIC = re.compile(r"(\d{4})\s*年\s*(半年度|年度)\s*报告\s*$")

# ⚠️ 时区陷阱：巨潮的 announcementTime 是毫秒时间戳，同一份公告
#    「北京时间 2026-08-29 00:00」用 UTC 解析会变成「2026-08-28 16:00」，
#    日期整整少一天，导致防重复比对误判为「新报告」而重复生成。
#    披露日期一律按**北京时间**取，且显式指定，不依赖服务器时区。
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def to_cn_date(ms_timestamp: int | float) -> str:
    """毫秒时间戳 → 北京时间的 YYYY-MM-DD。"""
    return datetime.datetime.fromtimestamp(ms_timestamp / 1000, CN_TZ).strftime("%Y-%m-%d")




def _post(url: str, data: dict) -> str:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def lookup(query: str) -> dict:
    """按代码或名称查证券，返回标准化结果。

    返回 dict 的两种情况：
      {"ok": True,  "code", "name", "market", "org_id"}
      {"ok": False, "reason": "not_found" | "ambiguous" | "unsupported_industry",
       "detail": str}
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "reason": "empty", "detail": "请输入股票代码或公司名称"}

    try:
        raw = _post(TOP_SEARCH, {"keyWord": query, "maxSecNum": "10"})
        hits = json.loads(raw) if raw.strip() else []
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "upstream_error", "detail": f"巨潮查询失败: {type(exc).__name__}"}

    if not isinstance(hits, list):
        return {"ok": False, "reason": "upstream_error", "detail": "巨潮返回格式异常"}

    a_shares = [h for h in hits if h.get("category") == "A股"]

    if not a_shares:
        return {"ok": False, "reason": "not_found", "detail": "未找到该证券，请确认是 A 股上市公司代码或名称"}

    if len(a_shares) > 1:
        codes = "、".join(h.get("code", "") for h in a_shares)
        return {"ok": False, "reason": "ambiguous", "detail": f"匹配到多只 A 股（{codes}），请改用股票代码重试"}

    h = a_shares[0]
    name = h.get("zwjc", "")
    if any(kw in name for kw in BLOCK_KEYWORDS):
        return {"ok": False, "reason": "unsupported_industry",
                "detail": f"{name} 属于银行/券商，暂不支持生成研报"}

    code = h.get("code", "")
    # 交易所按代码前缀判断（topSearch 的 type 字段不区分深沪）
    if code.startswith("6"):
        market = "SH"
    elif code.startswith(("0", "3")):
        market = "SZ"
    elif code.startswith(("4", "8")):
        market = "BJ"
    else:
        market = ""

    return {
        "ok": True,
        "code": code,
        "name": name,
        "market": market,
        "org_id": h.get("orgId", ""),
    }


def _strip_tags(s: str) -> str:
    return (s or "").replace("<em>", "").replace("</em>", "").replace("<b>", "").replace("</b>", "").strip()


def _period_from_title(title: str) -> str | None:
    """由标题推报告期：「2026年半年度报告」→ 2026H1；「2025年年度报告」→ 2025FY。"""
    m = _RE_PERIODIC.search(title)
    if not m:
        return None
    year, kind = m.group(1), m.group(2)
    return f"{year}H1" if kind == "半年度" else f"{year}FY"


def latest_periodic_report(code: str, org_id: str, years_back: int = 2) -> dict:
    """查该公司**最新一期**定期报告（年报 / 半年报）。

    用于防重复生成：把它的披露日期与库里已有研报的披露日期比对，
    相同即表示「这一期已经生成过」。

    返回：
        {"ok": True, "title", "period", "disclosure_date", "url"}
        {"ok": False, "reason": "not_found" | "upstream_error", "detail": str}
    """
    if not code or not org_id:
        return {"ok": False, "reason": "bad_args", "detail": "缺少 code / org_id"}

    this_year = datetime.date.today().year
    start = f"{this_year - max(1, years_back)}-01-01"
    end = f"{this_year}-12-31"

    try:
        raw = _post(HIS_ANN, {
            "stock": "%s,%s" % (code, org_id),
            "tabName": "fulltext",
            "pageSize": "100",
            "pageNum": "1",
            "column": "szse",
            "category": "",
            "plate": "",
            "seDate": f"{start}~{end}",
            "searchkey": "",
            "secid": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        })
        obj = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "upstream_error",
                "detail": f"巨潮公告查询失败: {type(exc).__name__}"}

    anns = obj.get("announcements") or []
    if not anns:
        return {"ok": False, "reason": "not_found", "detail": "近两年内未找到定期报告"}

    # 只保留「正式定期报告正文」：标题以 年年度报告 / 年半年度报告 结尾，
    # 且排除摘要、英文版、更正/补充/提示性公告（这些都不是正文）
    picked = None
    for a in anns:
        title = _strip_tags(a.get("announcementTitle", ""))
        if not _RE_PERIODIC.search(title):
            continue
        if any(k in title for k in ("摘要", "英文", "更正", "补充", "提示性", "取消", "说明")):
            continue
        ts = a.get("announcementTime") or 0
        if picked is None or ts > picked[0]:
            picked = (ts, title, a.get("adjunctUrl", ""))

    if not picked:
        return {"ok": False, "reason": "not_found",
                "detail": "未找到定期报告正文（可能尚未披露）"}

    ts, title, adj = picked
    disc = to_cn_date(ts)  # 按北京时间，见文件头时区说明

    return {
        "ok": True,
        "title": title,
        "period": _period_from_title(title) or "",
        "disclosure_date": disc,
        "url": ("http://static.cninfo.com.cn/" + adj) if adj else "",
    }

