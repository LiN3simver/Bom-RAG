"""槽位標註：呼叫下游「材料建議 Agent」的 classify_role，為每一列標上
線材 / 連接器 / 端子 / 包材等角色。

* 角色規則由材料建議 Agent 統一維護，本專案不複製一份，避免兩邊判定不一致。
* 槽位不是必要欄位：下游掛掉時主答案照樣完整，只是 role 為 null。
  策略是「降級不失敗」，且絕不自行猜測槽位。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .guard import Breaker

BASE_URL = os.environ.get("MATERIAL_BASE_URL", "http://127.0.0.1:8400").rstrip("/")

# classify_role 是純函式，正常為毫秒級；超過 3 秒視為下游不在
TIMEOUT = 3.0


class Slots:
    """單次請求內的槽位標註器（快取 + 斷路器）。不跨請求共用。"""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        self.breaker = Breaker(f"材料建議 Agent（{base_url}）")
        self._cache: dict[str, dict[str, Any] | None] = {}
        # 共用同一個 Client 重用連線：約 100 次呼叫從 ~11s 降到 ~0.1s
        self._http = httpx.Client(
            base_url=base_url,
            timeout=TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Slots:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 單一料號 ----------------------------------------------------------
    def _fetch(self, item_number: str, name: str | None, part_type: str | None):
        try:
            r = self._http.post(
                "/a2a/tool/classify_role",
                json={"item_number": item_number, "name": name, "part_type": part_type},
            )
            r.raise_for_status()
            out = r.json()
        except Exception as e:  # noqa: BLE001
            self.breaker.fail(f"{type(e).__name__}: {e}")
            return None
        # 工具回傳形狀為 {"ok": bool, ...}，內容在 result 或攤平在頂層
        if isinstance(out, dict) and out.get("ok") is False:
            self.breaker.fail(str(out.get("error") or out.get("detail") or out))
            return None
        self.breaker.ok()
        body = out.get("result") if isinstance(out.get("result"), dict) else out
        return {
            "role": body.get("role"),
            "role_label": body.get("role_label"),
            "role_kind": body.get("role_kind"),
            "is_packaging": body.get("is_packaging"),
        }

    def get(self, item_number: str, name: str | None = None,
            part_type: str | None = None) -> dict[str, Any] | None:
        if item_number in self._cache:
            return self._cache[item_number]
        if self.breaker.is_open:
            return None
        got = self._fetch(item_number, name, part_type)
        # 只快取成功結果，下游中途恢復時後面的列仍能取得槽位
        if got is not None:
            self._cache[item_number] = got
        return got

    # -- 整批標註 ----------------------------------------------------------
    def annotate(self, lines: list[dict[str, Any]]) -> list[str]:
        """就地補上 role / role_label / slot_source，回傳 degraded 訊息。

        只對相異料號呼叫（一張千餘列的 BOM 通常只有百種以內的相異子件）。
        """
        if not lines:
            return []
        seen: dict[str, tuple[str | None, str | None]] = {}
        for ln in lines:
            seen.setdefault(ln["child"], (ln.get("child_name"), ln.get("part_type")))

        for item_number, (name, part_type) in seen.items():
            self.get(item_number, name, part_type)

        for ln in lines:
            got = self._cache.get(ln["child"])
            if got:
                ln.update(got)
                ln["slot_source"] = "material:8400"
            else:
                # 誠實留白：「沒問到」與「問到但未分類」是兩回事，不填 unknown
                ln["role"] = None
                ln["role_label"] = None
                ln["slot_source"] = "unavailable"

        notes: list[str] = []
        if reason := self.breaker.reason():
            notes.append(f"{reason} 槽位欄位從缺，其餘資料不受影響。")
        elif self.breaker.failures:
            notes.append(
                f"槽位標註有 {self.breaker.failures}/{self.breaker.calls} 次呼叫失敗，"
                f"那幾列的 role 是 null。"
            )
        return notes


def annotate(lines: list[dict[str, Any]]) -> list[str]:
    """一次性用法，會自動關閉連線。"""
    with Slots() as s:
        return s.annotate(lines)


def available() -> tuple[bool, str]:
    """下游是否可用。給健康檢查用，永遠不丟例外。"""
    try:
        r = httpx.get(f"{BASE_URL}/a2a/card", timeout=TIMEOUT)
        r.raise_for_status()
        card = r.json()
        return True, f"材料建議 Agent status={card.get('status')}"
    except Exception as e:  # noqa: BLE001
        return False, f"連不上 {BASE_URL}：{type(e).__name__}: {e}"
