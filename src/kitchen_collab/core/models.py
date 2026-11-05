"""核心数据模型：订单、菜品行、状态机、事件总线与审计留痕。

设计原则（来自行业分析报告）：
- 业务状态统一视图：多源订单归一后共用同一套模型与状态机；
- 可重建性：每次处理保留业务编号、操作人、改动前后值，审计时能重建处理链；
- 口径统一：出餐时长、漏菜、催菜、过敏传递率共用一套计量定义。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any, Optional


def new_id(prefix: str) -> str:
    """生成业务编号（审计重建的最小单元）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def now() -> float:
    return time.time()


class OrderSource(str, Enum):
    """单据来源：纸质单与系统单并存是后厨排序冲突的根源（断点一）。"""

    POS = "pos"            # 收银系统单
    DINE_SCAN = "dine_scan"  # 堂食扫码单
    TAKEOUT = "takeout"      # 外卖单
    PAPER = "paper"          # 纸质加单（补录）
    PHONE = "phone"          # 电话单（补录）


class OrderStatus(str, Enum):
    """订单主状态机。口径统一：只有 SERVED 之后才计入「已出齐」。"""

    CREATED = "created"        # 已接单
    PARSED = "parsed"          # 已结构化解析
    VALIDATED = "validated"    # 已通过确定性校验
    SCHEDULED = "scheduled"    # 已排菜
    COOKING = "cooking"        # 制作中
    READY = "ready"            # 已出品（厨房口径）
    SERVED = "served"          # 已上桌（前厅口径）
    CLOSED = "closed"          # 已结账归零
    EXCEPTION = "exception"    # 异常挂起（转人工）
    VOID = "void"              # 已作废


# 合法状态迁移表：状态机是「口径统一」的技术载体
LEGAL_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.PARSED, OrderStatus.VOID},
    OrderStatus.PARSED: {OrderStatus.VALIDATED, OrderStatus.EXCEPTION, OrderStatus.VOID},
    OrderStatus.VALIDATED: {OrderStatus.SCHEDULED, OrderStatus.EXCEPTION, OrderStatus.VOID},
    OrderStatus.SCHEDULED: {OrderStatus.COOKING, OrderStatus.EXCEPTION, OrderStatus.VOID},
    OrderStatus.COOKING: {OrderStatus.READY, OrderStatus.EXCEPTION, OrderStatus.VOID},
    OrderStatus.READY: {OrderStatus.SERVED, OrderStatus.EXCEPTION},
    OrderStatus.SERVED: {OrderStatus.CLOSED},
    OrderStatus.EXCEPTION: {OrderStatus.VALIDATED, OrderStatus.SCHEDULED, OrderStatus.VOID},
    OrderStatus.VOID: set(),
    OrderStatus.CLOSED: set(),
}


class TransitionError(Exception):
    """非法状态迁移。"""


@dataclass
class OrderLine:
    """订单中的单个菜品行。套餐拆单后每行都必须携带完整备注（断点二）。"""

    dish_name: str
    qty: int = 1
    spec: str = ""                      # 规格/口味，如「中辣」「不要香菜」
    notes: list[str] = field(default_factory=list)  # 备注列表（含过敏信息）
    from_set: bool = False              # 是否来自套餐拆单
    status: str = "pending"             # pending/cooking/ready/served/short/void
    station: str = ""                   # 分配到的档口
    line_id: str = field(default_factory=lambda: new_id("LN"))

    @property
    def allergen_notes(self) -> list[str]:
        """从备注中提取的过敏相关条目（食安链路专用，避免散落在自由文本里）。"""
        return [n for n in self.notes if "过敏" in n or "忌口" in n]


@dataclass
class Order:
    """一笔业务订单。所有来源的订单最终归一为同一模型。"""

    table_no: str
    source: OrderSource
    lines: list[OrderLine] = field(default_factory=list)
    order_id: str = field(default_factory=lambda: new_id("SO"))
    status: OrderStatus = OrderStatus.CREATED
    created_at: float = field(default_factory=now)
    timestamps: dict[str, float] = field(default_factory=dict)  # 各状态到达时间（口径埋点）
    guest_count: int = 0
    remark: str = ""

    def transition(self, target: OrderStatus) -> None:
        if target not in LEGAL_TRANSITIONS[self.status]:
            raise TransitionError(f"非法状态迁移: {self.status.value} -> {target.value}")
        self.status = target
        self.timestamps[target.value] = now()


class EventType(str, Enum):
    """系统事件类型（事件驱动协同的骨架）。"""

    ORDER_SUBMITTED = "order_submitted"        # 原始单进入（多源）
    ORDER_PARSED = "order_parsed"
    ORDER_VALIDATED = "order_validated"
    ORDER_BLOCKED = "order_blocked"            # 校验拦截（含过敏冲突）→ 转人工
    FOOD_SAFETY_SIGNED = "food_safety_signed"  # 食安签核完成
    ORDER_SCHEDULED = "order_scheduled"        # 排菜完成
    TASK_STARTED = "task_started"
    TASK_FINISHED = "task_finished"
    DISH_READY = "dish_ready"
    ORDER_READY = "order_ready"
    ORDER_SERVED = "order_served"
    ORDER_CLOSED = "order_closed"
    SHORTAGE = "shortage"                      # 缺料
    URGE = "urge"                              # 催菜
    RETURN_DISH = "return_dish"                # 退菜
    SUGGESTION_PENDING = "suggestion_pending"  # 建议待人工确认（写操作闸门）
    SUGGESTION_CONFIRMED = "suggestion_confirmed"


@dataclass
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=now)
    event_id: str = field(default_factory=lambda: new_id("EV"))


class EventBus:
    """极简进程内事件总线：Agent 之间不直接调用，只通过事件协同。"""

    def __init__(self) -> None:
        self._subs: dict[EventType, list[Callable[[Event], None]]] = {}

    def subscribe(self, etype: EventType, handler: Callable[[Event], None]) -> None:
        self._subs.setdefault(etype, []).append(handler)

    def publish(self, event: Event) -> None:
        for handler in self._subs.get(event.type, []):
            handler(event)


class AuditLog:
    """审计留痕：JSONL 追加写。

    每条记录包含：业务编号、操作人、动作、改动前后值、时间戳。
    满足「审计时能重建这次处理」的可重建性标准，而不只是「有日志」。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []

    def append(self, actor: str, action: str, target: str,
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
        rec = {
            "ts": round(now(), 3),
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail or {},
        }
        self.records.append(rec)
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def __len__(self) -> int:
        return len(self.records)
