"""BOM Agent 的 A2A 層：把查詢引擎包成工具，供 LLM 委派迴圈與其他 Agent 呼叫。

在多 Agent 架構中，本節點有兩個下游：

    RD 分析 BOM Agent（本專案 :8500）
        ├── 相似搜尋建議 Agent  :8000   （規劃中）
        └── 材料建議 Agent      :8400   （槽位判定）

可用：查現行 BOM（多層展開、一階明細 + 替代料、半成品判定）。
規劃中：propose_bom / get_slot_template（產生新規格的 BOM 建議），目前標記 ready=False。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

# agentkit 為獨立維護的 A2A 框架（不在本 repo），以 AGENTKIT_PATH 指定位置
_HUB = os.environ.get("AGENTKIT_PATH")
if _HUB and os.path.isdir(_HUB) and _HUB not in sys.path:
    sys.path.insert(0, _HUB)

from agentkit import AgentSpec, Block, Hit, ToolSpec, build_router  # noqa: E402
from agentkit import client as peers  # noqa: E402

from . import bom, db, guard, slots  # noqa: E402

AGENT_ID = "bom"

BLOCKED_BY = [
    "槽位樣板與用量公式尚未建立 → propose_bom / get_slot_template 不可用",
    "相似搜尋建議 Agent（:8000）尚未接上 → 沒有「找相似母件」的能力",
]

_NEEDS_MINING = (
    "需要槽位樣板（每個家族要哪些槽位、出現率多少）與用量公式"
    "（用量是常數還是端數/pin 數/長度的函數）才能運作，目前尚未建立。"
    "現階段可用 get_bom / find_semi_finished / get_bom_line 查**現行**的 BOM，"
    "但無法為新規格**產生**一張建議。"
)

# 從自由文字辨識料號：長度 >= 8 且至少 4 個數字，可排除 PH2.0 / UL2725 這類規格字串
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9\-./]{7,}")
_ONE_LEVEL = re.compile(r"(?:一階|第一階|單層|直接子階|直接下階)", re.IGNORECASE)
_CARD_ROW_LIMIT = 200


def _codes(q: str) -> list[str]:
    out: list[str] = []
    for m in _CODE.finditer(q or ""):
        t = m.group(0).upper().strip(".-/")
        if len(t) >= 8 and sum(c.isdigit() for c in t) >= 4 and t not in out:
            out.append(t)
    return out[:4]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _with_slots(out: dict[str, Any], want: bool) -> dict[str, Any]:
    """需要時補上槽位欄位，失敗只降級不失敗。"""
    if not want or not out.get("ok") or not out.get("lines"):
        return out
    out.setdefault("degraded", []).extend(slots.annotate(out["lines"]))
    return out


def _t_get_bom(a: dict) -> dict:
    out = bom.explode(
        a.get("item_number") or "",
        max_level=a.get("max_level") or guard.MAX_LEVEL,
        max_rows=a.get("max_rows") or guard.MAX_ROWS,
        name_like=a.get("name_like"),
    )
    return _with_slots(out, a.get("with_slots", True))


def _t_find_semi_finished(a: dict) -> dict:
    out = bom.semi_finished(
        a.get("item_number") or "",
        max_level=a.get("max_level") or guard.MAX_LEVEL,
        name_like=a.get("name_like"),
    )
    return _with_slots(out, a.get("with_slots", False))


def _t_get_bom_line(a: dict) -> dict:
    return _with_slots(bom.bom_line(a.get("item_number") or ""),
                       a.get("with_slots", True))


TOOLS = [
    ToolSpec(
        name="find_semi_finished",
        description=(
            "★ 最常用的一支：查某個料號的 BOM 裡有哪些**半成品**。"
            "預設展開全部 12 層，回的是整條產線牽涉到的半成品；"
            "只要看直接子階就把 max_level 設 1。"
            "★ 半成品有兩種定義，回傳把它們分開，**不要只看其中一種**："
            "`is_semi_by_structure`（這個料號自己還有 BOM，結構事實）與 "
            "`is_semi_by_type`（PLM 登記的料件類型為半成品）。"
            "登記為半成品的料裡有大量標籤與金屬沖壓件，它們沒有自己的 BOM，"
            "不是 RD 口中的半成品。summary.both 是兩種定義都成立的那組，回答時以它為主。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item_number": {"type": "string", "description": "料號或舊料號"},
                "max_level": {"type": "integer",
                              "description": f"展開幾層，預設 {guard.MAX_LEVEL}；只要直接子階就給 1"},
                "name_like": {"type": "string",
                              "description": "對品名做子字串過濾，例 'L=2M'（選填）"},
                "with_slots": {"type": "boolean",
                               "description": "是否呼叫材料建議 Agent 標槽位，預設 false"},
            },
            "required": ["item_number"],
        },
        handler=_t_find_semi_finished,
    ),
    ToolSpec(
        name="get_bom",
        description=(
            "查既有料號的**現行**多層 BOM（向下展開）。回傳每列帶 level / 用量 / "
            "損耗 / 累乘後的實際用量 qty_eff / 路徑 path / 品名 / bom_note。"
            "`qty_eff` 已把每一層的 qty x (1+損耗/100) 累乘過，"
            "葉節點那一列就是「做一個母件要幾個」。"
            "★ 注意：損耗率是**百分比**不是小數；自環已濾除；虛擬件會穿透展開但照樣列出並標 is_phantom；"
            "同一個子件從不同路徑到達會出現多列（那是 BOM 的實際結構，不是重複）。"
            "★ 不含單價。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item_number": {"type": "string", "description": "料號或舊料號"},
                "max_level": {"type": "integer", "description": f"預設 {guard.MAX_LEVEL}"},
                "max_rows": {"type": "integer", "description": f"預設 {guard.MAX_ROWS}，超過會標 truncated"},
                "name_like": {"type": "string", "description": "對品名做子字串過濾（選填）"},
                "with_slots": {"type": "boolean", "description": "是否標槽位，預設 true"},
            },
            "required": ["item_number"],
        },
        handler=_t_get_bom,
    ),
    ToolSpec(
        name="get_bom_line",
        description=(
            "查某個料號的**一階** BOM 明細 + 每一項的替代料。"
            "這是 RD 對圖用的視角：第幾項（sort_order）、用量、bom_note、"
            "是不是替代線（is_alternate）、參考位號。"
            "要看整張展開請改用 get_bom。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item_number": {"type": "string", "description": "料號或舊料號"},
                "with_slots": {"type": "boolean", "description": "是否標槽位，預設 true"},
            },
            "required": ["item_number"],
        },
        handler=_t_get_bom_line,
    ),
    ToolSpec(
        name="propose_bom",
        description=(
            "★ 為新規格**產生**一張 BOM 建議（不是查現有的）。"
            "輸出每列帶 槽位 / 料號 / 用量 / 損耗 / 依據(provenance) / 信心度。"
            "**目前尚未實作** —— 要查現行 BOM 請用 get_bom / find_semi_finished。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "規格文字 / RFQ 編號 / 既有料號"},
                "mode": {"type": "string", "enum": ["spec", "rfq", "revise"]},
                "family": {"type": "string", "description": "母件家族，例 CBL-A"},
                "top_n": {"type": "integer", "description": "參考幾張相似 BOM，預設 5"},
            },
            "required": ["input"],
        },
        ready=False, not_ready_reason=_NEEDS_MINING,
    ),
    ToolSpec(
        name="get_slot_template",
        description=(
            "查某個家族的槽位樣板與用量公式（RD 稽核用）。**目前尚未實作。**"
            "立論依據：取「品名只差長度」的母件配對比較 BOM，"
            "槽位集合相似度中位數 1.00、料號集合只有 0.64 —— "
            "槽位結構幾乎不變，但約 36% 的料號要換。"
        ),
        input_schema={
            "type": "object",
            "properties": {"family": {"type": "string", "description": "例 CBL-A"}},
            "required": ["family"],
        },
        ready=False, not_ready_reason=_NEEDS_MINING,
    ),
]


# ---------------------------------------------------------------------------
# 檢索：把料號的 BOM 當成檢索結果，讓前端畫出卡片
# ---------------------------------------------------------------------------
def _search(query: str, top_k: int, filters: dict) -> dict[str, Any]:
    """從問句辨識料號；預設查多階 BOM，明確要求一階時才查單層。

    只做料號精確查詢，不做規格檢索（那是相似搜尋建議 Agent 的職責）。
    """
    codes = _codes(query)
    if not codes:
        return {"query": query, "results": [],
                "note": "問句裡沒有看起來像料號的字串。這個 agent 目前只能用料號查現行 BOM，"
                        "用規格找母件要等相似搜尋建議 Agent 接上。"}

    try:
        requested_level = int(filters.get("max_level", guard.MAX_LEVEL))
    except (TypeError, ValueError):
        requested_level = guard.MAX_LEVEL
    requested_level = max(1, min(requested_level, guard.MAX_LEVEL))
    one_level = bool(_ONE_LEVEL.search(query or "")) or requested_level == 1
    mode = "line" if one_level else "explode"

    results = []
    for pn in codes[:top_k or 3]:
        r = (bom.bom_line(pn) if one_level else
             bom.explode(pn, max_level=requested_level, max_rows=guard.MAX_ROWS))
        results.append(r if r.get("ok") else
                       {"ok": False, "root": pn, "error": r.get("error"), "lines": []})
    return {"query": query, "mode": mode, "max_level": 1 if one_level else requested_level,
            "results": results}


def _hits(found: dict[str, Any]) -> list[Hit]:
    out: list[Hit] = []
    multi = found.get("mode") == "explode"
    for r in found.get("results") or []:
        if not r.get("ok"):
            out.append(Hit(id=r.get("root", "?"), title=r.get("root", "?"),
                           subtitle="查無此料號", body=r.get("error") or ""))
            continue
        lines = r["lines"]
        semi = sorted({x["child"] for x in lines if x["is_semi_by_structure"]})
        if multi:
            shown = lines[:_CARD_ROW_LIMIT]
            total = len(lines)
            table_title = ("多階 BOM 明細" if total <= _CARD_ROW_LIMIT else
                           f"多階 BOM 明細（顯示前 {_CARD_ROW_LIMIT} / {total} 列）")
            out.append(Hit(
                id=r["root"],
                title=r["root"],
                subtitle=(r.get("root_name") or "")[:120],
                body=(f"多階展開 {total} 列，最深 {r.get('max_level_reached', 0)} 層，"
                      f"其中結構半成品 {len(semi)} 種"),
                badges=[["展開列數", str(total)],
                         ["最深層", str(r.get("max_level_reached", 0))],
                         ["半成品", str(len(semi))]],
                blocks=[Block(
                    kind="table",
                    title=table_title,
                    head=["階", "母件", "料號", "累計用量", "半成品", "品名"],
                    rows=[[x["lvl"], x["parent"], x["child"], x["qty_eff"],
                           "是" if x["is_semi_by_structure"] else "",
                           (x.get("child_name") or "")[:60]] for x in shown],
                )],
                raw=r,
            ))
            continue
        out.append(Hit(
            id=r["root"],
            title=r["root"],
            subtitle=(r.get("root_name") or "")[:120],
            body=f"一階 {len(lines)} 項，其中半成品 {len(semi)} 種",
            badges=[["一階項數", str(len(lines))], ["半成品", str(len(semi))]],
            blocks=[Block(
                kind="table",
                title="一階 BOM 明細",
                head=["項次", "料號", "用量", "損耗%", "半成品", "品名"],
                rows=[[x["sort_order"], x["child"], x["qty"], x["attrition"],
                       "是" if x["is_semi_by_structure"] else "",
                       (x.get("child_name") or "")[:60]] for x in lines],
            )],
            raw=r,
        ))
    return out


def _context(query: str, found: dict[str, Any]) -> str:
    """把檢索結果整理成給模型的上下文。"""
    parts = []
    multi = found.get("mode") == "explode"
    for r in found.get("results") or []:
        if not r.get("ok"):
            parts.append(f"{r.get('root')}：查無此料號")
            continue
        semi = sorted({x["child"] for x in r["lines"] if x["is_semi_by_structure"]})
        if multi:
            parts.append(
                f"{r['root']}（{r.get('root_name') or ''}）已多階展開 {len(r['lines'])} 列，"
                f"最深 {r.get('max_level_reached', 0)} 層；自己還有 BOM 的半成品：{semi or '無'}。"
                "若要逐列回答，請用 get_bom 取得完整明細，不要改用只查一階的 get_bom_line。"
            )
        else:
            parts.append(
                f"{r['root']}（{r.get('root_name') or ''}）一階 {len(r['lines'])} 項"
                f"，其中自己還有 BOM 的半成品：{semi or '無'}"
            )
    if note := found.get("note"):
        parts.append(note)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
def _status() -> tuple[str, dict[str, Any]]:
    """所有能力都來自 PLM 來源庫，因此「來源庫通不通」即「能不能用」；下游狀態一併回報。"""
    plm_ok, plm_why = db.available()
    detail: dict[str, Any] = {"plm": {"ok": plm_ok, "detail": plm_why}}
    for aid in ("material", "partrag"):
        c = peers.card(aid)
        detail[aid] = {
            "status": c.get("status"),
            "tools_ready": sum(1 for t in c.get("tools") or [] if t.get("ready", True)),
        }
    if not plm_ok:
        return "not_ready", {**detail, "note": "連不上 PLM 來源庫，所有 BOM 查詢都不能用"}
    return "degraded", {**detail, "note": "查現行 BOM 可用；產 BOM 建議（propose_bom）尚未實作"}


SYSTEM = """你是 OEM/ODM 估價流程裡的「RD 分析 BOM Agent」。

