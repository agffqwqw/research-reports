# -*- coding: utf-8 -*-
"""证券查询接口（B4 类型判断）

供前端「提交生成」前做前置校验：确认是 A 股、唯一、且非银行/券商。
"""
from fastapi import APIRouter, Query

from app.services.cninfo import lookup

router = APIRouter(prefix="/api", tags=["securities"])


@router.get("/securities/lookup", summary="证券查询与类型判断")
def securities_lookup(
    q: str = Query(..., min_length=1, description="股票代码或公司名称"),
):
    """返回示例：
      成功  {"ok": true, "code": "300750", "name": "宁德时代", "market": "SZ", "org_id": "..."}
      拒绝  {"ok": false, "reason": "unsupported_industry", "detail": "..."}
    """
    return lookup(q)
