"""RD 分析 BOM Agent API（:8500）。

    .venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8500

端點
----
    GET  /api/bom/{item_number}          多層展開（?level= &rows= &like= &slots=）
    GET  /api/bom/{item_number}/semi     半成品
    GET  /api/bom/{item_number}/line     一階明細 + 替代料
    GET  /api/health                     來源庫 / 下游 / LLM gateway 狀態

    GET  /a2a/card / tools / health / peers
    POST /a2a/tool/{name}                find_semi_finished / get_bom / get_bom_line
    POST /a2a/search                     料號 -> BOM 卡片
    POST /a2a/message                    委派型對話（SSE）
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from src import bom, db, guard, slots

app = FastAPI(title="BOM Agent — RD 分析 BOM Agent", version="0.2")


def _out(result: dict, with_slots: bool) -> dict:
    """查不到料號回 404（附上試過的策略）；查得到就選擇性補槽位，槽位失敗只降級。"""
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail={
            "error": result.get("error"),
            "input": result.get("input"),
            "tried": result.get("tried"),
        })
    if with_slots and result.get("lines"):
        result.setdefault("degraded", []).extend(slots.annotate(result["lines"]))
    return result


@app.get("/api/bom/{item_number}")
def get_bom(
    item_number: str,
    level: int = Query(guard.MAX_LEVEL, ge=1, le=guard.MAX_LEVEL, description="展開幾層"),
    rows: int = Query(guard.MAX_ROWS, ge=1, le=guard.MAX_ROWS, description="列數上限"),
    like: str | None = Query(None, description="對品名做子字串過濾，例 L=2M"),
    slots_on: bool = Query(True, alias="slots", description="是否呼叫材料建議 Agent 標槽位"),
) -> dict:
    """多層向下展開。qty_eff 已逐層累乘 qty x (1+損耗/100)。"""
    return _out(bom.explode(item_number, max_level=level, max_rows=rows, name_like=like),
                slots_on)


@app.get("/api/bom/{item_number}/semi")
def get_semi(
    item_number: str,
    level: int = Query(guard.MAX_LEVEL, ge=1, le=guard.MAX_LEVEL),
    like: str | None = Query(None),
    slots_on: bool = Query(False, alias="slots"),
) -> dict:
    """半成品，summary 依兩種定義分三組。預設展開全部層數，只要直接子階用 ?level=1。"""
    return _out(bom.semi_finished(item_number, max_level=level, name_like=like), slots_on)


@app.get("/api/bom/{item_number}/line")
def get_line(item_number: str, slots_on: bool = Query(True, alias="slots")) -> dict:
    """一階明細 + 替代料。"""
    return _out(bom.bom_line(item_number), slots_on)


@app.get("/api/health")
def health() -> dict:
    """各依賴分別回報：來源庫掛掉 = 不能用；材料建議 Agent 掛掉 = 只少槽位欄位。"""
    mat_ok, mat_why = slots.available()
    plm_ok, plm_why = db.available()
    out = {
        "plm": {"ok": plm_ok, "detail": plm_why,
                      "note": "本 agent 的全部能力來源。掛掉 = 不能用。"},
        "material_8400": {"ok": mat_ok, "detail": mat_why,
                          "note": "只影響槽位欄位。掛掉 = 降級，主答案不受影響。"},
        "limits": {
            "max_level": guard.MAX_LEVEL,
            "max_rows": guard.MAX_ROWS,
            "max_fails": guard.MAX_FAILS,
            "statement_timeout_s": guard.STATEMENT_TIMEOUT,
        },
    }
    try:
        from agentkit import gateway

        from src import a2a
    except ImportError as e:
        out["a2a"] = {"ok": False, "detail": f"agentkit 未安裝（{e}）"}
        return out

    status, detail = a2a.SPEC.status()
    llm_ok, llm_why = gateway.available()
    return {
        "agent_id": a2a.AGENT_ID,
        "status": status,
        "detail": detail,
        **out,
        "llm": {"ok": llm_ok, "detail": llm_why},
        "blocked_by": a2a.BLOCKED_BY,
    }


# --------------------------------------------------------------------------
# A2A（需要 agentkit；未安裝時 REST API 仍可獨立運作）
# --------------------------------------------------------------------------
try:
    from src import a2a as _a2a

    _a2a.mount(app)
except Exception as _e:  # pragma: no cover
    print(f"[a2a] 未掛載（{type(_e).__name__}: {_e}）")
