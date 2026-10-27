"""迴圈與失敗防護：所有上限值集中在這裡，避免各處設定漂移。

    第 1 層  查詢圖遞迴      MAX_LEVEL / MAX_ROWS / MAXRECURSION（套用於展開查詢）
    第 2 層  查詢失敗預算    Budget：一次請求失敗 3 次就終止
    第 3 層  下游斷路器      Breaker：連續失敗 3 次就停止呼叫下游
    第 4 層  LLM 工具迴圈    由 A2A 框架限制最大步數

這些失敗（資料庫連不上、料號不存在、下游沒開）重試也不會好，
所以一律「計數然後停」，不做自動重試。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# 第 1 層：查詢圖遞迴上限
# ---------------------------------------------------------------------------
# 資料中最深的 BOM 為 12 層
MAX_LEVEL = 12

# MSSQL 硬殺上限，刻意高於 MAX_LEVEL：正常應由深度閘先擋下，觸發代表邏輯有錯。
# src/bom.py 會用這個值覆寫展開查詢檔裡的字面值。
MAXRECURSION = 20

# 最大單張展開約 1,500 列，保留約 3 倍餘裕
MAX_ROWS = 5_000

# ---------------------------------------------------------------------------
# 逾時（兩種都要設）
# ---------------------------------------------------------------------------
LOGIN_TIMEOUT = 10        # 登入逾時：伺服器不在時多久放棄
STATEMENT_TIMEOUT = 20    # 查詢逾時：遞迴 CTE 失控時的煞車

# ---------------------------------------------------------------------------
# 第 2 / 3 層共用的次數
# ---------------------------------------------------------------------------
MAX_FAILS = 3


class BudgetExhausted(RuntimeError):
    """失敗次數用完。帶著 `tried`，讓錯誤訊息說得出試過哪些步驟。"""

    def __init__(self, tried: list[dict[str, Any]]) -> None:
        self.tried = tried
        steps = "、".join(t["step"] for t in tried if not t.get("ok"))
        super().__init__(f"連續失敗 {sum(1 for t in tried if not t.get('ok'))} 次後停止（{steps}）")


class Budget:
    """一次 API 請求的失敗預算，第 MAX_FAILS 次失敗就終止。

        b = Budget()
        row = b.attempt("精確料號", lambda: _q(SQL_EXACT, [pn]))
        row = row or b.attempt("正規化", lambda: _q(SQL_NORM, [pn]))
        row = row or b.attempt("舊料號", lambda: _q(SQL_OLD, [pn]))

    fn 回傳 None（查無結果）也算一次失敗。
    """

    def __init__(self, max_fails: int = MAX_FAILS) -> None:
        self.max_fails = max_fails
        self.fails = 0
        self.tried: list[dict[str, Any]] = []

    @property
    def exhausted(self) -> bool:
        return self.fails >= self.max_fails

    def attempt(self, step: str, fn: Callable[[], T | None]) -> T | None:
        """執行 fn。成功回結果，失敗記一筆並回 None；預算用完時丟 BudgetExhausted。"""
        if self.exhausted:
            raise BudgetExhausted(self.tried)
        try:
            out = fn()
        except Exception as e:                  # noqa: BLE001
            self._fail(step, f"{type(e).__name__}: {e}")
            return None
        if out is None:
            self._fail(step, "查無結果")
            return None
        self.tried.append({"step": step, "ok": True})
        return out

    def _fail(self, step: str, why: str) -> None:
        self.fails += 1
        self.tried.append({"step": step, "ok": False, "error": why})
        if self.exhausted:
            raise BudgetExhausted(self.tried)


class Breaker:
    """下游斷路器：連續失敗 max_consecutive 次就開路，之後直接短路。

    用「連續」而非「累計」：偶發逾時不該讓整批放棄，成功一次即歸零。
    """

    def __init__(self, name: str, max_consecutive: int = MAX_FAILS) -> None:
        self.name = name
        self.max_consecutive = max_consecutive
        self.consecutive = 0
        self.last_error: str | None = None
        self.calls = 0
        self.failures = 0

    @property
    def is_open(self) -> bool:
        return self.consecutive >= self.max_consecutive

    def ok(self) -> None:
        self.calls += 1
        self.consecutive = 0

    def fail(self, why: str) -> None:
        self.calls += 1
        self.failures += 1
        self.consecutive += 1
        self.last_error = why

    def reason(self) -> str | None:
        """開路原因，直接放進回應的 `degraded` 陣列。"""
        if not self.is_open:
            return None
        return (f"{self.name} 連續 {self.consecutive} 次失敗，已停止呼叫"
                f"（最後一次：{self.last_error}）")
