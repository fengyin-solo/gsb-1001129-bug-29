"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

裁决落库（阶段推进、维修清单、车辆路径）要求事务相符：``transaction`` 会在进入时
快照全部表，写入过程中只要抛异常就整体回滚，避免出现“阶段已推进、路径没落地”这类
半成品状态。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 进程内互斥：状态机推进、故障包重算与下发放量都要过这把锁，
        # 保证并发下发时同一回路只有一个版本能进入执行。
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """对全部表做快照的本地事务：异常时回滚，正常退出时一次性提交。"""
        with self._lock:
            snapshot = {name: [dict(row) for row in rows] for name, rows in self._tables.items()}
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按业务键（如回路编号、故障包键）定位记录。"""
        for row in self.rows(module):
            if str(row.get(field, "")) == str(value):
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def upsert(
        self,
        module: str,
        key_field: str,
        key_value: Any,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """按业务键幂等写入：故障包重算、重复下发都复用同一行，而不是重复造数据。"""
        row = self.find_by(module, key_field, key_value)
        if row is None:
            row = {"id": self.next_id(module), key_field: key_value}
            self.rows(module).append(row)
        row.update(values)
        return row

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
