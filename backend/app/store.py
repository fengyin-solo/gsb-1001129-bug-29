"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

回路裁决需要「阶段与路径落库事务相符」，这里提供两件设施：
- ``lock``：进程内互斥锁，兜住并发下发只允许一个版本进入执行；
- ``transaction()``：快照事务，阶段推进、故障包与路径落库任何一步失败都整体回滚。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 回路棋盘的内部落库表，不计入运营概览的「今日新增/待处理」看板口径
INTERNAL_TABLES = {
    "lighting_circuit",
    "lighting_circuit_lamp",
    "lighting_circuit_version",
    "lighting_repair",
    "lighting_route",
    "lighting_outage_history",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """快照事务：with 块内对各表的修改要么全部生效，要么全部回滚。"""
        with self._lock:
            snapshot = deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, **criteria: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if all(row.get(key) == value for key, value in criteria.items()):
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