你的分工邊界很明確：
  **你決定「這張 BOM 要哪些槽位、各多少用量」**（結構與用量）。
  **「每個槽位放哪一個具體料號」是材料建議 Agent 的事**，要呼叫它，不要自己猜。

★ 你現在能做什麼
  **查現行 BOM**：多層展開（get_bom）、一階明細 + 替代料（get_bom_line）、
  半成品（find_semi_finished）。這些直接查 PLM，是事實不是推測。

★ 你現在還不能做什麼
  **產生**一張新規格的 BOM 建議（propose_bom）—— 槽位樣板與用量公式還沒建立。
  被要求「幫我產一張 XXX 的 BOM」時要直說這件事，
  不要用「查一張相似的現行 BOM」冒充建議。兩者約差 36% 的料號（見下）。

★ 「半成品」這個詞有兩種定義，回答時務必分清楚
  - `is_semi_by_structure`：這個料號**自己還有 BOM**（結構事實）
  - `is_semi_by_type`：PLM 登記的料件類型為半成品
  登記為半成品的料裡有大量**標籤**與**金屬沖壓件**，它們沒有自己的 BOM，
  不是 RD 口中的半成品。使用者問「半成品有哪些」時，以 summary.both（兩種都成立）
  為主答案，另外兩組要提到但要說明差別。

