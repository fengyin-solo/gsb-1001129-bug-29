"""HTTP 层冒烟：用 TestClient 走真实路由，验证状态码与跳级/并发拒绝。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name} {detail}")


def post(path: str, values: dict) -> tuple[int, dict]:
    response = client.post(path, json={"values": values})
    return response.status_code, response.json()


def main() -> None:
    print("[A] 路由与只读接口")
    check("健康检查", client.get("/api/health").status_code == 200)
    r = client.get("/api/lighting/circuits")
    check("棋盘列表 200", r.status_code == 200 and r.json()["total"] == 2)
    r = client.get("/api/lighting/circuits/C-101")
    check("棋盘详情 200", r.status_code == 200 and r.json()["版本"] == 2)
    check("详情带杆号清单", r.json()["杆号清单"] == ["J-3201", "J-3202", "J-3203", "J-3204"])
    check("未知回路 404", client.get("/api/lighting/circuits/NOPE").status_code == 404)
    r = client.get("/api/lighting?circuit=C-101")
    check("台账按回路过滤", r.status_code == 200 and r.json()["total"] == 4)
    r = client.get("/api/lighting/export")
    check("导出路由不再被 /{entry_id} 吞掉", r.status_code == 200 and r.json()["total"] == 6)

    print("[B] 顺序推进 + 跳级拒绝")
    code, body = post("/api/lighting/circuits/review", {
        "回路编号": "C-101", "版本": 2, "主管裁决级别": "一级抢修", "主管复核人": "周正"})
    check("跳过测光直接复核 → 409", code == 409, body)

    code, body = post("/api/lighting/circuits/verify", {
        "回路编号": "C-101", "版本": 2, "测光读数": 8.4, "证据核对人": "王敏"})
    check("测光核验 200", code == 200, body)
    check("冲突规则返回给前端", len(body["board"]["冲突规则"]) >= 1, body["board"]["冲突规则"])

    code, body = post("/api/lighting/circuits/verify", {
        "回路编号": "C-101", "版本": 2, "测光读数": 8.4, "证据核对人": "王敏"})
    check("重复测光（已过阶段）→ 409", code == 409)

    code, body = post("/api/lighting/circuits/review", {
        "回路编号": "C-101", "版本": 2, "主管裁决级别": "特级", "主管复核人": "周正"})
    check("非法裁决级别 → 400", code == 400)

    code, body = post("/api/lighting/circuits/review", {
        "回路编号": "C-101", "版本": 2, "主管裁决级别": "一级抢修",
        "主管复核人": "周正", "主管复核意见": "学校路口按一级"})
    check("主管复核 200", code == 200 and body["board"]["最终抢修级别"] == "一级抢修")

    code, body = post("/api/lighting/circuits/merge", {"回路编号": "C-101", "版本": 2})
    check("故障包合并 200", code == 200 and body["board"]["阶段"] == "车辆下发")

    print("[C] 结论回写台账/清单/日历，级别三处一致")
    level_lamp = client.get("/api/lighting/1").json()["抢修级别"]
    level_repair = client.get("/api/lighting/repairs/all?circuit=C-101").json()["items"][0]["抢修级别"]
    level_cal = client.get("/api/lighting/calendar/all?circuit=C-101").json()["items"][0]["抢修级别"]
    check("台账级别=一级", level_lamp == "一级抢修")
    check("维修清单级别=一级", level_repair == "一级抢修")
    check("抢修日历级别=一级（三处口径统一）", level_cal == "一级抢修")

    print("[D] 过期版本与并发下发")
    code, _ = post("/api/lighting/circuits/dispatch", {"回路编号": "C-101", "版本": 99})
    check("过期版本下发 → 409", code == 409)
    code, body = post("/api/lighting/circuits/dispatch", {"回路编号": "C-101", "版本": 2})
    check("正常下发 200", code == 200 and body["board"]["阶段"] == "执行中")
    code, _ = post("/api/lighting/circuits/dispatch", {"回路编号": "C-101", "版本": 2})
    check("执行中重复下发 → 409", code == 409)
    code, _ = post("/api/lighting/circuits/open", {"回路编号": "C-101"})
    check("执行中开新版本 → 409", code == 409)
    route = client.get("/api/lighting/routes/all?circuit=C-101").json()["items"][0]
    check("路径汇总与故障包杆号一致",
          route["停靠点"] == ["J-3201", "J-3202", "J-3203", "J-3204"]
          and route["抢修级别"] == "一级抢修")

    code, _ = post("/api/lighting/circuits/complete", {"回路编号": "C-101", "版本": 2})
    check("回执闭环 200", code == 200)
    check("闭环后阶段已闭环",
          client.get("/api/lighting/circuits/C-101").json()["阶段"] == "已闭环")

    print("[E] 登记故障保留当时阈值")
    response = client.post("/api/lighting/4/actions",
                           json={"values": {"action": "登记故障", "测光读数": 14.2}})
    body = response.json()
    check("登记故障 200", response.status_code == 200 and body["ok"] is True)
    records = client.get("/api/lighting/4").json()["灭灯记录"]
    check("灭灯记录新增且保留阈值", len(records) == 2 and "当时照度阈值" in records[-1])
    check("历史首条阈值未被改写", records[0]["当时照度阈值"]["杆号清单"] == 20.0)

    print(f"\nHTTP 冒烟结果：{passed} 通过，{failed} 失败")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
