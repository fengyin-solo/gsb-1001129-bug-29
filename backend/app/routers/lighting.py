"""路灯照明接口：灯具台账 + 回路棋盘（照度级别裁决版本化状态机）。

棋盘接口一律先于 /{entry_id} 声明，避免被单灯明细路由吞掉。跳级、过期版本、
并发下发等违规操作由服务端抛 409 拒绝。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.lighting import LightingService
from app.services.lighting_board import BoardError, CircuitBoardService

router = APIRouter(prefix="/api/lighting", tags=["路灯照明"])

service = LightingService()
board_service = CircuitBoardService()

LIST_FIELDS = ["灯具编号", "灯具类型", "功率", "所属路段", "安装日期", "杆号", "不亮原因", "设施状态"]
STATUSES = ["正常", "不亮", "闪烁", "已修复"]


def _guard(call: Any) -> dict[str, Any]:
    """统一把棋盘业务异常翻译成 HTTP 状态：404/400/409。"""
    try:
        return call()
    except BoardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ---- 回路棋盘 -------------------------------------------------------------

@router.get("/circuits", response_model=PageResult[dict])
def list_circuits(
    keyword: str | None = Query(default=None, description="按回路编号/名称/路段检索"),
    stage: str | None = Query(default=None, description="按状态机阶段过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """回路棋盘列表：每个回路一个版本化裁决状态机。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = board_service.list_boards(keyword=keyword, stage=stage, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/circuits/{code}", response_model=dict)
def get_circuit(code: str) -> dict[str, Any]:
    """回路棋盘详情：杆号清单、灯具台账、维修清单、车辆路线、抢修日历同屏同口径。"""
    return _guard(lambda: board_service.get_board(code))


@router.post("/circuits/open")
def open_circuit(payload: EntryPayload) -> dict[str, Any]:
    """开启新版本：同一回路已有未闭环版本时返回 409。"""
    return {"ok": True, "message": "裁决版本已开启，等待测光核验",
            "board": _guard(lambda: board_service.open_version(payload.values))}


@router.post("/circuits/verify")
def verify_circuit(payload: EntryPayload) -> dict[str, Any]:
    """阶段一：调度员核对测光证据，冻结阈值版本。"""
    return {"ok": True, "message": "测光证据已核验，进入主管复核",
            "board": _guard(lambda: board_service.verify_photometry(payload.values))}


@router.post("/circuits/review")
def review_circuit(payload: EntryPayload) -> dict[str, Any]:
    """阶段二：照明主管复核；规则冲突时以主管裁决级别为准。"""
    return {"ok": True, "message": "主管复核完成，故障级别已裁定",
            "board": _guard(lambda: board_service.supervisor_review(payload.values))}


@router.post("/circuits/merge")
def merge_circuit(payload: EntryPayload) -> dict[str, Any]:
    """阶段三：幂等合并故障包，结论回写台账、维修清单、抢修日历。"""
    return {"ok": True, "message": "故障包已按回路版本合并重算",
            "board": _guard(lambda: board_service.merge_package(payload.values))}


@router.post("/circuits/dispatch")
def dispatch_circuit(payload: EntryPayload) -> dict[str, Any]:
    """阶段四：下发车辆；阶段推进与路径落库同事务，重复/过期版本 409。"""
    return {"ok": True, "message": "车辆已下发，版本进入执行（同一回路仅允许一个执行版本）",
            "board": _guard(lambda: board_service.dispatch_vehicle(payload.values))}


@router.post("/circuits/complete")
def complete_circuit(payload: EntryPayload) -> dict[str, Any]:
    """闭环当前版本：台账、维修清单、路径、日历统一归档。"""
    return {"ok": True, "message": "裁决版本已闭环，历史灭灯记录与阈值快照已保留",
            "board": _guard(lambda: board_service.complete_version(payload.values))}


@router.get("/repairs/all")
def list_repairs(
    circuit: str | None = Query(default=None),
    level: str | None = Query(default=None),
) -> dict[str, Any]:
    """维修清单汇总页：所有故障包维修行。"""
    return {"items": board_service.list_repairs(circuit=circuit, level=level)}


@router.get("/routes/all")
def list_routes(circuit: str | None = Query(default=None)) -> dict[str, Any]:
    """车辆路径汇总页。"""
    return {"items": board_service.list_routes(circuit=circuit)}


@router.get("/calendar/all")
def list_calendar(circuit: str | None = Query(default=None)) -> dict[str, Any]:
    """抢修日历汇总页。"""
    return {"items": board_service.list_calendar(circuit=circuit)}


# ---- 灯具台账 -------------------------------------------------------------

@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按灯具编号/杆号检索"),
    status: str | None = Query(default=None, description="正常、不亮、闪烁、已修复"),
    circuit: str | None = Query(default=None, description="按回路编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按灯具编号、状态与回路过滤路灯照明列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, circuit=circuit, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出路灯照明清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "lighting", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条路灯设施明细；不存在时给出可读的错误说明。"""
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
    """对单条路灯设施执行登记故障、派发修复、确认修复。

    登记故障会追加一条灭灯记录并保留当时照度阈值；不允许的动作会被拦下并说明原因。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
