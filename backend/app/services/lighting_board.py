"""回路棋盘：路灯照度级别裁决的版本化状态机。

一条路灯回路同时被三套照度阈值判定规则命中时，杆号清单、抢修日历、车辆路线过去会
各算各的，给出互不一致的抢修级别。棋盘把这三处规则收成同一处裁决：

    测光核验 → 主管复核 → 故障包合并 → 车辆下发 → 已闭环

约束：
* 只能顺序推进，跳级（如未复核直接合并/下发）由服务端拒绝；
* 同一回路同一时刻只允许一个在执行的版本，并发下发时 stale 版本一律 409；
* 三套阈值规则结论不一致、或道路等级与现场安全结论不一致时，以照明主管复核为准；
* 故障包按「回路#版本」幂等重算，阶段推进与路径落库在同一事务里提交/回滚；
* 裁决结论回写灯具台账、维修清单、车辆路径与抢修日历；
* 灯具台账上的历史灭灯记录只追加、不改写，永久保留当时阈值快照。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "lighting"
CIRCUIT_MODULE = "lighting_circuit"
REPAIR_MODULE = "lighting_repair"
ROUTE_MODULE = "lighting_route"
CALENDAR_MODULE = "lighting_calendar"

# 状态机阶段：严格顺序，下标即次序。
STAGES = ["测光核验", "主管复核", "故障包合并", "车辆下发", "执行中", "已闭环"]
STAGE_FIRST = STAGES[0]
STAGE_CLOSED = STAGES[-1]

# 抢修级别：一级最急。
LEVELS = ["一级抢修", "二级抢修", "三级抢修"]
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}

# 三套照度阈值判定规则各自的分级带宽（单位 lx）：
# 实测读数 < 一级边界 → 一级；< 二级边界 → 二级；否则三级。
# 同一读数在三套规则下可能落到不同级别——这正是要由棋盘统一裁决的冲突来源。
SOURCE_BANDS: dict[str, dict[str, float]] = {
    "杆号清单": {"一级": 8.0, "二级": 15.0},
    "抢修日历": {"一级": 10.0, "二级": 20.0},
    "车辆路线": {"一级": 6.0, "二级": 12.0},
}
# 各源“是否故障”的阈值版本，随测光核验一起冻结。
DEFAULT_THRESHOLDS = {name: bands["二级"] for name, bands in SOURCE_BANDS.items()}

ROAD_GRADE_LEVEL = {
    "快速路": "一级抢修",
    "主干道": "一级抢修",
    "次干道": "二级抢修",
    "支路": "三级抢修",
}
# 现场安全结论关键字 → 安全级别。
SAFETY_HIGH = ("学校", "路口", "行人", "事故", "高架", "匝道", "密集")
SAFETY_LOW = ("流量低", "无行人", "封闭施工", "绿化带")

PLAN_DAYS = {"一级抢修": 1, "二级抢修": 3, "三级抢修": 7}
FAULT_STATUSES = {"不亮", "闪烁"}


class BoardError(Exception):
    """棋盘操作被拒绝：400=入参不合法，409=跳级/版本过期/并发冲突，404=回路不存在。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return date.today().isoformat()


def _max_level(levels: list[str]) -> str:
    return min(levels, key=lambda item: LEVEL_RANK[item])


def _level_from_reading(reading: float, bands: dict[str, float]) -> str:
    if reading < bands["一级"]:
        return "一级抢修"
    if reading < bands["二级"]:
        return "二级抢修"
    return "三级抢修"


def _safety_level(conclusion: str) -> str:
    if any(word in conclusion for word in SAFETY_HIGH):
        return "一级抢修"
    if any(word in conclusion for word in SAFETY_LOW):
        return "三级抢修"
    return "二级抢修"


