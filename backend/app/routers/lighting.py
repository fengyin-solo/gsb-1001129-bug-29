"""路灯照明接口：维护路灯设施，并承载「回路棋盘」照度级别裁决。

裁决纪律：测光核验 → 主管复核 → 故障包下发只能顺序推进，跳级下发由服务端拒绝；
冲突以照明主管复核为准；故障包按回路版本幂等重算；并发下发仅允许一个版本进入执行。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.circuit_board import (
    AdjudicationError,
    LEVEL_ORDER,
    circuit_board,
)
from app.services.lighting import LightingService

router = APIRouter(prefix="/api/lighting", tags=["路灯照明"])

service = LightingService()

LIST_FIELDS = ["灯具编号", "灯具类型", "功率", "所属路段", "安装日期", "杆号", "不亮原因", "设施状态"]
STATUSES = ["正常", "不亮", "闪烁", "已修复"]


def _raise_if_rejected(exc: AdjudicationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ---------------------------------------------------------------- 灯具台账
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按灯具编号检索"),
    status: str | None = Query(default=None, description="正常、不亮、闪烁、已修复"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按灯具编号与状态过滤路灯照明列表；没有数据时返回空页，不报错。

    同一回路的照度裁决根因、版本与阶段一并透出，影响列表口径。
    """
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出路灯照明清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lighting", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条路灯设施明细；不存在时给出可读的错误说明。详情透出裁决根因。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"路灯设施 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条路灯设施，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="路灯设施已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条路灯设施执行登记故障、派发修复、确认修复；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# --------------------------------------------------------------- 回路棋盘 DTO
class MeteringReading(BaseModel):
    杆号: str
    照度: float
    现场安全结论: str | None = None


class MeteringPayload(BaseModel):
    readings: list[MeteringReading] = Field(default_factory=list)
    inspector: str = Field(default="值班测光员")
    expected_version: int | None = None


class ReviewPayload(BaseModel):
    reviewed_level: str
    reviewer: str = Field(default="照明主管")
    remark: str = Field(default="")
    expected_version: int | None = None


class DispatchPayload(BaseModel):
    vehicle_id: int
    operator: str = Field(default="值班调度")
    expected_version: int | None = None


class VersionPayload(BaseModel):
    expected_version: int | None = None


# --------------------------------------------------------------- 回路棋盘接口
@router.get("/circuits/board")
def circuit_board_overview() -> dict[str, Any]:
    """回路棋盘总览：每个回路一张棋盘，杆号清单、抢修日历、车辆路线统一在此裁决。"""
    circuits = circuit_board.list_circuits()
    return {
        "stage_order": ["测光核验", "主管复核", "已复核", "执行中", "已归档"],
        "level_order": LEVEL_ORDER,
        "items": circuits,
    }


@router.get("/circuits/repairs")
def list_repairs(circuit_no: str | None = None) -> dict[str, Any]:
    """维修清单：按回路版本幂等落库的裁决故障包。"""
    return {"items": circuit_board.list_repairs(circuit_no)}


@router.get("/circuits/routes")
def list_routes(circuit_no: str | None = None) -> dict[str, Any]:
    """路径汇总页：故障包下发后生成的车辆路线待办。"""
    return {"items": circuit_board.list_routes(circuit_no)}


@router.get("/circuits/outage-history")
def list_outage_history(circuit_no: str | None = None) -> dict[str, Any]:
    """历史灭灯记录：归档时冻结，保留当时阈值快照。"""
    return {"items": circuit_board.list_history(circuit_no)}


@router.get("/circuits/{circuit_no}")
def get_circuit(circuit_no: str) -> dict[str, Any]:
    try:
        return circuit_board.get_circuit(circuit_no)
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/metering")
def verify_metering(circuit_no: str, payload: MeteringPayload) -> dict[str, Any]:
    """阶段一·测光核验：调度员核对测光证据，服务端试算两路规则后推进到主管复核。"""
    try:
        return circuit_board.verify_metering(
            circuit_no,
            [item.model_dump() for item in payload.readings],
            inspector=payload.inspector,
            expected_version=payload.expected_version,
        )
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/review")
def supervisor_review(circuit_no: str, payload: ReviewPayload) -> dict[str, Any]:
    """阶段二·主管复核：道路等级与现场安全结论冲突时，以复核级别为准并回写结论。"""
    try:
        return circuit_board.supervisor_review(
            circuit_no,
            reviewed_level=payload.reviewed_level,
            reviewer=payload.reviewer,
            remark=payload.remark,
            expected_version=payload.expected_version,
        )
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/recompute")
def recompute_package(circuit_no: str, payload: VersionPayload) -> dict[str, Any]:
    """故障包重算：按回路版本幂等，证据/复核结论未变则不重复落库。"""
    try:
        return circuit_board.recompute_package(
            circuit_no, expected_version=payload.expected_version
        )
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/dispatch")
def dispatch(circuit_no: str, payload: DispatchPayload) -> dict[str, Any]:
    """阶段三·故障包下发车辆：阶段与路径同事务落库，并发仅允许一个版本进入执行。"""
    try:
        return circuit_board.dispatch(
            circuit_no,
            vehicle_id=payload.vehicle_id,
            expected_version=payload.expected_version,
            operator=payload.operator,
        )
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/archive")
def archive(circuit_no: str, payload: VersionPayload) -> dict[str, Any]:
    """执行归档：冻结灭灯记录与当时阈值，台账置修复，车辆回库。"""
    try:
        return circuit_board.archive(circuit_no, expected_version=payload.expected_version)
    except AdjudicationError as exc:
        _raise_if_rejected(exc)


@router.post("/circuits/{circuit_no}/new-version")
def open_new_version(circuit_no: str) -> dict[str, Any]:
    """归档后开启下一裁决版本，版本号自增并回到测光核验。"""
    try:
        return circuit_board.open_new_version(circuit_no)
    except AdjudicationError as exc:
        _raise_if_rejected(exc)
