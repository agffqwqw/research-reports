# -*- coding: utf-8 -*-
"""研报的修改与删除（需权限）

权限（见 app/deps.py 的 PERMISSIONS）：
    can_edit_report   → PUT    /api/reports/{code}/{period}
    can_delete_report → DELETE /api/reports/{code}/{period}

设计要点：
1. **只允许改「人写的那部分」**：评级、结论、依据。
   来源类字段（source_url / disclosure_date / meta.code）**不可改** —— 它们是
   一手凭据，改了就等于伪造出处。这样「AI 生成 + 人工修订」的边界是清晰的。
2. **删除是软删除**（is_deleted=1）：数据留档，公开查询立即不可见。
   详情接口会先查 SQLite 的可见性再读 Redis，所以软删后即时 404，无需清 Redis。
3. 修改后**同时更新 SQLite（事实源）与 Redis（加速层）**，
   且 Redis 失败不影响修改结果（下次读会从 SQLite 回退）。
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import db, query_one
from app.deps import require_permission
from app.redis_client import get_redis, save_report

router = APIRouter(prefix="/api/reports", tags=["reports-admin"])

now = lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# 六档评级：中文 ↔ 编码。与技能包契约保持一致，勿另立标准。
RATING_CODES = {
    "强": "strong",
    "较强": "good",
    "中等偏强": "mid_plus",
    "中性": "mid",
    "结构性分化": "mixed",
    "偏负面": "weak",
}
ALLOWED_RATINGS = tuple(RATING_CODES.keys())


class EvidenceItem(BaseModel):
    label: str = Field(default="", max_length=40)
    text: str = Field(default="", max_length=2000)


class DimPatch(BaseModel):
    order: int = Field(ge=1, le=6)
    rating: str | None = None
    conclusion: str | None = Field(default=None, max_length=1000)
    evidence: list[EvidenceItem] | None = None


class ReportPatch(BaseModel):
    dims: list[DimPatch]


def _load_row(conn, code: str, period: str):
    return query_one(
        conn,
        """SELECT r.id, r.content_json, r.is_public, r.is_deleted, r.quality, r.redis_key
           FROM report r
           WHERE r.company_code = ? AND r.period = ?""",
        (code, period),
    )


@router.put("/{code}/{period}", summary="修改研报内容（需「修改研报」权限）")
def update_report(code: str, period: str, data: ReportPatch,
                  user: dict = Depends(require_permission("edit"))):
    if not data.dims:
        raise HTTPException(400, "没有需要修改的内容")

    with db() as conn:
        row = _load_row(conn, code, period)
        if not row:
            raise HTTPException(404, "研报不存在")
        if row["is_deleted"]:
            raise HTTPException(410, "该研报已被删除，无法修改")

        try:
            obj = json.loads(row["content_json"] or "{}")
        except json.JSONDecodeError:
            raise HTTPException(500, "研报内容损坏，请联系管理员")

        dims = obj.get("dims") or []
        by_order = {int(d.get("order") or 0): d for d in dims}

        changed: list[str] = []
        for patch in data.dims:
            target = by_order.get(patch.order)
            if not target:
                raise HTTPException(400, f"不存在第 {patch.order} 个维度")

            if patch.rating is not None:
                if patch.rating not in ALLOWED_RATINGS:
                    raise HTTPException(
                        400,
                        "评级只能是：%s" % "、".join(ALLOWED_RATINGS),
                    )
                if target.get("rating") != patch.rating:
                    target["rating"] = patch.rating
                    # 中文改了，编码必须同步，否则列表页色块与详情页会对不上
                    target["rating_code"] = RATING_CODES[patch.rating]
                    changed.append(f"dim{patch.order}.rating")

            if patch.conclusion is not None and target.get("conclusion") != patch.conclusion:
                target["conclusion"] = patch.conclusion.strip()
                changed.append(f"dim{patch.order}.conclusion")

            if patch.evidence is not None:
                new_ev = [
                    {"label": e.label.strip(), "text": e.text.strip()}
                    for e in patch.evidence
                    if (e.label.strip() or e.text.strip())
                ]
                if target.get("evidence") != new_ev:
                    target["evidence"] = new_ev
                    changed.append(f"dim{patch.order}.evidence")

        if not changed:
            return {"ok": True, "changed": [], "message": "内容没有变化，未做修改"}

        obj["dims"] = dims
        # 记录人工修订痕迹（不改 meta 里的来源字段）
        obj.setdefault("meta", {})["last_edited_at"] = now()
        obj["meta"]["last_edited_by"] = user.get("email", "")

        conn.execute(
            "UPDATE report SET content_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(obj, ensure_ascii=False), now(), row["id"]),
        )

    # Redis 加速层同步（失败不影响结果）
    redis_ok = False
    try:
        save_report(get_redis(), code, period, obj)
        redis_ok = True
    except Exception:  # noqa: BLE001
        redis_ok = False

    return {
        "ok": True,
        "changed": changed,
        "redis": redis_ok,
        "message": "已保存修改（%d 处）" % len(changed),
    }


@router.delete("/{code}/{period}", summary="删除研报（需「删除研报」权限，软删除）")
def delete_report(code: str, period: str,
                  user: dict = Depends(require_permission("delete"))):
    with db() as conn:
        row = _load_row(conn, code, period)
        if not row:
            raise HTTPException(404, "研报不存在")
        if row["is_deleted"]:
            return {"ok": True, "already": True, "message": "该研报已是删除状态"}

        conn.execute(
            "UPDATE report SET is_deleted = 1, updated_at = ? WHERE id = ?",
            (now(), row["id"]),
        )
        # 留一条痕迹：谁删的、什么时候
        try:
            obj = json.loads(row["content_json"] or "{}")
            obj.setdefault("meta", {})["deleted_at"] = now()
            obj["meta"]["deleted_by"] = user.get("email", "")
            conn.execute(
                "UPDATE report SET content_json = ? WHERE id = ?",
                (json.dumps(obj, ensure_ascii=False), row["id"]),
            )
        except json.JSONDecodeError:
            pass

    return {
        "ok": True,
        "soft_delete": True,
        "message": "研报已删除（软删除，数据留档；公开页已不可见）",
    }
