"""路灯照明业务规则：状态流转、字段校验与筛选口径都收在这里。

灯具台账与「回路棋盘」（services/lighting_board.py）的关系：
* 登记故障时追加一条灭灯记录，定格当时的照度阈值与抢修级别，历史记录永不改写；
* 棋盘每推进一版，裁决结论统一回写台账上的抢修级别与阈值快照。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.lighting_board import (
    CIRCUIT_MODULE,
    DEFAULT_THRESHOLDS,
    SOURCE_BANDS,
    STAGE_CLOSED,
    _level_from_reading,
    _max_level,
)
from app.store import store

MODULE = "lighting"
REQUIRED_FIELDS = ["灯具编号", "灯具类型", "功率"]
STATUS_ORDER = ["正常", "不亮", "闪烁", "已修复"]
ACTION_RULES = {"登记故障": "不亮", "派发修复": "已修复", "确认修复": "已修复"}
NEGATIVE_ACTIONS = []


class LightingService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        circuit: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("灯具编号", ""))
                    or keyword in str(row.get("杆号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if circuit:
            rows = [row for row in rows if row.get("回路编号") == circuit]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 回路归属让台账能被棋盘按回路聚合。
        for field in ("所属路段", "安装日期", "杆号", "不亮原因", "设施状态", "回路编号"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["灭灯记录"] = []
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"路灯设施 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于路灯照明可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "登记故障":
            self._append_outage_record(entry, values or {})

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS or target in {"不亮", "闪烁"}
        if action == "登记故障" and values:
            reason = str(values.get("不亮原因") or "").strip()
            if reason:
                entry["不亮原因"] = reason
        return entry, f"路灯设施已{action}"

    def _append_outage_record(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """追加灭灯记录：阈值取回路当前在执行版本的冻结值；记录只追加不改写。"""
        records = entry.setdefault("灭灯记录", [])
        circuit_code = entry.get("回路编号")
        board = None
        if circuit_code:
            board = store.find_by(CIRCUIT_MODULE, "回路编号", circuit_code)
            if board is not None and board.get("阶段") == STAGE_CLOSED:
                board = None

        thresholds = dict(board.get("阈值版本") if board else DEFAULT_THRESHOLDS)
        reading = None
        if values.get("测光读数") is not None:
            try:
                reading = float(values["测光读数"])
            except (TypeError, ValueError):
                reading = None
        elif board is not None and board.get("测光读数") is not None:
            reading = float(board["测光读数"])

        level = ""
        if board is not None and board.get("最终抢修级别"):
            level = board["最终抢修级别"]
        elif reading is not None:
            per_source = {
                source: _level_from_reading(reading, bands)
                for source, bands in SOURCE_BANDS.items()
            }
            level = _max_level(list(per_source.values()))
        # 读不到读数时暂不定级，保留空值，由棋盘后续裁决补齐。

        records.append({
            "日期": date.today().isoformat(),
            "故障包版本": int(board["版本"]) if board else None,
            "当时照度阈值": thresholds,
            "当时抢修级别": level,
            "测光读数": reading,
        })