def _adjudicate(board: dict[str, Any]) -> dict[str, Any]:
    """按三套阈值规则 + 道路等级 + 现场安全，给出统一裁决建议与冲突清单。

    这里只产出“建议级别”和冲突说明；最终级别必须等主管复核敲章。
    """
    reading = float(board.get("测光读数", 0) or 0)
    per_source = {
        source: _level_from_reading(reading, bands)
        for source, bands in SOURCE_BANDS.items()
    }
    grade_level = ROAD_GRADE_LEVEL.get(str(board.get("道路等级", "")), "二级抢修")
    safety_level = _safety_level(str(board.get("现场安全结论", "")))

    conflicts: list[str] = []
    source_items = list(per_source.items())
    for index, (name_a, level_a) in enumerate(source_items):
        for name_b, level_b in source_items[index + 1:]:
            if level_a != level_b:
                conflicts.append(f"{name_a}判定{level_a}、{name_b}判定{level_b}，抢修级别不一致")
    if grade_level != safety_level:
        conflicts.append(
            f"道路等级（{board.get('道路等级')}）建议{grade_level}，"
            f"现场安全结论建议{safety_level}，二者冲突，以照明主管复核为准"
        )

    suggested = _max_level([*per_source.values(), grade_level, safety_level])
    return {
        "各源判定": {**per_source, "道路等级": grade_level, "现场安全": safety_level},
        "冲突规则": conflicts,
        "建议级别": suggested,
    }


def _board_view(board: dict[str, Any]) -> dict[str, Any]:
    """列表/详情共用的同一份裁决视图，避免列表与详情根因不同步。"""
    view = dict(board)
    view["故障包键"] = f"{board['回路编号']}#{board['版本']}"
    if board.get("最终抢修级别"):
        view["裁决级别"] = board["最终抢修级别"]
    elif board.get("各源判定"):
        source_levels = [
            level for name, level in board["各源判定"].items()
            if name in SOURCE_BANDS
        ]
        view["裁决级别"] = _max_level(source_levels) if source_levels else ""
    else:
        view["裁决级别"] = ""
    view["存在冲突"] = bool(board.get("冲突规则"))
    return view


