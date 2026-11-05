"""演示流程：把「晚市高峰」剧本从渲染中剥离，供控制台与 HTML 报告共用。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.config import load_config
from .agents import KitchenSystem
from .audit import Metrics
from .menu import MenuBook
from .models import AuditLog, Event, EventType, OrderStatus, now as _now
from .scheduler import Scheduler

# 剧本：POS 单+过敏、纸质加单撞单、套餐拆单、外卖、电话单写错菜名
SCENARIO_ORDERS: list[dict] = [
    {"source": "pos", "table_no": "T05", "guest_count": 4, "items": [
        {"dish": "清蒸鲈鱼", "qty": 1, "spec": "清淡", "notes": []},
        {"dish": "宫保鸡丁", "qty": 1, "spec": "中辣", "notes": ["过敏:花生"]},
        {"dish": "蒜蓉西兰花", "qty": 1, "spec": "", "notes": []},
    ]},
    {"source": "paper", "table_no": "T05", "items": [
        {"dish": "宫保鸡丁", "qty": 1, "spec": "中辣", "notes": []},
        {"dish": "酸辣土豆丝", "qty": 1, "spec": "微辣", "notes": []},
    ]},
    {"source": "dine_scan", "table_no": "T12", "items": [
        {"dish": "清蒸鲈鱼", "qty": 1, "spec": "", "notes": ["套餐拆单", "过敏:鱼"]},
        {"dish": "番茄蛋汤", "qty": 1, "spec": "", "notes": ["套餐拆单"]},
    ]},
    {"source": "takeout", "table_no": "WM-88", "items": [
        {"dish": "红烧肉", "qty": 1, "spec": "", "notes": []},
    ]},
    {"source": "phone", "table_no": "T07", "items": [
        {"dish": "宫保鸡丁面", "qty": 2, "spec": "", "notes": []},
    ]},
]


@dataclass
class FlowResult:
    """一次演示流程的全部结构化产物（渲染器只消费这个，不碰系统内部）。"""
    hints: list[str] = field(default_factory=list)
    validations: list[dict] = field(default_factory=list)   # {order_id, source, table, ok, issues:[{code,level,message,candidates}]}
    signings: list[dict] = field(default_factory=list)      # {order_id, ok}
    suggestion: dict = field(default_factory=dict)          # {text, confirmed}
    receipts: list[str] = field(default_factory=list)
    audit_count: int = 0
    board: "StationBoardView | None" = None                 # 后向引用由渲染层展开
    board_lines: list[str] = field(default_factory=list)    # 排菜完成时刻的看板快照（终端用）
    board_snapshot: dict = field(default_factory=dict)      # {station: [task, ...]}（报告用）


def build_system(config_path: str | None = None,
                 audit_path: str | None = "data/audit_log.jsonl") -> KitchenSystem:
    cfg = load_config(config_path)
    menu = MenuBook.from_config(cfg)
    audit = AuditLog(audit_path)
    metrics = Metrics(cfg.get("targets", {}))
    scheduler = Scheduler(menu, cfg.get("weights", {}))
    return KitchenSystem(menu, scheduler, audit, metrics,
                         valid_tables=set(cfg.get("valid_tables", [])))


def run_flow(system: KitchenSystem | None = None,
             audit_path: str | None = "data/audit_log.jsonl") -> tuple[KitchenSystem, FlowResult]:
    """执行完整剧本：接单→校验→签核→排菜→建议→异常→上桌。返回系统与结果快照。"""
    result = FlowResult()
    system = system or build_system(audit_path=audit_path)

    # 1. 接单归一
    result.hints = system.submit_raw_orders(SCENARIO_ORDERS)

    # 2. 演示用：接单时间回拨 16 分钟，模拟真实等待
    for order in system.orders.values():
        order.created_at = _now() - 16 * 60
        order.timestamps["created"] = order.created_at

    # 3. 校验快照
    for oid, v in system.validations.items():
        order = system.orders[oid]
        result.validations.append({
            "order_id": oid, "source": order.source.value, "table": order.table_no,
            "ok": v.ok,
            "issues": [{"code": i.code, "level": i.level, "message": i.message,
                        "candidates": list(i.candidates)} for i in v.issues],
        })

    # 4. 食安签核
    for oid, order in system.orders.items():
        if any(l.allergen_notes for l in order.lines):
            ok = system.food_safety_sign(oid)
            result.signings.append({"order_id": oid, "ok": ok})

    # 5. 触发排菜
    for oid, order in list(system.orders.items()):
        if order.status == OrderStatus.VALIDATED:
            system.bus.publish(Event(EventType.ORDER_VALIDATED, {"order_id": oid}))
    # 排菜完成即拍快照：后续改派/催菜/完工会实时改动看板（完工即清格），
    # 演示与报告要展示的是「排出来的板」，不是收市后的空板
    result.board_lines = list(system.board.queue_lines())
    result.board_snapshot = {st: list(ts) for st, ts in system.board.queues.items()}
    result.board = system.board

    # 6. 改派建议 + 人工确认
    sug = system.suggest_reassign(list(system.orders)[0], "宫保鸡丁", "炒锅", "备用灶")
    result.suggestion = {"text": sug.text,
                         "confirmed": system.confirm_suggestion(sug.suggestion_id)}

    # 7. 异常：催菜 + 缺料
    tid = next(t for tasks in system.board.queues.values() for t in tasks)
    system.bus.publish(Event(EventType.URGE, {"order_id": tid.order_id}))
    system.bus.publish(Event(EventType.SHORTAGE,
                             {"order_id": tid.order_id, "dish": tid.dish_name,
                              "material": "活鲈鱼售罄"}))

    # 8. 制作 + 上桌
    for station, tasks in system.board.queues.items():
        for task in list(tasks):
            system.bus.publish(Event(EventType.TASK_STARTED,
                                     {"order_id": task.order_id, "dish": task.dish_name,
                                      "station": station}))
            order = system.orders[task.order_id]
            if order.status == OrderStatus.SCHEDULED:
                order.transition(OrderStatus.COOKING)
            for line in order.lines:
                if line.dish_name == task.dish_name:
                    line.status = "cooking"
            system.bus.publish(Event(EventType.TASK_FINISHED,
                                     {"order_id": task.order_id, "dish": task.dish_name}))
    for order in list(system.orders.values()):
        if order.status == OrderStatus.READY:
            result.receipts.append(system.serve_order(order.order_id))

    result.audit_count = len(system.audit)
    return system, result