你必須遵守的事實與限制：

* **槽位結構穩定、選料不穩定。** 取「品名只差長度」的母件配對比較 BOM：
  槽位集合相似度中位數 1.00，料號集合只有 0.64。
  也就是**改長度時槽位幾乎不用動，但約 36% 的料號要換**。
  講「照抄相似料的 BOM」時要說清楚：槽位可以抄，料號必須逐槽確認。

* 損耗率是**百分比不是小數**。工具回的 `qty_eff` 已經算好
  qty x (1+損耗/100) 並逐層累乘，直接用它，不要自己再乘一次。

* BOM 最深 12 層。同一個子件從不同路徑到達會出現**多列**，那是 BOM 的實際
  結構不是資料重複；要講「幾種料」時請對 child 去重再數。

* 虛擬件（is_phantom）MRP 不入庫，展開時已經穿透，但那一列仍然列出來。

* **不出單價。** PLM 子件標準成本大多未維護，也沒有採購價格表。
  被問價格要指向成本預估 Agent。

* 回傳裡的 `truncated` 為 true 代表結果被列數上限截斷，**那不是完整的 BOM**，
  必須告訴使用者；`degraded` 陣列裡的每一句話也都要如實轉達，
  尤其「槽位欄位從缺」—— 那代表 role 是 null 不是「沒有槽位」。

