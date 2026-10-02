"""回路棋盘状态机行为测试（仅依赖标准库）：

  python3 -m tests.test_lighting_board
"""
from __future__ import annotations

import threading

from app.services.lighting import LightingService
from app.services.lighting_board import (
    DEFAULT_THRESHOLDS,
    BoardError,
    STAGES,
    circuit_board_service,
)
from app.store import store

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def expect_conflict(name: str, call, snippet: str = "", status: int = 409) -> None:
    try:
        call()
    except BoardError as exc:
        check(f"{name}（{status}: {exc.message[:38]}…）",
              exc.status_code == status and (not snippet or snippet in exc.message),
              f"status={exc.status_code}")
        return exc
    check(name + " 应被拒绝", False)
    return None


def reset() -> None:
    store.__init__()


def main() -> None:
    print("[1] 初始棋盘：C-101 v2 处于测光核验，三套规则对同一读数给出不同级别")
    boards, total = circuit_board_service.list_boards()
    check("棋盘有 2 个回路", total == 2)
    c101 = store.find_by("lighting_circuit", "回路编号", "C-101")
    check("C-101 处于测光核验", c101["阶段"] == "测光核验")
    check("C-101 当前版本 v2", c101["版本"] == 2)

    print("[2] 跳级一律由服务端拒绝")
    expect_conflict("未测光直接复核",
                    lambda: circuit_board_service.supervisor_review(
                        {"回路编号": "C-101", "版本": 2, "主管裁决级别": "一级抢修", "主管复核人": "周正"}),
                    "跳级")
    expect_conflict("未测光直接合并",
                    lambda: circuit_board_service.merge_package({"回路编号": "C-101", "版本": 2}))
    expect_conflict("未走流程直接下发",
                    lambda: circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2}))

    print("[3] 阶段一：测光核验后，三套阈值规则结论不一致被显式标出")
    board = circuit_board_service.verify_photometry({
        "回路编号": "C-101", "版本": 2, "测光读数": 8.4, "证据核对人": "王敏",
    })
    check("阶段推进到主管复核", board["阶段"] == "主管复核")
    per = board["各源判定"]
    check("杆号清单规则判二级", per["杆号清单"] == "二级抢修", str(per))
    check("抢修日历规则判一级", per["抢修日历"] == "一级抢修", str(per))
    check("车辆路线规则判二级", per["车辆路线"] == "二级抢修", str(per))
    check("照度规则冲突被记录", any("不一致" in c for c in board["冲突规则"]))
    check("阈值版本已冻结", board["阈值版本"] == DEFAULT_THRESHOLDS)

    print("[4] 阶段二：道路等级与现场安全冲突也会标出，最终以主管复核为准")
    check("道路/安全冲突被记录",
          any("道路等级" in c for c in board["冲突规则"]), str(board["冲突规则"]))
    expect_conflict("非法级别被拒",
                    lambda: circuit_board_service.supervisor_review(
                        {"回路编号": "C-101", "版本": 2, "主管裁决级别": "特级", "主管复核人": "周正"}),
                    "级别必须是", status=400)
    board = circuit_board_service.supervisor_review({
        "回路编号": "C-101", "版本": 2,
        "主管裁决级别": "一级抢修", "主管复核人": "照明主管-周正",
        "主管复核意见": "学校路口，照度规则虽有分歧，按一级处置",
    })
    check("阶段推进到故障包合并", board["阶段"] == "故障包合并")
    check("最终级别=主管裁决", board["最终抢修级别"] == "一级抢修")

    print("[5] 阶段三：故障包幂等重算，并回写台账/维修清单/抢修日历")
    board = circuit_board_service.merge_package({"回路编号": "C-101", "版本": 2})
    pkg = board["故障包"]
    check("阶段推进到车辆下发", board["阶段"] == "车辆下发")
    check("故障包杆号=回路内 4 盏灭灯/闪烁灯",
          pkg["杆号清单"] == ["J-3201", "J-3202", "J-3203", "J-3204"], str(pkg["杆号清单"]))
    check("故障包级别=一级", pkg["建议级别"] == "一级抢修")
    from datetime import date, timedelta
    check("一级计划日期=次日", pkg["计划日期"] == (date.today() + timedelta(days=1)).isoformat())
    check("首次重算次数为 1", pkg["重算次数"] == 1)
    key = "C-101#2"
    repair = store.find_by("lighting_repair", "故障包键", key)
    check("维修清单已回写", repair is not None and repair["抢修级别"] == "一级抢修")
    cal = store.find_by("lighting_calendar", "故障包键", key)
    check("抢修日历已回写", cal is not None and cal["计划日期"] == pkg["计划日期"])
    lamp = store.find("lighting", 1)
    check("台账灯具回写级别", lamp["抢修级别"] == "一级抢修")
    check("台账保留阈值快照", lamp["裁决阈值快照"] == DEFAULT_THRESHOLDS)
    check("历史灭灯记录未被改写", len(lamp["灭灯记录"]) == 1
          and lamp["灭灯记录"][0]["当时抢修级别"] == "二级抢修"
          and lamp["灭灯记录"][0]["故障包版本"] == 1,
          str(lamp["灭灯记录"]))

    print("[6] 故障包按回路版本幂等：重复重算不造新行、不增加重算次数")
    before_rows = len(store.rows("lighting_repair"))
    board = circuit_board_service.merge_package({"回路编号": "C-101", "版本": 2})
    check("维修清单仍只有一行", len(store.rows("lighting_repair")) == before_rows)
    check("重算次数保持 1", board["故障包"]["重算次数"] == 1)

    print("[7] 阶段四：下发车辆，阶段与路径在同一事务落库")
    board = circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2})
    check("下发后版本进入执行中", board["阶段"] == "执行中", board["阶段"])
    route = store.find_by("lighting_route", "故障包键", key)
    check("车辆路径已落库", route is not None and route["路径状态"] == "执行中", str(route))
    check("路线停靠点与故障包一致", route["停靠点"] == ["J-3201", "J-3202", "J-3203", "J-3204"])
    check("维修清单联动已下发", store.find_by("lighting_repair", "故障包键", key)["维修状态"] == "已下发")
    expect_conflict("执行中重复下发被拒",
                    lambda: circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2}),
                    "重复下发")

    print("[8] 事务回滚：构造路径落库失败，阶段与台账必须一起退回")
    reset()
    circuit_board_service.verify_photometry({
        "回路编号": "C-101", "版本": 2, "测光读数": 8.4, "证据核对人": "王敏"})
    circuit_board_service.supervisor_review({
        "回路编号": "C-101", "版本": 2, "主管裁决级别": "一级抢修", "主管复核人": "周正"})
    circuit_board_service.merge_package({"回路编号": "C-101", "版本": 2})
    original_upsert = store.upsert

    def failing_upsert(module, key_field, key_value, values):
        if module == "lighting_route":
            raise RuntimeError("模拟路径库写入失败")
        return original_upsert(module, key_field, key_value, values)

    store.upsert = failing_upsert  # type: ignore[method-assign]
    try:
        try:
            circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2})
            check("下发失败应抛错", False)
        except RuntimeError:
            pass
        board = store.find_by("lighting_circuit", "回路编号", "C-101")
        check("阶段回滚到车辆下发", board["阶段"] == "车辆下发", board["阶段"])
        check("没有残留 C-101#2 路径",
              store.find_by("lighting_route", "故障包键", "C-101#2") is None)
        lamp = store.find("lighting", 1)
        check("台账作业状态未污染", lamp.get("作业状态") != "出车抢修中")
        check("维修清单仍停在待下发",
              store.find_by("lighting_repair", "故障包键", "C-101#2")["维修状态"] == "待下发")
    finally:
        store.upsert = original_upsert  # type: ignore[method-assign]

    print("[9] 并发控制：只允许一个版本进入执行，stale 版本被拒")
    circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2})
    expect_conflict("对 v1 操作被拒（过期版本）",
                    lambda: circuit_board_service.complete_version(
                        {"回路编号": "C-101", "版本": 1}),
                    "已过期")
    # 执行中才能闭环
    expect_conflict("执行期间开新版本被拒",
                    lambda: circuit_board_service.open_version({"回路编号": "C-101", "版本": 99}),
                    "只允许一个")
    circuit_board_service.complete_version({"回路编号": "C-101", "版本": 2})
    check("闭环后阶段=已闭环",
          store.find_by("lighting_circuit", "回路编号", "C-101")["阶段"] == "已闭环")
    lamps_done = [r for r in store.rows("lighting") if r.get("回路编号") == "C-101"]
    check("台账灯具全部已修复", all(r["status"] == "已修复" for r in lamps_done))
    board = circuit_board_service.open_version({
        "回路编号": "C-101", "道路等级": "主干道",
        "现场安全结论": "夜间无行人", "版本": 99})
    check("闭环后开 v3", board["版本"] == 3 and board["阶段"] == "测光核验")
    archived = store.find_by("lighting_circuit", "回路编号", "C-101")["历史版本"]
    check("v2 归档且保留当时阈值",
          len(archived) >= 1 and archived[-1]["版本"] == 2
          and archived[-1]["阈值版本"] == DEFAULT_THRESHOLDS)

    print("[10] 并发下发竞争：两个线程同时下发，只有一个成功")
    reset()
    circuit_board_service.verify_photometry({
        "回路编号": "C-101", "版本": 2, "测光读数": 8.4, "证据核对人": "王敏"})
    circuit_board_service.supervisor_review({
        "回路编号": "C-101", "版本": 2, "主管裁决级别": "一级抢修", "主管复核人": "周正"})
    circuit_board_service.merge_package({"回路编号": "C-101", "版本": 2})
    results: list[str] = []

    def worker() -> None:
        try:
            circuit_board_service.dispatch_vehicle({"回路编号": "C-101", "版本": 2})
            results.append("ok")
        except BoardError:
            results.append("reject")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("恰好一次下发成功", sorted(results) == ["ok", "reject"], str(results))
    check("只落库一条 C-101#2 路径",
          len([r for r in store.rows("lighting_route") if r["故障包键"] == "C-101#2"]) == 1)

    print("[11] 登记故障追加灭灯记录：保留当时阈值，重复登记不覆盖历史")
    reset()
    svc = LightingService()
    entry, _ = svc.run_action(4, "登记故障", {"测光读数": 14.2})
    rec = entry["灭灯记录"][-1]
    check("灭灯记录定格当时阈值", rec["当时照度阈值"] == DEFAULT_THRESHOLDS)
    check("记录级别按读数推导", rec["当时抢修级别"] in {"二级抢修", "三级抢修"}, str(rec))
    # 之后阈值改版再登记，老记录不动
    board = circuit_board_service.open_version({"回路编号": "C-102", "道路等级": "次干道"})
    circuit_board_service.verify_photometry({
        "回路编号": "C-102", "版本": board["版本"], "测光读数": 5.0, "证据核对人": "王敏",
        "阈值版本": {"杆号清单": 10.0, "抢修日历": 12.0, "车辆路线": 8.0}})
    circuit_board_service.supervisor_review({
        "回路编号": "C-102", "版本": board["版本"], "主管裁决级别": "一级抢修", "主管复核人": "周正"})
    svc.run_action(4, "登记故障", {})
    entry = store.find("lighting", 4)
    check("共三条灭灯记录（历史两条 + 新版一条）", len(entry["灭灯记录"]) == 3, str(entry["灭灯记录"]))
    check("最早记录仍是旧阈值", entry["灭灯记录"][0]["当时照度阈值"]
          == {'杆号清单': 20.0, '抢修日历': 20.0, '车辆路线': 15.0})
    check("新记录采用 v2 自定义阈值", entry["灭灯记录"][-1]["当时照度阈值"]
          == {'杆号清单': 10.0, '抢修日历': 12.0, '车辆路线': 8.0})
    check("新记录定格主管裁决级别", entry["灭灯记录"][-1]["当时抢修级别"] == "一级抢修")

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