class CircuitBoardService:
    # ---- 查询 -----------------------------------------------------------
    def list_boards(
        self,
        *,
        keyword: str | None = None,
        stage: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(CIRCUIT_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("回路编号", ""))
                    or keyword in str(row.get("回路名称", ""))
                    or keyword in str(row.get("所属路段", ""))]
        if stage:
            rows = [row for row in rows if row.get("阶段") == stage]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_board_view(row) for row in rows[start:start + size]], total

    def get_board(self, code: str) -> dict[str, Any]:
        board = self._find(code)
        view = _board_view(board)
        key = view["故障包键"]
        lamps = [row for row in store.rows(MODULE) if row.get("回路编号") == code]
        view["杆号清单"] = sorted(
            (str(row.get("杆号", "")) for row in lamps if row.get("status") in FAULT_STATUSES)
        )
        view["灯具台账"] = lamps
        view["维修清单"] = [row for row in store.rows(REPAIR_MODULE) if row.get("故障包键") == key]
        view["车辆路线"] = store.find_by(ROUTE_MODULE, "故障包键", key)
        view["抢修日历"] = [row for row in store.rows(CALENDAR_MODULE) if row.get("故障包键") == key]
        return view

    def list_repairs(self, *, circuit: str | None = None, level: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(REPAIR_MODULE)
        if circuit:
            rows = [row for row in rows if row.get("回路编号") == circuit]
        if level:
            rows = [row for row in rows if row.get("抢修级别") == level]
        return rows

    def list_routes(self, *, circuit: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(ROUTE_MODULE)
        if circuit:
            rows = [row for row in rows if row.get("回路编号") == circuit]
        return rows

    def list_calendar(self, *, circuit: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CALENDAR_MODULE)
        if circuit:
            rows = [row for row in rows if row.get("回路编号") == circuit]
        return rows

    # ---- 状态机推进 ------------------------------------------------------
    def open_version(self, values: dict[str, Any]) -> dict[str, Any]:
        """新开一版裁决：已有未闭环版本时拒绝（并发下发只允许一个版本进入执行）。"""
        code = str(values.get("回路编号") or "").strip()
        if not code:
            raise BoardError(400, "缺少回路编号，无法开启裁决版本")
        name = str(values.get("回路名称") or code).strip()
        road = str(values.get("所属路段") or "").strip()
        grade = str(values.get("道路等级") or "").strip()
        if grade and grade not in ROAD_GRADE_LEVEL:
            raise BoardError(400, f"道路等级「{grade}」不在支持范围：{ '、'.join(ROAD_GRADE_LEVEL)}")

        with store.lock:
            existing = store.find_by(CIRCUIT_MODULE, "回路编号", code)
            if existing is not None and existing.get("阶段") != STAGE_CLOSED:
                raise BoardError(
                    409,
                    f"回路 {code} 的 v{existing['版本']} 仍处于「{existing['阶段']}」，"
                    f"必须闭环后才能开启新版本（同一回路只允许一个执行版本）",
                )
            with store.transaction():
                board = store.find_by(CIRCUIT_MODULE, "回路编号", code)
                if board is None:
                    board = {
                        "id": store.next_id(CIRCUIT_MODULE),
                        "回路编号": code,
                        "版本": 0,
                        "阶段": STAGE_CLOSED,
                        "历史版本": [],
                    }
                    store.rows(CIRCUIT_MODULE).append(board)
                if board.get("版本"):
                    board.setdefault("历史版本", []).append(self._archive(board))
                board["回路名称"] = name or board.get("回路名称", code)
                board["所属路段"] = road or board.get("所属路段", "")
                if grade:
                    board["道路等级"] = grade
                board["版本"] = int(board.get("版本", 0)) + 1
                board.update({
                    "阶段": STAGE_FIRST,
                    "测光读数": None,
                    "现场安全结论": str(values.get("现场安全结论") or "").strip(),
                    "证据核对人": "",
                    "主管复核人": "",
                    "主管裁决级别": "",
                    "主管复核意见": "",
                    "阈值版本": dict(DEFAULT_THRESHOLDS),
                    "各源判定": {},
                    "最终抢修级别": "",
                    "冲突规则": [],
                    "故障包": None,
                    "开启时间": _now(),
                    "测光核验时间": "",
                    "主管复核时间": "",
                    "合并时间": "",
                    "下发时间": "",
                    "闭环时间": "",
                })
            return _board_view(board)

    def verify_photometry(self, values: dict[str, Any]) -> dict[str, Any]:
        """阶段一：调度员核对测光证据，冻结阈值版本，推进到主管复核。"""
        with store.lock:
            board = self._load_active(values)
            self._assert_stage(board, STAGE_FIRST)
            reading_raw = values.get("测光读数")
            try:
                reading = float(reading_raw)
            except (TypeError, ValueError):
                raise BoardError(400, "测光读数必须是数字（单位 lx）")
            if reading < 0:
                raise BoardError(400, "测光读数不能为负值")
            checker = str(values.get("证据核对人") or "").strip()
            if not checker:
                raise BoardError(400, "请填写测光证据核对人")
            thresholds = self._parse_thresholds(values.get("阈值版本"))
            safety = str(values.get("现场安全结论") or board.get("现场安全结论") or "").strip()

            with store.transaction():
                board["测光读数"] = reading
                board["现场安全结论"] = safety
                board["阈值版本"] = thresholds
                board["证据核对人"] = checker
                verdict = _adjudicate(board)
                board["各源判定"] = verdict["各源判定"]
                board["冲突规则"] = verdict["冲突规则"]
                board["测光核验时间"] = _now()
                board["阶段"] = "主管复核"
            return _board_view(board)

    def supervisor_review(self, values: dict[str, Any]) -> dict[str, Any]:
        """阶段二：照明主管复核敲章。规则结论冲突时，一律以主管裁决为准。"""
        with store.lock:
            board = self._load_active(values)
            self._assert_stage(board, "主管复核")
            level = str(values.get("主管裁决级别") or "").strip()
            if level not in LEVELS:
                raise BoardError(400, f"主管裁决级别必须是：{'、'.join(LEVELS)}")
            reviewer = str(values.get("主管复核人") or "").strip()
            if not reviewer:
                raise BoardError(400, "请填写照明主管复核人")

            with store.transaction():
                board["主管裁决级别"] = level
                board["最终抢修级别"] = level  # 主管复核结论优先，覆盖各阈值规则建议
                board["主管复核人"] = reviewer
                board["主管复核意见"] = str(values.get("主管复核意见") or "").strip()
                board["主管复核时间"] = _now()
                board["阶段"] = "故障包合并"
            return _board_view(board)

    def merge_package(self, values: dict[str, Any]) -> dict[str, Any]:
        """阶段三：合并故障包并幂等重算，回写台账/维修清单/抢修日历（同一事务）。

        在「车辆下发」阶段再次提交视为幂等重算：内容不变就复用既有包，不推进阶段，
        这样调度员下发前反复核对杆号也不会产生重复维修行。
        """
        with store.lock:
            board = self._load_active(values)
            current = str(board.get("阶段", ""))
            if current not in {"故障包合并", "车辆下发"}:
                self._assert_stage(board, "故障包合并")
            if not board.get("最终抢修级别"):
                raise BoardError(409, "尚未取得照明主管复核结论，不能合并故障包")

            key = f"{board['回路编号']}#{board['版本']}"
            with store.transaction():
                package = self._recompute_package(board, key)
                board["故障包"] = package
                if current == "故障包合并":
                    board["合并时间"] = board.get("合并时间") or _now()
                    board["阶段"] = "车辆下发"
        return _board_view(board)

    def dispatch_vehicle(self, values: dict[str, Any]) -> dict[str, Any]:
        """阶段四：下发车辆，路径与阶段在同一事务落库。

        已经在执行中的版本不允许重复下发；过期版本号由乐观锁拒绝，
        从而保证并发下发时同一回路只有一个版本进入执行。
        """
        with store.lock:
            board = self._load_active(values)
            current = str(board.get("阶段", ""))
            if current == "执行中":
                raise BoardError(
                    409,
                    f"回路 {board['回路编号']} v{board['版本']} 已在执行中，"
                    f"重复下发被拒绝（一个版本只允许进入一次执行）",
                )
            self._assert_stage(board, "车辆下发")
            package = board.get("故障包")
            if not package:
                raise BoardError(409, "故障包尚未合并，不能下发车辆")

            poles = list(package["杆号清单"])
            if not poles:
                raise BoardError(400, "故障包内没有待修杆号，无需下发车辆")
            vehicle_no = str(values.get("车辆编号") or "").strip() or self._pick_vehicle()
            vehicle = store.find_by("vehicle", "车辆编号", vehicle_no)
            if vehicle is None:
                raise BoardError(400, f"车辆 {vehicle_no} 不在车辆台账内")

            key = f"{board['回路编号']}#{board['版本']}"
            with store.transaction():
                now = _now()
                # 路径落库：按故障包键 upsert，同版本重复下发不会造第二条路线。
                store.upsert(ROUTE_MODULE, "故障包键", key, {
                    "回路编号": board["回路编号"],
                    "版本": board["版本"],
                    "抢修级别": board["最终抢修级别"],
                    "车辆编号": vehicle_no,
                    "车辆类型": vehicle.get("车辆类型", ""),
                    "停靠点": poles,
                    "路径状态": "执行中",
                    "下发时间": now,
                })
                store.upsert(REPAIR_MODULE, "故障包键", key, {"维修状态": "已下发", "更新时间": now})
                for lamp in self._fault_lamps(board["回路编号"], poles):
                    lamp["作业状态"] = "出车抢修中"
                    lamp["pending"] = True
                board["下发时间"] = now
                board["阶段"] = "执行中"
                board["闭环时间"] = ""
        return _board_view(board)

    def complete_version(self, values: dict[str, Any]) -> dict[str, Any]:
        """执行回执：闭环当前版本，台账/清单/路径一起归档（历史灭灯记录原样保留）。"""
        with store.lock:
            board = self._load_active(values)
            self._assert_stage(board, "执行中")
            key = f"{board['回路编号']}#{board['版本']}"
            with store.transaction():
                now = _now()
                package = board.get("故障包") or {}
                poles = set(package.get("杆号清单", []))
                for lamp in store.rows(MODULE):
                    if lamp.get("回路编号") == board["回路编号"] and (not poles or lamp.get("杆号") in poles):
                        lamp["status"] = "已修复"
                        lamp["pending"] = False
                        lamp["abnormal"] = False
                        lamp["设施状态"] = "已修复"
                        lamp["作业状态"] = "已闭环"
                store.upsert(REPAIR_MODULE, "故障包键", key, {"维修状态": "已闭环", "更新时间": now})
                store.upsert(ROUTE_MODULE, "故障包键", key, {"路径状态": "已闭环"})
                store.upsert(CALENDAR_MODULE, "故障包键", key, {"抢修日历状态": "已闭环"})
                board["阶段"] = STAGE_CLOSED
                board["闭环时间"] = now
        return _board_view(board)

    # ---- 内部方法 --------------------------------------------------------
    def _recompute_package(self, board: dict[str, Any], key: str) -> dict[str, Any]:
        """按「回路#版本」幂等重算故障包：同一键内容不变只返回既有包。"""
        lamps = [row for row in store.rows(MODULE)
                 if row.get("回路编号") == board["回路编号"] and row.get("status") in FAULT_STATUSES]
        lamps.sort(key=lambda row: str(row.get("杆号", "")))
        poles = [str(row.get("杆号", "")) for row in lamps]
        level = board["最终抢修级别"]
        thresholds = dict(board.get("阈值版本") or DEFAULT_THRESHOLDS)
        plan_date = (date.today() + timedelta(days=PLAN_DAYS[level])).isoformat()

        previous = board.get("故障包")
        recompute_count = int(previous.get("重算次数", 0)) if previous else 0
        package = {
            "故障包键": key,
            "杆号清单": poles,
            "灯具数": len(poles),
            "建议级别": level,
            "重算版本": board["版本"],
            "重算次数": recompute_count or 1,
            "计划日期": plan_date,
        }
        if previous is None or previous.get("杆号清单") != poles or previous.get("建议级别") != level:
            package["重算次数"] = recompute_count + 1
        else:
            package["重算次数"] = recompute_count
            package["计划日期"] = previous.get("计划日期", plan_date)

        with store.transaction():
            now = _now()
            for lamp in lamps:
                # 裁决结论回写灯具台账；灭灯记录只追加，阈值快照定格在本版本。
                lamp["故障包键"] = key
                lamp["裁决版本"] = board["版本"]
                lamp["抢修级别"] = level
                lamp["裁决阈值快照"] = thresholds
                lamp["设施状态"] = f"待抢修·{level}"
            store.upsert(REPAIR_MODULE, "故障包键", key, {
                "回路编号": board["回路编号"],
                "版本": board["版本"],
                "杆号清单": poles,
                "抢修级别": level,
                "维修状态": "待下发",
                "所属路段": board.get("所属路段", ""),
                "不亮原因": "、".join(sorted({str(r.get("不亮原因", "")) for r in lamps if r.get("不亮原因")})),
                "计划日期": package["计划日期"],
                "阈值快照": thresholds,
                "更新时间": now,
            })
            store.upsert(CALENDAR_MODULE, "故障包键", key, {
                "回路编号": board["回路编号"],
                "版本": board["版本"],
                "抢修级别": level,
                "计划日期": package["计划日期"],
                "抢修日历状态": "待下发",
            })
        return package

    def _fault_lamps(self, code: str, poles: list[str]) -> list[dict[str, Any]]:
        pole_set = set(poles)
        return [
            row for row in store.rows(MODULE)
            if row.get("回路编号") == code and row.get("杆号") in pole_set
        ]

    def _pick_vehicle(self) -> str:
        vehicles = store.rows("vehicle")
        preferred = next((row for row in vehicles if "高空" in str(row.get("车辆类型", ""))), None)
        chosen = preferred or (vehicles[0] if vehicles else None)
        if chosen is None:
            raise BoardError(400, "车辆台账为空，无法指派抢修车辆")
        return str(chosen["车辆编号"])

    def _archive(self, board: dict[str, Any]) -> dict[str, Any]:
        return {
            "版本": board["版本"],
            "阶段": board["阶段"],
            "最终抢修级别": board.get("最终抢修级别", ""),
            "阈值版本": dict(board.get("阈值版本") or {}),
            "各源判定": dict(board.get("各源判定") or {}),
            "测光读数": board.get("测光读数"),
            "开启时间": board.get("开启时间", ""),
            "闭环时间": board.get("闭环时间", ""),
        }

    def _find(self, code: str) -> dict[str, Any]:
        board = store.find_by(CIRCUIT_MODULE, "回路编号", code)
        if board is None:
            raise BoardError(404, f"回路 {code} 尚未在回路棋盘登记")
        return board

    def _load_active(self, values: dict[str, Any]) -> dict[str, Any]:
        """定位回路并做乐观锁校验：版本号过期的并发请求一律拒绝。"""
        code = str(values.get("回路编号") or "").strip()
        if not code:
            raise BoardError(400, "缺少回路编号")
        board = self._find(code)
        expected = values.get("版本")
        if expected is None:
            raise BoardError(400, "缺少裁决版本号，请基于棋盘当前版本提交")
        try:
            expected_version = int(expected)
        except (TypeError, ValueError):
            raise BoardError(400, "裁决版本号必须是整数")
        if expected_version != int(board.get("版本", 0)):
            raise BoardError(
                409,
                f"提交针对 v{expected_version}，回路 {code} 当前是 v{board['版本']}，"
                f"该版本已过期（冲突时以主管复核后的最新版本为准）",
            )
        return board

    def _assert_stage(self, board: dict[str, Any], expected: str) -> None:
        current = str(board.get("阶段", ""))
        if current == expected:
            return
        current_index = STAGES.index(current) if current in STAGES else -1
        expected_index = STAGES.index(expected)
        if current in STAGES and current_index > expected_index:
            raise BoardError(409, f"回路 {board['回路编号']} 已推进到「{current}」，不能回退到「{expected}」")
        raise BoardError(
            409,
            f"跳级被拒绝：回路 {board['回路编号']} v{board['版本']} 当前在「{current}」，"
            f"必须按 {' → '.join(STAGES)} 顺序推进，不能直接执行「{expected}」",
        )

    @staticmethod
    def _parse_thresholds(raw: Any) -> dict[str, float]:
        thresholds = dict(DEFAULT_THRESHOLDS)
        if raw is None:
            return thresholds
        if not isinstance(raw, dict):
            raise BoardError(400, "阈值版本必须是 {规则名: 阈值} 的映射")
        for name in SOURCE_BANDS:
            if name not in raw:
                continue
            try:
                value = float(raw[name])
            except (TypeError, ValueError):
                raise BoardError(400, f"规则「{name}」的阈值必须是数字")
            if value <= 0:
                raise BoardError(400, f"规则「{name}」的阈值必须大于 0")
            thresholds[name] = value
        return thresholds


circuit_board_service = CircuitBoardService()