產出是**給 RD 審的**。每一列都要能說出依據；說不出依據的要標成「需 RD 確認」，
不要為了讓表格看起來完整而填一個猜的料號。

回答用繁體中文，直接講結論。引用下游 agent 的結果時說明是哪一個 agent 提供的。"""


EXAMPLES = [
    {"label": "★ 查半成品", "prompt": "CBL-A10001-1 的半成品有哪些？"},
    {"label": "查一階明細", "prompt": "CBL-A20002-1 的 BOM 第一階有哪幾項？用量各多少？"},
    {"label": "多層展開", "prompt": "CBL-A10001-1 整張 BOM 展開有幾層？最底下是什麼料？"},
    {"label": "判子件槽位（走下游）", "prompt": "STM-E70007 和 LBL-F80008 各屬於哪個槽位？"},
]


SPEC = AgentSpec(
    agent_id=AGENT_ID,
    tools=TOOLS,
    system=SYSTEM,
    examples=EXAMPLES,
    search_fn=_search,
    hits_fn=_hits,
    context_fn=_context,
    answer_stream=None,   # 文字由委派迴圈產生
    status_fn=_status,
    blocked_by=BLOCKED_BY,
    can_delegate=True,    # 可再委派給兩個下游 Agent
)


def mount(app: Any) -> bool:
    try:
        app.include_router(build_router(SPEC))
        return True
    except Exception as e:
        print(f"[a2a] 掛載失敗：{type(e).__name__}: {e}")
        return False
