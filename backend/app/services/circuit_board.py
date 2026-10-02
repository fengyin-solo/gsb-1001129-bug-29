"""回路棋盘：照度级别裁决的版本化状态机。

业务背景：同一路灯回路同时命中不同照度阈值规则时，杆号清单、抢修日历和车辆路线
各自给出不同抢修级别。这里以「回路 + 版本」为唯一裁决单元，把三处置信源收成一个棋盘：

测光核验 → 主管复核 → 故障包下发（车辆）→ 执行归档，只能顺序推进，跳级由服务端拒绝。

裁决纪律：
- 道路等级阈值规则与现场安全结论冲突时，以照明主管复核级别为准；
- 复核结论事务性回写灯具台账、维修清单、车辆路径待办，并驱动故障包重算；
- 故障包重算按「回路版本 + 证据指纹」幂等，阶段与路径落库同一事务，不符则整体回滚；
- 同一回路并发下发只允许一个版本进入执行；
- 归档后历史灭灯记录保留当时的阈值快照，不随后续规则调整而改写。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

# --- 落库表名 -------------------------------------------------------------
T_CIRCUIT = "lighting_circuit"
T_LAMP_LINK = "lighting_circuit_lamp"
T_VERSION = "lighting_circuit_version"
T_REPAIR = "lighting_repair"
T_ROUTE = "lighting_route"
T_HISTORY = "lighting_outage_history"

# --- 裁决口径（规则版本即服务口径；历史版本在归档时快照当时阈值） --------
RULESET_VERSION = "照度阈值规则-2026版"
# 灭灯数达到 critical → 一级抢修；达到 urgent → 二级抢修；其余三级
GRADE_THRESHOLDS: dict[str, dict[str, int]] = {
    "快速路": {"critical": 5, "urgent": 2, "lux_floor": 20},
    "主干路": {"critical": 5, "urgent": 2, "lux_floor": 20},
    "次干路": {"critical": 8, "urgent": 3, "lux_floor": 15},
    "支路": {"critical": 10, "urgent": 5, "lux_floor": 8},
}
DEFAULT_THRESHOLD = {"critical": 5, "urgent": 10, "lux_floor": 8}

LEVEL_ORDER = ["一级抢修", "二级抢修", "三级抢修"]
LEVEL_RANK = {level: len(LEVEL_ORDER) - index for index, level in enumerate(LEVEL_ORDER)}
LEVEL_DEADLINE = {"一级抢修": "2 小时内到场", "二级抢修": "24 小时内到场", "三级抢修": "72 小时内到场"}

DARK_STATUSES = {"不亮", "闪烁"}
FAULTY_FACILITY = "待裁决"

# 状态机阶段：索引即次序，只许前进（归档后另开新版本回到测光核验）
STAGE_ORDER = ["测光核验", "主管复核", "已复核", "执行中", "已归档"]
STAGE_METERING = "测光核验"
STAGE_REVIEWING = "主管复核"
STAGE_REVIEWED = "已复核"
STAGE_EXECUTING = "执行中"
STAGE_ARCHIVED = "已归档"


class AdjudicationError(Exception):
    """裁决被服务端拒绝：携带可读原因，接口层映射为 4xx。"""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _higher(level_a: str | None, level_b: str | None) -> str | None:
    """两级取高（一级最高）。"""
    candidates = [level for level in (level_a, level_b) if level]
    if not candidates:
        return None
    return max(candidates, key=lambda level: LEVEL_RANK[level])


def _road_rule_level(dark_count: int, grade: str) -> str:
    rule = GRADE_THRESHOLDS.get(grade, DEFAULT_THRESHOLD)
    if dark_count >= rule["critical"]:
        return "一级抢修"
    if dark_count >= rule["urgent"]:
        return "二级抢修"
    return "三级抢修"


def _threshold_snapshot(grade: str) -> dict[str, Any]:
    rule = GRADE_THRESHOLDS.get(grade, DEFAULT_THRESHOLD)
    return {
        "规则版本": RULESET_VERSION,
        "道路等级": grade,
        "一级灭灯阈值": rule["critical"],
        "二级灭灯阈值": rule["urgent"],
        "最低维持照度_lx": rule["lux_floor"],
    }


class CircuitBoardService:
    # ------------------------------------------------------------------ 读取
    def list_circuits(self) -> list[dict[str, Any]]:
        boards: list[dict[str, Any]] = []
        for circuit in store.rows(T_CIRCUIT):
            boards.append(self._board_view(dict(circuit)))
        return boards

    def get_circuit(self, circuit_no: str) -> dict[str, Any]:
        circuit = self._require_circuit(circuit_no)
        return self._board_view(circuit)

    def list_repairs(self, circuit_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_REPAIR)
        if circuit_no:
            rows = [row for row in rows if row["回路编号"] == circuit_no]
        return [dict(row) for row in rows]

    def list_routes(self, circuit_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_ROUTE)
        if circuit_no:
            rows = [row for row in rows if row["回路编号"] == circuit_no]
        return [dict(row) for row in rows]

    def list_history(self, circuit_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_HISTORY)
        if circuit_no:
            rows = [row for row in rows if row["回路编号"] == circuit_no]
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- 阶段一：测光
    def verify_metering(
        self,
        circuit_no: str,
        readings: list[dict[str, Any]],
        *,
        inspector: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """调度员核对测光证据：证据落版本、道路规则与安全规则同时试算，随后推进到主管复核。"""
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            version = self._require_version(circuit, expected_version)
            evidence = self._build_evidence(circuit, readings)
            # 同版本重复提交先按指纹判幂等：一致原样返回（即使已推进到后续阶段），
            # 不一致按冲突拒绝，避免同一版本被两套证据改写。
            if version.get("测光证据"):
                if version["测光证据"].get("证据指纹") == evidence["证据指纹"]:
                    return self._board_view(circuit)
                raise AdjudicationError(
                    "同一版本已存在测光证据且数据不一致；请由主管复核裁决，或归档后另开新版本"
                )
            if version["阶段"] != STAGE_METERING:
                raise AdjudicationError(
                    f"回路 {circuit_no} v{version['版本号']} 处于「{version['阶段']}」，"
                    "测光核验只能在裁决首阶段提交，不可回退重录"
                )

            package = self._build_package(circuit, version, evidence, reviewed_level=None)
            version["测光证据"] = evidence
            version["测光员"] = inspector
            version["测光时间"] = _now()
            version["道路规则级别"] = package["规则命中"]["道路阈值规则"]["级别"]
            version["现场安全级别"] = package["规则命中"]["现场安全规则"]["级别"]
            version["冲突"] = package["规则冲突"]
            version["故障包"] = package
            version["阶段"] = STAGE_REVIEWING
            circuit["阶段"] = STAGE_REVIEWING
            # 测光后先把试算结论（待复核）推进维修清单与杆号清单，复核只改级别不换包
            self._write_repair(circuit, version, package, status="待复核")
            self._writeback_ledger(circuit, version, package, facility="待主管复核")
            return self._board_view(circuit)

    # --------------------------------------------------------- 阶段二：主管复核
    def supervisor_review(
        self,
        circuit_no: str,
        *,
        reviewed_level: str,
        reviewer: str,
        remark: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """照明主管复核：道路等级与现场安全结论冲突时，以本接口给的级别为准。"""
        if reviewed_level not in LEVEL_ORDER:
            raise AdjudicationError(
                f"复核级别必须是：{'、'.join(LEVEL_ORDER)}", status_code=400
            )
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            version = self._require_version(circuit, expected_version)
            if version["阶段"] != STAGE_REVIEWING:
                raise AdjudicationError(
                    f"主管复核要求回路处于「{STAGE_REVIEWING}」，当前为「{version['阶段']}」，"
                    "裁决只能顺序推进，不可跳级"
                )
            evidence = version.get("测光证据")
            if not evidence:
                raise AdjudicationError("测光证据缺失，须先完成测光核验才能复核")

            package = self._build_package(circuit, version, evidence, reviewed_level=reviewed_level)
            version["故障包"] = package
            version["复核级别"] = reviewed_level
            version["复核人"] = reviewer
            version["复核时间"] = _now()
            version["复核意见"] = remark
            version["阶段"] = STAGE_REVIEWED
            circuit["阶段"] = STAGE_REVIEWED
            circuit["最近级别"] = reviewed_level
            circuit["最近根因"] = package["根因"]
            # 复核结论回写三处：灯具台账、维修清单；路径待办在下发时落库
            self._write_repair(circuit, version, package, status="待下发")
            self._writeback_ledger(circuit, version, package, facility="已复核待下发")
            return self._board_view(circuit)

    # ----------------------------------------------------- 故障包重算（幂等）
    def recompute_package(self, circuit_no: str, *, expected_version: int | None = None) -> dict[str, Any]:
        """按回路版本重算故障包。证据与复核结论不变时包指纹不变，不产生重复落库。"""
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            version = self._require_version(circuit, expected_version)
            if version["阶段"] in (STAGE_EXECUTING, STAGE_ARCHIVED):
                raise AdjudicationError(
                    f"回路已「{version['阶段']}」，故障包已锁定，重算请在归档后另开新版本"
                )
            evidence = version.get("测光证据")
            if not evidence:
                raise AdjudicationError("尚无测光证据，请先完成测光核验")
            reviewed = version.get("复核级别")
            package = self._build_package(circuit, version, evidence, reviewed_level=reviewed)
            old_package = version.get("故障包") or {}
            if old_package.get("包指纹") == package["包指纹"]:
                return {"changed": False, "package": package}
            version["故障包"] = package
            if reviewed:
                self._write_repair(circuit, version, package, status="待下发")
                self._writeback_ledger(circuit, version, package, facility="已复核待下发")
            else:
                self._write_repair(circuit, version, package, status="待复核")
                self._writeback_ledger(circuit, version, package, facility="待主管复核")
            return {"changed": True, "package": package}

    # --------------------------------------------------------- 阶段三：车辆下发
    def dispatch(
        self,
        circuit_no: str,
        *,
        vehicle_id: int,
        expected_version: int | None = None,
        operator: str = "调度员",
    ) -> dict[str, Any]:
        """故障包下发车辆：阶段与路径同一事务落库；并发只允许一个版本进入执行。"""
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            version = self._require_version(circuit, expected_version)
            # 并发下发：store 锁串行化后，第二个请求会看到执行中状态，直接拒绝
            if version["阶段"] == STAGE_EXECUTING and version.get("下发状态") == "已下发":
                raise AdjudicationError(
                    f"回路 {circuit_no} v{version['版本号']} 已下发执行，请勿重复派车"
                )
            if version["阶段"] != STAGE_REVIEWED:
                raise AdjudicationError(
                    f"车辆下发要求回路处于「{STAGE_REVIEWED}」，当前为「{version['阶段']}」，"
                    "须先测光核验、主管复核，顺序不可跳级"
                )
            vehicle = store.find("vehicle", vehicle_id)
            if vehicle is None:
                raise AdjudicationError(f"车辆 {vehicle_id} 不存在，无法承接故障包", status_code=404)

            package = version.get("故障包")
            assert package, "已复核版本必须携带故障包"
            now = _now()
            version["阶段"] = STAGE_EXECUTING
            version["下发状态"] = "已下发"
            version["车辆编号"] = vehicle.get("车辆编号")
            version["下发人"] = operator
            version["下发时间"] = now
            circuit["阶段"] = STAGE_EXECUTING

            self._write_repair(circuit, version, package, status="已下发")
            self._write_route(circuit, version, package, vehicle, now)
            self._writeback_ledger(circuit, version, package, facility="抢修中")
            vehicle["status"] = "出车作业"
            vehicle["车辆状态"] = "抢修出车"
            return self._board_view(circuit)

    # ------------------------------------------------------------- 执行归档
    def archive(self, circuit_no: str, *, expected_version: int | None = None) -> dict[str, Any]:
        """车辆作业完成后归档：冻结灭灯记录与当时阈值，随后可另开新版本。"""
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            version = self._require_version(circuit, expected_version)
            if version["阶段"] != STAGE_EXECUTING:
                raise AdjudicationError(
                    f"归档要求回路处于「{STAGE_EXECUTING}」，当前为「{version['阶段']}」"
                )
            package = version.get("故障包")
            assert package, "执行中版本必须携带故障包"
            version["阶段"] = STAGE_ARCHIVED
            circuit["阶段"] = STAGE_ARCHIVED

            self._write_repair(circuit, version, package, status="已闭环")
            route = store.find_by(
                T_ROUTE, 回路编号=circuit_no, 版本号=version["版本号"]
            )
            if route:
                route["状态"] = "已完成"
            vehicle_no = version.get("车辆编号")
            if vehicle_no:
                for vehicle in store.rows("vehicle"):
                    if vehicle.get("车辆编号") == vehicle_no:
                        vehicle["status"] = "在库"
                        vehicle["车辆状态"] = "待命"
            # 台账置为已修复，但根因、裁决版本与当时阈值快照保留
            for lamp in self._circuit_lamps(circuit_no):
                ledger = store.find("lighting", lamp["灯具id"])
                if ledger and ledger.get("status") in DARK_STATUSES:
                    ledger["status"] = "已修复"
                    ledger["pending"] = False
                    ledger["abnormal"] = False
                    ledger["设施状态"] = "已修复"
            self._write_history(circuit, version, package)
            return self._board_view(circuit)

    def open_new_version(self, circuit_no: str) -> dict[str, Any]:
        """归档回路开启下一裁决版本：版本号自增，回到测光核验重新取证。"""
        with store.transaction():
            circuit = self._require_circuit(circuit_no)
            if circuit["阶段"] != STAGE_ARCHIVED:
                raise AdjudicationError(
                    f"只有「{STAGE_ARCHIVED}」回路能开启新版本，当前为「{circuit['阶段']}」"
                )
            next_version_no = int(circuit["当前版本"]) + 1
            version = self._new_version_row(circuit, next_version_no)
            store.rows(T_VERSION).append(version)
            circuit["当前版本"] = next_version_no
            circuit["阶段"] = STAGE_METERING
            circuit["最近级别"] = None
            return self._board_view(circuit)

    # -------------------------------------------------------------- 台账透出
    def attach_circuit_view(self, ledger_row: dict[str, Any]) -> dict[str, Any]:
        """灯具列表/详情透出同一根因：回路裁决阶段、版本、级别与阈值快照。"""
        circuit_no = ledger_row.get("回路编号")
        if not circuit_no:
            return ledger_row
        circuit = store.find_by(T_CIRCUIT, 回路编号=circuit_no)
        if circuit:
            ledger_row["裁决阶段"] = circuit["阶段"]
            ledger_row["当前裁决版本"] = f"v{circuit['当前版本']}"
        return ledger_row

    # -------------------------------------------------------------- 内部装配
    def _require_circuit(self, circuit_no: str) -> dict[str, Any]:
        circuit = store.find_by(T_CIRCUIT, 回路编号=circuit_no)
        if circuit is None:
            raise AdjudicationError(f"回路 {circuit_no} 不存在", status_code=404)
        return circuit

    def _require_version(self, circuit: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
        version_no = int(expected_version or circuit["当前版本"])
        version = store.find_by(
            T_VERSION, 回路编号=circuit["回路编号"], 版本号=version_no
        )
        if version is None:
            raise AdjudicationError(
                f"回路 {circuit['回路编号']} 不存在版本 v{version_no}", status_code=404
            )
        if expected_version is not None and int(circuit["当前版本"]) != int(expected_version):
            raise AdjudicationError(
                f"版本已过期：回路当前裁决版本为 v{circuit['当前版本']}，"
                "请刷新棋盘后重算；冲突结论以主管复核为准"
            )
        return version

    def _circuit_lamps(self, circuit_no: str) -> list[dict[str, Any]]:
        return [
            dict(link)
            for link in store.rows(T_LAMP_LINK)
            if link["回路编号"] == circuit_no
        ]

    def _build_evidence(
        self, circuit: dict[str, Any], readings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not readings:
            raise AdjudicationError("测光证据为空，无法核验", status_code=400)
        grade = circuit["道路等级"]
        lux_floor = GRADE_THRESHOLDS.get(grade, DEFAULT_THRESHOLD)["lux_floor"]
        by_rod: dict[str, dict[str, Any]] = {}
        for item in readings:
            rod = str(item.get("杆号") or "").strip()
            if not rod:
                raise AdjudicationError("测光读数必须包含杆号", status_code=400)
            try:
                lux = float(item.get("照度"))
            except (TypeError, ValueError):
                raise AdjudicationError(f"杆号 {rod} 的照度读数不是数值", status_code=400)
            explicit = str(item.get("现场安全结论") or "").strip()
            safety = explicit or ("高风险" if lux < lux_floor else "正常")
            by_rod[rod] = {
                "杆号": rod,
                "照度_lx": lux,
                "现场安全结论": safety,
                "低于维持照度": lux < lux_floor,
            }
        # 证据必须覆盖回路全部在账灭灯/闪烁灯具，杜绝只测好灯就降级
        dark_rods: list[str] = []
        for link in self._circuit_lamps(circuit["回路编号"]):
            ledger = store.find("lighting", link["灯具id"])
            if ledger and ledger.get("status") in DARK_STATUSES:
                dark_rods.append(link["杆号"])
        missing = sorted(set(dark_rods) - set(by_rod))
        if missing:
            raise AdjudicationError(
                f"测光证据不完整，缺少灭灯杆号：{'、'.join(missing)}", status_code=400
            )
        digest_src = json.dumps(
            sorted((key, value["照度_lx"], value["现场安全结论"]) for key, value in by_rod.items()),
            ensure_ascii=False,
        )
        signature = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:12]
        return {
            "证据指纹": signature,
            "读数": [by_rod[rod] for rod in sorted(by_rod)],
            "取证时间": _now(),
        }

    def _build_package(
        self,
        circuit: dict[str, Any],
        version: dict[str, Any],
        evidence: dict[str, Any],
        *,
        reviewed_level: str | None,
    ) -> dict[str, Any]:
        grade = circuit["道路等级"]
        threshold = GRADE_THRESHOLDS.get(grade, DEFAULT_THRESHOLD)
        lamps = self._circuit_lamps(circuit["回路编号"])
        reading_by_rod = {item["杆号"]: item for item in evidence["读数"]}

        items: list[dict[str, Any]] = []
        dark_count = 0
        safety_level: str | None = None
        safety_hits: list[str] = []
        lux_values: list[float] = []
        for link in lamps:
            ledger = store.find("lighting", link["灯具id"])
            if not ledger or ledger.get("status") not in DARK_STATUSES:
                continue
            dark_count += 1
            reading = reading_by_rod.get(link["杆号"])
            lux = reading["照度_lx"] if reading else None
            if lux is not None:
                lux_values.append(lux)
            lamp_safety = reading["现场安全结论"] if reading else "正常"
            lamp_level = "一级抢修" if lamp_safety == "高风险" else None
            if lamp_level:
                safety_level = _higher(safety_level, lamp_level)  # type: ignore[arg-type]
                safety_hits.append(link["杆号"])
            items.append({
                "杆号": link["杆号"],
                "灯具编号": ledger.get("灯具编号"),
                "现象": ledger.get("status"),
                "照度_lx": lux,
                "安全结论": lamp_safety,
                "安全规则级别": lamp_level,
            })

        road_level = _road_rule_level(dark_count, grade)
        suggested = _higher(road_level, safety_level)
        final_level = reviewed_level or suggested
        conflict = safety_level is not None and safety_level != road_level

        # 车辆路线：级别高的在前，同级按杆号顺序
        rank = {"一级抢修": 0, "二级抢修": 1, "三级抢修": 2}
        items.sort(key=lambda item: (rank[item["安全规则级别"] or final_level], item["杆号"]))
        rods = [item["杆号"] for item in items]
        min_lux = min(lux_values) if lux_values else None
        root_cause = self._root_cause_text(grade, dark_count, rods, min_lux, threshold)

        digest_src = "|".join(
            [
                circuit["回路编号"],
                str(version["版本号"]),
                evidence["证据指纹"],
                str(dark_count),
                ",".join(rods),
                final_level or "",
            ]
        )
        return {
            "包指纹": hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:12],
            "回路编号": circuit["回路编号"],
            "版本号": version["版本号"],
            "裁决级别": final_level,
            "建议级别": suggested,
            "规则冲突": conflict,
            "规则命中": {
                "道路阈值规则": {
                    "道路等级": grade,
                    "灭灯数": dark_count,
                    "一级阈值": threshold["critical"],
                    "二级阈值": threshold["urgent"],
                    "级别": road_level,
                },
                "现场安全规则": {
                    "最低照度_lx": min_lux,
                    "高风险杆号": safety_hits,
                    "级别": safety_level,
                },
            },
            "冲突处置": "以照明主管复核级别为准" if conflict and reviewed_level else "待主管复核",
            "故障灯数": dark_count,
            "杆号清单": rods,
            "抢修时限": LEVEL_DEADLINE[final_level] if final_level else None,
            "根因": root_cause,
            "条目": items,
        }

    @staticmethod
    def _root_cause_text(
        grade: str,
        dark_count: int,
        rods: list[str],
        min_lux: float | None,
        threshold: dict[str, int],
    ) -> str:
        if not dark_count:
            return f"{grade}回路测光核验未见灭灯"
        lux_text = f"，最低照度 {min_lux:g}lx" if min_lux is not None else ""
        if dark_count >= threshold["critical"]:
            level_word = "一"
        elif dark_count >= threshold["urgent"]:
            level_word = "二"
        else:
            level_word = "三"
        return (
            f"{grade}回路根因性灭灯 {dark_count} 盏（杆号 {'、'.join(rods)}），"
            f"达{level_word}级判定口径{lux_text}"
        )

    def _write_repair(
        self,
        circuit: dict[str, Any],
        version: dict[str, Any],
        package: dict[str, Any],
        *,
        status: str,
    ) -> None:
        row = store.find_by(T_REPAIR, 回路编号=circuit["回路编号"], 版本号=version["版本号"])
        values = {
            "维修单号": f"RPR-{circuit['回路编号'][2:]}-v{version['版本号']}",
            "回路编号": circuit["回路编号"],
            "回路名称": circuit["回路名称"],
            "版本号": version["版本号"],
            "抢修级别": package["裁决级别"],
            "故障灯数": package["故障灯数"],
            "杆号清单": package["杆号清单"],
            "根因": package["根因"],
            "规则冲突": package["规则冲突"],
            "抢修时限": package["抢修时限"],
            "状态": status,
            "更新时间": _now(),
        }
        if row is None:
            row = {"id": store.next_id(T_REPAIR)}
            row.update(values)
            store.rows(T_REPAIR).append(row)
        else:
            row.update(values)

    def _write_route(
        self,
        circuit: dict[str, Any],
        version: dict[str, Any],
        package: dict[str, Any],
        vehicle: dict[str, Any],
        now: str,
    ) -> None:
        row = store.find_by(T_ROUTE, 回路编号=circuit["回路编号"], 版本号=version["版本号"])
        values = {
            "路径单号": f"ROU-{circuit['回路编号'][2:]}-v{version['版本号']}",
            "回路编号": circuit["回路编号"],
            "版本号": version["版本号"],
            "抢修级别": package["裁决级别"],
            "车辆编号": vehicle.get("车辆编号"),
            "车牌号": vehicle.get("车牌号"),
            "驾驶员": vehicle.get("驾驶员"),
            "杆号顺序": package["杆号清单"],
            "停靠点": [
                f"{item['杆号']}（{item['安全规则级别'] or package['裁决级别']}）"
                for item in package["条目"]
            ],
            "状态": "执行中",
            "下发时间": now,
        }
        if row is None:
            row = {"id": store.next_id(T_ROUTE)}
            row.update(values)
            store.rows(T_ROUTE).append(row)
        else:
            row.update(values)

    def _writeback_ledger(
        self,
        circuit: dict[str, Any],
        version: dict[str, Any],
        package: dict[str, Any],
        *,
        facility: str,
    ) -> None:
        """裁决结论回写灯具台账：同一根因影响路灯照明列表与详情。"""
        rods = set(package["杆号清单"])
        snapshot = _threshold_snapshot(circuit["道路等级"])
        for link in self._circuit_lamps(circuit["回路编号"]):
            ledger = store.find("lighting", link["灯具id"])
            if ledger is None:
                continue
            hit = link["杆号"] in rods
            ledger["裁决回路"] = circuit["回路编号"]
            ledger["裁决版本"] = f"v{version['版本号']}"
            ledger["裁决级别"] = package["裁决级别"]
            ledger["阈值快照"] = snapshot
            if hit:
                ledger["根因"] = package["根因"]
                ledger["设施状态"] = facility
                ledger["不亮原因"] = package["根因"]

    def _write_history(
        self,
        circuit: dict[str, Any],
        version: dict[str, Any],
        package: dict[str, Any],
    ) -> None:
        # 归档灭灯记录按（回路，版本）幂等保留，阈值取当时快照，不随后续规则改写
        existing = store.find_by(
            T_HISTORY, 回路编号=circuit["回路编号"], 版本号=version["版本号"]
        )
        if existing is not None:
            return
        evidence = version.get("测光证据") or {}
        store.rows(T_HISTORY).append({
            "id": store.next_id(T_HISTORY),
            "灭灯记录编号": f"OUT-{circuit['回路编号'][2:]}-v{version['版本号']}",
            "回路编号": circuit["回路编号"],
            "回路名称": circuit["回路名称"],
            "版本号": version["版本号"],
            "道路等级": circuit["道路等级"],
            "故障灯数": package["故障灯数"],
            "杆号清单": package["杆号清单"],
            "抢修级别": version.get("复核级别") or package["裁决级别"],
            "根因": package["根因"],
            "测光证据": evidence.get("读数"),
            "测光时间": version.get("测光时间"),
            "复核人": version.get("复核人"),
            "复核时间": version.get("复核时间"),
            "车辆编号": version.get("车辆编号"),
            "下发时间": version.get("下发时间"),
            "归档时间": _now(),
            "当时阈值快照": version.get("规则阈值快照"),
        })

    def _new_version_row(self, circuit: dict[str, Any], version_no: int) -> dict[str, Any]:
        return {
            "id": store.next_id(T_VERSION),
            "回路编号": circuit["回路编号"],
            "版本号": version_no,
            "阶段": STAGE_METERING,
            "测光证据": None,
            "测光员": None,
            "测光时间": None,
            "道路规则级别": None,
            "现场安全级别": None,
            "冲突": False,
            "复核级别": None,
            "复核人": None,
            "复核时间": None,
            "故障包": None,
            "下发状态": None,
            "车辆编号": None,
            "下发时间": None,
            "规则阈值快照": _threshold_snapshot(circuit["道路等级"]),
        }

    def _board_view(self, circuit: dict[str, Any]) -> dict[str, Any]:
        version = store.find_by(
            T_VERSION, 回路编号=circuit["回路编号"], 版本号=int(circuit["当前版本"])
        )
        links = self._circuit_lamps(circuit["回路编号"])
        evidence = (version or {}).get("测光证据") or {}
        reading_by_rod = {item["杆号"]: item for item in evidence.get("读数", [])}
        lamps: list[dict[str, Any]] = []
        for link in links:
            ledger = store.find("lighting", link["灯具id"]) or {}
            reading = reading_by_rod.get(link["杆号"])
            lamps.append({
                "灯具id": link["灯具id"],
                "杆号": link["杆号"],
                "灯具编号": ledger.get("灯具编号"),
                "台账状态": ledger.get("status"),
                "现象": ledger.get("status"),
                "照度_lx": reading["照度_lx"] if reading else None,
                "现场安全结论": reading["现场安全结论"] if reading else None,
                "裁决根因": ledger.get("根因"),
            })
        package = (version or {}).get("故障包")
        return {
            "回路编号": circuit["回路编号"],
            "回路名称": circuit["回路名称"],
            "所属路段": circuit["所属路段"],
            "道路等级": circuit["道路等级"],
            "当前版本": f"v{circuit['当前版本']}",
            "阶段": circuit["阶段"],
            "阶段次序": STAGE_ORDER.index(circuit["阶段"]) + 1,
            "阶段总数": len(STAGE_ORDER),
            "可执行动作": self._available_actions(circuit, version),
            "道路规则级别": (version or {}).get("道路规则级别"),
            "现场安全级别": (version or {}).get("现场安全级别"),
            "规则冲突": bool((version or {}).get("冲突")),
            "复核级别": (version or {}).get("复核级别"),
            "复核人": (version or {}).get("复核人"),
            "车辆编号": (version or {}).get("车辆编号"),
            "最近级别": circuit.get("最近级别"),
            "最近根因": circuit.get("最近根因"),
            "阈值快照": _threshold_snapshot(circuit["道路等级"]),
            "故障包": package,
            "灯具": lamps,
        }

    @staticmethod
    def _available_actions(circuit: dict[str, Any], version: dict[str, Any] | None) -> list[str]:
        if version is None:
            return []
        stage = version["阶段"]
        if stage == STAGE_METERING:
            return ["测光核验"]
        if stage == STAGE_REVIEWING:
            return ["主管复核", "故障包重算"]
        if stage == STAGE_REVIEWED:
            return ["故障包下发", "故障包重算"]
        if stage == STAGE_EXECUTING:
            return ["执行归档"]
        return ["开启新版本"]

    # -------------------------------------------------------------- 演示播种
    def bootstrap(self) -> None:
        """播种回路棋盘：一条主干路回路停在测光核验；一条支路回路 v1 已归档、v2 待测光。"""
        with store.transaction():
            if store.rows(T_VERSION):
                return
            self._seed_circuit(
                "C-BH-A", "北环大道路灯 A 回路", "北环大道 K12+000~K13+000", "主干路"
            )
            self._seed_circuit(
                "C-MY-B", "梅园路路灯 B 回路", "梅园路 K3+200~K3+900", "支路"
            )
            # C-BH-A：v1 停在测光核验，等待调度员取证
            # C-MY-B：v1 走完测光→复核→下发→归档，再开 v2 回到测光核验
            self._replay_archived_cycle("C-MY-B")

    def _seed_circuit(self, circuit_no: str, name: str, section: str, grade: str) -> None:
        rows = store.rows("lighting")
        lamps = [row for row in rows if row.get("回路编号") == circuit_no]
        store.rows(T_CIRCUIT).append({
            "回路编号": circuit_no,
            "回路名称": name,
            "所属路段": section,
            "道路等级": grade,
            "当前版本": 1,
            "阶段": STAGE_METERING,
            "最近级别": None,
            "最近根因": None,
        })
        for lamp in lamps:
            store.rows(T_LAMP_LINK).append({
                "id": store.next_id(T_LAMP_LINK),
                "回路编号": circuit_no,
                "灯具id": int(lamp["id"]),
                "杆号": lamp["杆号"],
            })
        store.rows(T_VERSION).append(self._new_version_row(
            store.find_by(T_CIRCUIT, 回路编号=circuit_no), 1
        ))

    def _replay_archived_cycle(self, circuit_no: str) -> None:
        circuit = self._require_circuit(circuit_no)
        grade = circuit["道路等级"]
        lux_floor = GRADE_THRESHOLDS.get(grade, DEFAULT_THRESHOLD)["lux_floor"]
        readings = []
        for link in self._circuit_lamps(circuit_no):
            ledger = store.find("lighting", link["灯具id"])
            if ledger and ledger.get("status") in DARK_STATUSES:
                # 支路一盏灯照度低于维持值，触发现场安全规则，与灭灯数阈值规则级别不同
                high_risk = not readings
                readings.append({
                    "杆号": link["杆号"],
                    "照度": lux_floor - 3 if high_risk else lux_floor + 6,
                    "现场安全结论": "高风险" if high_risk else "正常",
                })
        self.verify_metering(circuit_no, readings, inspector="夜班测光员", expected_version=1)
        package = self.get_circuit(circuit_no)["故障包"]
        # 主管复核：安全结论与道路阈值冲突时，以主管复核（就高一级）为准
        level = _higher(package["建议级别"], "二级抢修")
        self.supervisor_review(
            circuit_no,
            reviewed_level=level or "二级抢修",
            reviewer="照明主管",
            remark="支路临近学校出入口，按安全结论就高裁决",
            expected_version=1,
        )
        self.dispatch(circuit_no, vehicle_id=2, expected_version=1, operator="值班调度")
        self.archive(circuit_no, expected_version=1)
        self.open_new_version(circuit_no)
        # v2 新一轮：前两盏再次灭灯，等待调度员重新测光取证；
        # v1 根因作为历史灭灯记录保留，不随新版本清空
        for link in self._circuit_lamps(circuit_no)[:2]:
            ledger = store.find("lighting", link["灯具id"])
            ledger["status"] = "不亮"
            ledger["pending"] = True
            ledger["abnormal"] = True
            ledger["设施状态"] = "待裁决(v2测光核验中)"


circuit_board = CircuitBoardService()
