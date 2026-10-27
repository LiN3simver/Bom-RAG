"""BOM 查詢引擎：料號解析 / 單層明細 / 多層展開 / 半成品。

    python -m src.bom --selftest                # 回歸測試
    python -m src.bom CBL-A10001-1              # 多層展開
    python -m src.bom CBL-A10001-1 --semi       # 只看半成品
    python -m src.bom CBL-A20002-1 --level 1    # 只看第一階

查詢檔（*.sql）不隨 repo 公開，放在 BOM_SQL_DIR 指定的目錄；查詢設計見 README。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import guard
from .db import cursor, mssql
from .guard import Budget, BudgetExhausted

_SQL_DIR = Path(os.environ.get("BOM_SQL_DIR") or Path(__file__).resolve().parent.parent / "sql")
_cache: dict[str, str] = {}

# 用 guard.MAXRECURSION 覆寫查詢檔中的字面值，讓 guard.py 成為唯一設定來源
_MAXREC_RE = re.compile(r"MAXRECURSION\s+\d+")
_BLOCK_RE = re.compile(r"^--\s*@(\w+)\s*$", re.MULTILINE)

COST_NOTE = "本 Agent 不提供單價：PLM 子件標準成本大多未維護，且無採購價格表。"


def _sql(name: str) -> str:
    if name not in _cache:
        path = _SQL_DIR / f"{name}.sql"
        if not path.is_file():
            raise FileNotFoundError(f"找不到查詢檔 {path}（請以 BOM_SQL_DIR 指定）")
        text = path.read_text(encoding="utf-8")
        _cache[name] = _MAXREC_RE.sub(f"MAXRECURSION {guard.MAXRECURSION}", text)
    return _cache[name]


def _block(name: str, block: str) -> str:
    """取 {name}.sql 裡 `-- @block` 之後、下一個 `-- @` 之前的那段。"""
    text = _sql(name)
    marks = [(m.group(1), m.end()) for m in _BLOCK_RE.finditer(text)]
    for i, (key, start) in enumerate(marks):
        if key == block:
            end = _BLOCK_RE.search(text, start).start() if i + 1 < len(marks) else len(text)
            return text[start:end]
    raise KeyError(f"{name}.sql 裡沒有 -- @{block}")


# ---------------------------------------------------------------------------
# 型別
# ---------------------------------------------------------------------------
def _val(v: Any) -> Any:
    """pyodbc 的值 -> 可 JSON 序列化的值（Decimal 先取 6 位，避免浮點雜訊）。"""
    if isinstance(v, Decimal):
        return float(round(v, 6))
    if isinstance(v, str):
        return v.strip() or None
    return v


def _rows(cur, sql: str, args: list) -> list[dict[str, Any]]:
    cur.execute(sql, *args)
    cols = [d[0] for d in cur.description]
    return [{c: _val(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def _as_bool(v: Any) -> bool:
    """旗標欄位轉 bool。

    ★ PLM 的旗標欄位可能是字串 '0'/'1'，而 Python 的 bool('0') 是 True。
      查詢中計算出的旗標則是 int，兩種型別會混在同一份結果裡，這裡都吃。
    """
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() not in ("", "0")
    return bool(v)


def _flags(row: dict[str, Any]) -> dict[str, Any]:
    for k in ("is_semi_by_type", "is_semi_by_structure", "is_phantom", "is_alternate"):
        if k in row:
            row[k] = _as_bool(row[k])
    return row


def _is_depth_first(lines: list[dict[str, Any]]) -> bool:
    """驗證輸出順序為深度優先：離開某祖先的子樹後，不可再回到該 path prefix。"""
    previous: tuple[str, ...] = ()
    closed: set[tuple[str, ...]] = set()
    for line in lines:
        path = tuple(part for part in (line.get("path") or "").split("|") if part)
        if len(path) < 2:
            return False

        common = 0
        for old, new in zip(previous, path):
            if old != new:
                break
            common += 1
        for size in range(common + 1, len(previous) + 1):
            closed.add(previous[:size])
        if any(path[:size] in closed for size in range(2, len(path) + 1)):
            return False
        previous = path
    return True


# ---------------------------------------------------------------------------
# 料號解析
# ---------------------------------------------------------------------------
def resolve(pn: str, *, budget: Budget | None = None) -> dict[str, Any]:
    """把使用者輸入對應到一個現行 PART。三種策略各吃一次失敗預算，不做模糊比對。"""
    pn = (pn or "").strip()
    if not pn:
        return {"found": False, "input": pn, "tried": [], "error": "沒有給料號"}

    b = budget or Budget()
    strategies = (
        ("料號精確比對", "exact"),
        ("料號正規化（去空白/轉大寫）", "normalized"),
        ("舊制料號", "old"),
    )
    try:
        with mssql() as conn:
            cur = cursor(conn)
            for label, block in strategies:
                hit = b.attempt(label, lambda blk=block: (
                    _rows(cur, _block("resolve", blk), [pn]) or [None]
                )[0])
                if hit:
                    return {
                        "found": True,
                        "input": pn,
                        "item_number": hit["item_number"],
                        "name": hit.get("name"),
                        "part_type": hit.get("part_type"),
                        "matched_by": block,
                        "tried": b.tried,
                    }
    except BudgetExhausted as e:
        return {"found": False, "input": pn, "tried": e.tried,
                "error": f"查無此料號。{e}"}
    return {"found": False, "input": pn, "tried": b.tried, "error": "查無此料號"}


# ---------------------------------------------------------------------------
# 單層明細
# ---------------------------------------------------------------------------
def bom_line(item_number: str, *, budget: Budget | None = None) -> dict[str, Any]:
    """一階 BOM 明細 + 替代料。"""
    b = budget or Budget()
    r = resolve(item_number, budget=b)
    if not r["found"]:
        return {"ok": False, "error": r.get("error"), "tried": r["tried"], "input": item_number}
    root = r["item_number"]

    with mssql() as conn:
        cur = cursor(conn)
        lines = [_flags(x) for x in _rows(cur, _block("bom_line", "lines"), [root])]
        subs = _rows(cur, _block("bom_line", "subs"), [root])

    by_line: dict[str, list[dict]] = {}
    for s in subs:
        by_line.setdefault(s["bom_line_id"], []).append(s)
    for ln in lines:
        ln["substitutes"] = by_line.get(ln["bom_line_id"], [])

    semi = sorted({ln["child"] for ln in lines if ln["is_semi_by_structure"]})
    alt = sum(1 for ln in lines if ln.get("substitutes"))
    if lines:
        _ans = (f"{root}（{r.get('name') or '無品名'}）一階 {len(lines)} 項，"
                f"其中半成品 {len(semi)} 種" + (f"，{alt} 項有替代料。" if alt else "。"))
    else:
        _ans = (f"{root}（{r.get('name') or '無品名'}）沒有現行 BOM，"
                f"它是最底層的採購件（或尚未建 BOM）。")
    return {
        "ok": True,
        "root": root,
        "root_name": r.get("name"),
        "part_type": r.get("part_type"),
        "matched_by": r["matched_by"],
        "lines": lines,
        "cost_note": COST_NOTE,
        "answer": _ans,
    }


# ---------------------------------------------------------------------------
# `answer` 欄位：給人看的一句結論
# ---------------------------------------------------------------------------
# 小模型偶爾會把整包工具結果原樣貼出當回答。A2A 框架會優先挑出 `answer`
# 這個非空字串當答案本文，因此即使模型偷懶，使用者看到的仍是一句完整結論。
# 欄位名稱需與框架約定一致，不要更改。內容只陳述查到的事實，不做推測。
def _is_semi_type(part_type: str | None) -> bool:
    return (part_type or "").upper() == "SEMI"


def _answer_explode(root: str, r: dict[str, Any], lines: list[dict[str, Any]],
                    scanned: int, depth: int, truncated: int, max_rows: int) -> str:
    who = f"{root}（{r.get('name') or '無品名'}）"
    if lines:
        kinds = len({ln["child"] for ln in lines})
        ans = (f"{who}多階展開 {len(lines)} 列，最深 {depth} 層，相異子件 {kinds} 種。")
    elif scanned == 0:
        ans = f"{who}沒有現行 BOM，它是最底層的採購件（或尚未建 BOM）。"
    else:
        ans = f"{who}多階展開共 {scanned} 列，但沒有一列符合你給的過濾條件。"
    if truncated:
        ans += f" ★ 結果在 {max_rows} 列就被截斷，這**不是**完整的 BOM。"
    return ans


# ---------------------------------------------------------------------------
# 多層展開
# ---------------------------------------------------------------------------
def explode(
    item_number: str,
    *,
    max_level: int = guard.MAX_LEVEL,
    max_rows: int = guard.MAX_ROWS,
    semi_only: bool = False,
    name_like: str | None = None,
    budget: Budget | None = None,
) -> dict[str, Any]:
    """多層向下展開。

    `qty_eff` 已逐層累乘 `qty x (1 + attrition/100)`，即「做一個 root 要幾個」。
    `semi_only` 保留任一半成品定義成立的列；`name_like` 對品名做子字串過濾。
    """
    b = budget or Budget()
    r = resolve(item_number, budget=b)
    if not r["found"]:
        return {"ok": False, "error": r.get("error"), "tried": r["tried"], "input": item_number}
    root = r["item_number"]

    max_level = max(1, min(int(max_level), guard.MAX_LEVEL))
    max_rows = max(1, min(int(max_rows), guard.MAX_ROWS))

    def _run() -> list[dict[str, Any]] | None:
        with mssql() as conn:
            cur = cursor(conn)
            return _rows(cur, _sql("explode"), [root, max_level, max_rows])

    try:
        # 空 BOM 是合法答案（採購件），包一層讓空清單不被當成失敗
        raw = b.attempt("多層展開", lambda: {"rows": _run()})
    except BudgetExhausted as e:
        return {"ok": False, "error": str(e), "tried": e.tried, "input": item_number}
    if raw is None:
        return {"ok": False, "error": "多層展開失敗", "tried": b.tried, "input": item_number}

    lines = [_flags(x) for x in raw["rows"]]

    # ★ 截斷判斷必須在過濾之前，否則被截斷的結果可能被誤判為完整
    truncated = len(lines) >= max_rows
    depth = max((ln["lvl"] for ln in lines), default=0)
    # 過濾前列數：用來區分「沒有 BOM」與「有 BOM 但沒有符合條件的列」
    scanned = len(lines)

    if semi_only:
        lines = [ln for ln in lines
                 if ln["is_semi_by_type"] or ln["is_semi_by_structure"]]
    if name_like:
        needle = name_like.lower()
        lines = [ln for ln in lines if needle in (ln.get("child_name") or "").lower()]

    out: dict[str, Any] = {
        "ok": True,
        "root": root,
        "root_name": r.get("name"),
        "part_type": r.get("part_type"),
        "matched_by": r["matched_by"],
        "lines": lines,
        "line_count": len(lines),
        "rows_scanned": scanned,
        "max_level_reached": depth,
        "max_level_requested": max_level,
        "truncated": truncated,
        "degraded": [],
        "cost_note": COST_NOTE,
        "answer": _answer_explode(root, r, lines, scanned, depth, truncated, max_rows),
    }
    if truncated:
        out["degraded"].append(
            f"結果在 {max_rows} 列被截斷（上限見 src/guard.py 的 MAX_ROWS），下面不是完整的 BOM。"
        )
    if depth >= max_level:
        # 碰到使用者調小的上限才示警；碰到預設上限（即資料最深層數）視為完整，避免假警報
        if max_level < guard.MAX_LEVEL:
            out["degraded"].append(
                f"展開到第 {depth} 層就停了（你指定的上限是 {max_level}）。"
                f"這張 BOM 可能更深，下層沒有列出來 —— 拿掉 max_level 可展到 {guard.MAX_LEVEL} 層。"
            )
        else:
            out["notes"] = [
                f"這張 BOM 展到第 {depth} 層，即資料中的最深層數（{guard.MAX_LEVEL}），"
                f"應為完整展開。"
            ]
    return out


def semi_finished(item_number: str, **kw: Any) -> dict[str, Any]:
    """「某料號的半成品有哪些」：explode(semi_only=True) 再依兩種定義分組摘要。

        both            兩種定義都成立（主答案）
        structure_only  自己有 BOM，但未登記為半成品
        type_only       登記為半成品，但自己沒有 BOM（例如標籤、沖壓件）
    """
    out = explode(item_number, semi_only=True, **kw)
    if not out.get("ok"):
        return out
    lines = out["lines"]
    both = sorted({ln["child"] for ln in lines
                   if ln["is_semi_by_type"] and ln["is_semi_by_structure"]})
    structure_only = sorted({ln["child"] for ln in lines
                             if ln["is_semi_by_structure"] and not ln["is_semi_by_type"]})
    type_only = sorted({ln["child"] for ln in lines
                        if ln["is_semi_by_type"] and not ln["is_semi_by_structure"]})
    out["summary"] = {
        "both": both,
        "structure_only": structure_only,
        "type_only": type_only,
        "note": ("both / structure_only 才是「自己還有 BOM」的真半成品；"
                 "type_only 是登記為半成品但沒有自己 BOM 的料"
                 "（大宗為標籤與金屬沖壓件）。"),
    }
    out["answer"] = _answer_semi(out, both, structure_only, type_only)
    return out


def _answer_semi(out: dict[str, Any], both: list[str],
                 structure_only: list[str], type_only: list[str]) -> str:
    """把半成品結果講成一句話。主答案為「自己還有 BOM」的那些，type_only 另外附註。"""
    root = out["root"]
    who = f"{root}（{out.get('root_name') or '無品名'}）"
    real = both + structure_only
    scanned = out.get("rows_scanned", 0)

    if real:
        shown = "、".join(real[:20]) + ("…" if len(real) > 20 else "")
        ans = f"{who}底下有 {len(real)} 種半成品（自己還有 BOM 的）：{shown}。"
    elif scanned == 0:
        ans = f"{who}沒有現行 BOM，它是最底層的採購件（或尚未建 BOM），談不上半成品。"
    else:
        ans = (f"{who}底下沒有半成品 —— 往下展開的 {scanned} 列全部是採購件，"
               f"沒有任何子件自己還有 BOM。")
        if _is_semi_type(out.get("part_type")):
            ans += f"（{root} 自己登記為半成品，它就是那一層。）"
    if type_only:
        shown = "、".join(type_only[:20]) + ("…" if len(type_only) > 20 else "")
        ans += (f" 另外有 {len(type_only)} 個子件登記為半成品但自己"
                f"沒有 BOM（標籤、沖壓件之類），不是 RD 口中的半成品：{shown}。")
    if out.get("truncated"):
        ans += f" ★ 結果被列數上限截斷，上面**不是**完整的清單。"
    return ans


# ---------------------------------------------------------------------------
# 回歸測試
# ---------------------------------------------------------------------------
# join 寫錯的症狀是「查得到、但數量不對」而不是報錯，只能靠固定基準值抓。
# ※ 料號與基準值皆為去識別化的示意值；接上實際資料庫時請先量測後替換。
_BASELINE = {
    "CBL-A30003-8 的一階明細數": 1,
    "CBL-A10001-1 最深層數": 12,
    "ASM-B40004-Q 展開列數": 1_466,
    "ASM-B40004-Q 相異子件數": 96,
}


def selftest() -> int:
    """回傳不符的條數，0 = 全部相符。輸出只用 cp950 可編碼的字元（Windows 主控台）。"""
    bad = 0
    got: dict[str, int] = {}
    t0 = time.time()

    r = explode("CBL-A30003-8", max_level=1)
    got["CBL-A30003-8 的一階明細數"] = len(r["lines"])

    r = explode("CBL-A10001-1")
    got["CBL-A10001-1 最深層數"] = r["max_level_reached"]
    depth_first_ok = _is_depth_first(r["lines"])

    r = explode("ASM-B40004-Q")
    got["ASM-B40004-Q 展開列數"] = len(r["lines"])
    got["ASM-B40004-Q 相異子件數"] = len({x["child"] for x in r["lines"]})

    print("\n■ 基準比對")
    for k, want in _BASELINE.items():
        have = got[k]
        ok = have == want
        bad += not ok
        mark = "相符" if ok else f"不符！基準 {want:,}"
        print(f"  {k:36s} {have:>10,}  {mark}")

    print("\n■ 迴圈防護")
    # 自環料號：唯一的邊就是自環，濾掉後展開必須為空且秒回
    t = time.time()
    r = explode("STM-E70007")
    el = time.time() - t
    ok = r["ok"] and not r["lines"] and el < 5
    bad += not ok
    print(f"  自環料號 STM-E70007 展開 {len(r['lines'])} 列 / {el:.2f}s  "
          f"{'相符（沒有無限迴圈）' if ok else '不符！'}")

    # 列數上限：截斷後必須標記 truncated
    r = explode("ASM-B40004-Q", max_rows=100)
    ok = len(r["lines"]) == 100 and r["truncated"]
    bad += not ok
    print(f"  max_rows=100 -> {len(r['lines'])} 列 truncated={r['truncated']}  "
          f"{'相符' if ok else '不符！截斷沒有被標出來'}")

    # 深度上限
    r = explode("CBL-A10001-1", max_level=3)
    ok = r["max_level_reached"] == 3
    bad += not ok
    print(f"  max_level=3 -> 最深 {r['max_level_reached']} 層  {'相符' if ok else '不符！'}")

    # 失敗預算：不存在的料號應在三種策略用完後停止
    r = resolve("這個料號不存在XYZ999")
    ok = (not r["found"]) and sum(1 for t_ in r["tried"] if not t_.get("ok")) == guard.MAX_FAILS
    bad += not ok
    print(f"  查無料號 -> 試了 {len(r['tried'])} 種策略後停止  "
          f"{'相符（失敗 3 次就停）' if ok else '不符！'}")

    print("\n■ 已知答案")
    bad += not depth_first_ok
    print(f"  CBL-A10001-1 多階列為深度優先  "
          f"{'相符（各半成品子樹沒有交錯）' if depth_first_ok else '不符！同階分支互相交錯'}")

    r = bom_line("CBL-A20002-1")
    ok = len(r["lines"]) == 10 and r["lines"][0]["child"] == "PRT-D60006-B"
    bad += not ok
    print(f"  CBL-A20002-1 一階 {len(r['lines'])} 項，第一項 "
          f"{r['lines'][0]['child'] if r['lines'] else '無'}  {'相符' if ok else '不符！'}")

    # 旗標型別：字串 '0'/'1' 必須被正確解讀
    alt_no = sum(ln["is_alternate"] for ln in r["lines"])
    alt_yes = bom_line("CBL-A30003-8")["lines"][0]["is_alternate"]
    ok = alt_no == 0 and alt_yes is True
    bad += not ok
    print(f"  替代料旗標 CBL-A20002-1={alt_no} 項 / CBL-A30003-8={alt_yes}  "
          f"{'相符（字串 0/1 有被正確解讀）' if ok else '不符！'}")

    # 一階與全深度要一起測，只測一階抓不到「遞迴沒展下去」的錯
    r = semi_finished("CBL-A10001-1", max_level=1)
    ok = r["summary"]["both"] == ["SUB-C50005-1"]
    bad += not ok
    print(f"  CBL-A10001-1 一階真半成品 {r['summary']['both']}  {'相符' if ok else '不符！'}")

    r = semi_finished("CBL-A10001-1")
    ok = (len(r["summary"]["both"]) == 22 and r["line_count"] == 45
          and r["max_level_reached"] == 12)
    bad += not ok
    print(f"  CBL-A10001-1 全深度半成品 {r['line_count']} 列 / "
          f"真半成品 {len(r['summary']['both'])} 個 / 最深 {r['max_level_reached']} 層  "
          f"{'相符' if ok else '不符！'}")

    print(f"\n{'全部相符' if bad == 0 else f'★ {bad} 條不符'}  （{time.time() - t0:.1f}s）")
    return bad


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="BOM 查詢引擎")
    ap.add_argument("item_number", nargs="?", help="料號 / 舊料號")
    ap.add_argument("--selftest", action="store_true", help="跑回歸測試")
    ap.add_argument("--semi", action="store_true", help="只看半成品")
    ap.add_argument("--level", type=int, default=guard.MAX_LEVEL)
    ap.add_argument("--like", help="對品名做子字串過濾，例 --like L=2M")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(1 if selftest() else 0)
    if not args.item_number:
        ap.error("要給料號，或用 --selftest")

    fn = semi_finished if args.semi else explode
    out = fn(args.item_number, max_level=args.level, name_like=args.like)
    if not out.get("ok"):
        print(f"失敗：{out.get('error')}")
        for t in out.get("tried", []):
            print(f"   {'成功' if t.get('ok') else '失敗'}  {t['step']}"
                  f"{'  ' + t['error'] if t.get('error') else ''}")
        sys.exit(1)

    print(f"{out['root']}  {out['root_name']}")
    print(f"（比對方式 {out['matched_by']}，{out['line_count']} 列，"
          f"最深 {out['max_level_reached']} 層）\n")
    for ln in out["lines"]:
        pad = "  " * ln["lvl"]
        tag = "".join((
            "半" if ln["is_semi_by_structure"] else ("s" if ln["is_semi_by_type"] else " "),
            "虛" if ln["is_phantom"] else " ",
            "替" if ln["is_alternate"] else " ",
        ))
        print(f"  L{ln['lvl']:<2}{tag} {pad}{ln['child']:<18} x{ln['qty_eff']:<10.4f} "
              f"{(ln.get('child_name') or '')[:46]}")
    for d in out.get("degraded", []):
        print(f"\n★ {d}")
    if args.semi:
        s = out["summary"]
        print(f"\n真半成品（兩種定義都成立）  {s['both']}")
        print(f"只有結構定義成立            {s['structure_only']}")
        print(f"只有登記值成立              {s['type_only']}")


if __name__ == "__main__":
    main()
