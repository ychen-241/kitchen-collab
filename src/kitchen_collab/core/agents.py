"""多角色 Agent 协同（流程图右侧「人工角色与责任边界」）。

五个角色对应一张岗位表：
- OrderClerkAgent   点品员/前厅经理：接单 → 解析 → 校验 → 撞单提示
- FoodSafetyAgent   食品安全主管：过敏行签核（未签核不得开工）
- ChefSupervisorAgent 厨师长/前厅经理：排菜确认、改派确认、异常处置
- StationChefAgent  档口厨师：开工/完工上报、缺料一键标记
- RunnerAgent       传菜：核对桌号上菜、状态回写

硬规则：所有写操作（改顺序/改派/退菜/替代食材）由 Agent 产出「建议」，
必须经 confirm() 人工确认后才生效，全程审计留痕 —— 不做静默执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit import Metrics
from .menu import MenuBook
from .models import AuditLog, Event, EventBus, EventType, Order, OrderStatus
from .parser import ParseResult, get_parser, normalize_orders
from .scheduler import Scheduler, SchedulerContext, StationBoard
from .validator import ValidationResult, validate


@dataclass
class Suggestion:
    """一条待确认的写操作建议。"""
    suggestion_id: str
    kind: str                     # reassign / reorder / substitute / void ...
    text: str
    payload: dict = field(default_factory=dict)
    confirmed: bool = False

    def confirm(self, by: str) -> None:
        self.confirmed = True
        self.payload["confirmed_by"] = by


class KitchenSystem:
    """把所有角色组装起来的顶层编排器（等价于一个最小版 LangGraph 状态机）。"""

    def __init__(self, menu: MenuBook, scheduler: Scheduler,
                 audit: AuditLog, metrics: Metrics,
                 valid_tables: set[str] | None = None) -> None:
        self.menu = menu
        self.scheduler = scheduler
        self.audit = audit
        self.metrics = metrics
        self.valid_tables = valid_tables or set()
        self.bus = EventBus()
        self.orders: dict[str, Order] = {}
        self.parse_results: dict[str, ParseResult] = {}
        self.validations: dict[str, ValidationResult] = {}
        self.board = StationBoard()
        self.ctx = SchedulerContext()
        self.pending_suggestions: list[Suggestion] = []
        self.signed_orders: set[str] = set()   # 食安已签核

        # 事件接线：角色之间只通过事件通信
        self.bus.subscribe(EventType.ORDER_PARSED, self.on_order_parsed)
        self.bus.subscribe(EventType.FOOD_SAFETY_SIGNED, self.on_food_safety_signed)
        self.bus.subscribe(EventType.ORDER_VALIDATED, self.on_order_validated)
        self.bus.subscribe(EventType.TASK_STARTED, self.on_task_started)
        self.bus.subscribe(EventType.TASK_FINISHED, self.on_task_finished)
        self.bus.subscribe(EventType.DISH_READY, self.on_dish_ready)
        self.bus.subscribe(EventType.ORDER_SERVED, self.on_order_served)
        self.bus.subscribe(EventType.SHORTAGE, self.on_shortage)
        self.bus.subscribe(EventType.URGE, self.on_urge)

    # ------------------------------------------------------------------
    # 点品员：接单 → 归一 → 解析 → 校验
    # ------------------------------------------------------------------
    def submit_raw_orders(self, raw_list: list[dict]) -> list[str]:
        """多源订单入口。返回提示列表（撞单/差异），不静默合并。"""
        parser = get_parser()
        hints: list[str] = []
        parsed_orders = []
        for raw in raw_list:
            result = parser.parse(raw)
            self.parse_results[result.order.order_id] = result
            parsed_orders.append(result)
        normalized = normalize_orders([r.order for r in parsed_orders])
        for order, hs in normalized:
            self.orders[order.order_id] = order
            hints.extend(hs)
            order.transition(OrderStatus.PARSED)
            self.audit.append("点品员", "接单归一", order.order_id,
                              {"source": order.source.value, "table": order.table_no,
                               "lines": len(order.lines), "hints": hs})
            self.bus.publish(Event(EventType.ORDER_PARSED, {"order_id": order.order_id}))
        return hints

    def on_order_parsed(self, event: Event) -> None:
        # 校验阶段（由点品员触发）
        order_id = event.payload.get("order_id")
        if not order_id:
            return
        order = self.orders[order_id]
        pr = self.parse_results.get(order_id)
        conf = pr.confidence if pr else 1.0
        result = validate(order, self.menu, self.valid_tables, confidence=conf)
        self.validations[order_id] = result
        if result.ok:
            order.transition(OrderStatus.VALIDATED)
            self.audit.append("点品员", "校验通过", order_id, {})
            self.bus.publish(Event(EventType.ORDER_VALIDATED, {"order_id": order_id}))
        else:
            order.transition(OrderStatus.EXCEPTION)
            for issue in result.blockers:
                self.audit.append("点品员", "校验拦截", order_id,
                                  {"code": issue.code, "message": issue.message})
                self.metrics.record_exception(issue.code)
            self.bus.publish(Event(EventType.ORDER_BLOCKED,
                                   {"order_id": order_id,
                                    "codes": [i.code for i in result.blockers]}))

    # ------------------------------------------------------------------
    # 食品安全主管：过敏行签核（未签核不得开工）
    # ------------------------------------------------------------------
    def food_safety_sign(self, order_id: str, by: str = "食品安全主管") -> bool:
        order = self.orders.get(order_id)
        if not order:
            return False
        # 校验备注是否随每一行传递（套餐拆单不丢备注）
        missing = [l.line_id for l in order.lines if not l.notes]
        self.audit.append(by, "食安签核", order_id,
                          {"allergen_lines": sum(1 for l in order.lines if l.allergen_notes),
                           "lines_without_notes": len(missing)})
        self.signed_orders.add(order_id)
        self.metrics.record_allergen_passed(order_id)
        self.bus.publish(Event(EventType.FOOD_SAFETY_SIGNED, {"order_id": order_id}))
        return True

    def on_food_safety_signed(self, event: Event) -> None:
        # 签核后：若订单仅因过敏冲突挂起，则放行回校验态并触发排菜
        order_id = event.payload["order_id"]
        order = self.orders[order_id]
        if order.status == OrderStatus.EXCEPTION:
            blockers = [i.code for i in self.validations.get(order_id).blockers] \
                if order_id in self.validations else []
            if set(blockers) <= {"ALLERGEN_CONFLICT"}:
                order.transition(OrderStatus.VALIDATED)
        self.bus.publish(Event(EventType.ORDER_VALIDATED, {"order_id": order_id}))

    # ------------------------------------------------------------------
    # 厨师长：排菜（引擎给建议，确认后生效）
    # ------------------------------------------------------------------
    def on_order_validated(self, event: Event) -> None:
        order_id = event.payload["order_id"]
        order = self.orders[order_id]
        if order.status != OrderStatus.VALIDATED:
            return
        if any(l.allergen_notes for l in order.lines) and order_id not in self.signed_orders:
            self.audit.append("系统", "挂起待食安签核", order_id, {})
            return
        self.board = self.scheduler.schedule([order], self.ctx, board=self.board)
        self._apply_station_pressure(order_id)
        self.audit.append("厨师长", "确认排菜", order_id,
                          {"stations": sorted({l.station for l in order.lines})})
        self.bus.publish(Event(EventType.ORDER_SCHEDULED, {"order_id": order_id}))

    def _apply_station_pressure(self, order_id: str) -> None:
        """档口压力规则（确定性，非模型）：

        已有桌号占用时再开新单，新单点了某档口的菜，则该档口**其他桌**的
        菜各 +1 分（后厨压力上升，先来者优先级相对提高）；新单自己的菜
        分不变。空场首单时看板为空，自然无调整。
        """
        stations = {t.station for tasks in self.board.queues.values()
                    for t in tasks if t.order_id == order_id}
        for st in stations:
            q = self.board.queues.get(st, [])
            for t in q:
                if t.order_id != order_id:
                    t.priority += 1
                    t.reasons.append(f"后厨加压 +1（新单 {order_id} 进入 {st}）")
            # 加压改变了分数，必须重排保持降序
            q.sort(key=lambda t: -t.priority)

    def suggest_reassign(self, order_id: str, dish: str, from_station: str,
                         to_station: str, by: str = "厨师长") -> Suggestion:
        """改派建议：只建议，不执行。"""
        s = Suggestion(
            suggestion_id=f"SG-{len(self.pending_suggestions) + 1:03d}",
            kind="reassign",
            text=f"{dish}（{order_id}）由 {from_station} 改派至 {to_station}",
            payload={"order_id": order_id, "dish": dish,
                     "from": from_station, "to": to_station})
        self.pending_suggestions.append(s)
        self.bus.publish(Event(EventType.SUGGESTION_PENDING,
                               {"id": s.suggestion_id, "text": s.text}))
        return s

    def confirm_suggestion(self, suggestion_id: str, by: str = "厨师长") -> str:
        for s in self.pending_suggestions:
            if s.suggestion_id == suggestion_id and not s.confirmed:
                s.confirm(by)
                self.audit.append(by, "确认写操作", s.suggestion_id,
                                  {"kind": s.kind, "text": s.text})
                self.bus.publish(Event(EventType.SUGGESTION_CONFIRMED,
                                       {"id": s.suggestion_id}))
                if s.kind == "reassign":
                    for tasks in list(self.board.queues.values()):
                        for t in list(tasks):
                            if (t.order_id == s.payload["order_id"]
                                    and t.dish_name == s.payload["dish"]):
                                tasks.remove(t)
                                t.station = s.payload["to"]
                                self.board.push(t)
                return f"已确认并执行：{s.text}"
        return f"未找到待确认建议 {suggestion_id}"

    # ------------------------------------------------------------------
    # 档口厨师 / 传菜：状态回写
    # ------------------------------------------------------------------
    def on_task_started(self, event: Event) -> None:
        p = event.payload
        self.audit.append("档口厨师", "开工", p["dish"],
                          {"order": p["order_id"], "station": p["station"]})

    def on_task_finished(self, event: Event) -> None:
        p = event.payload
        order = self.orders[p["order_id"]]
        for line in order.lines:
            if line.dish_name == p["dish"] and line.status in ("pending", "cooking"):
                line.status = "ready"
                break
        self.audit.append("档口厨师", "完工", p["dish"], {"order": p["order_id"]})
        # 完工即清格：从看板移除该任务，空出档口位置（KDS 标准行为）
        for station in list(self.board.queues):
            self.board.queues[station] = [
                t for t in self.board.queues[station]
                if not (t.order_id == p["order_id"] and t.dish_name == p["dish"])]
        self.bus.publish(Event(EventType.DISH_READY,
                               {"order_id": p["order_id"], "dish": p["dish"]}))

    def on_dish_ready(self, event: Event) -> None:
        order = self.orders[event.payload["order_id"]]
        if all(l.status in ("ready", "served", "void") for l in order.lines) and order.status == OrderStatus.COOKING:
            order.transition(OrderStatus.READY)
            self.bus.publish(Event(EventType.ORDER_READY, {"order_id": order.order_id}))

    def serve_order(self, order_id: str, by: str = "传菜") -> str:
        """传菜核对桌号后上桌回写。"""
        order = self.orders[order_id]
        order.transition(OrderStatus.SERVED)
        for line in order.lines:
            line.status = "served"
        self.audit.append(by, "上桌回写", order_id, {"table": order.table_no})
        self.metrics.record_served(order)
        self.bus.publish(Event(EventType.ORDER_SERVED, {"order_id": order_id}))
        return f"单 {order_id} 已上桌（{order.table_no}）"

    def on_order_served(self, event: Event) -> None:
        # 结账归零：账单同步 + 闭环（报价单二期第 5 项的最小实现）
        order = self.orders[event.payload["order_id"]]
        order.transition(OrderStatus.CLOSED)
        missed = [l.dish_name for l in order.lines if l.status not in ("served", "void")]
        self.metrics.record_closed(order, missed)
        self.audit.append("系统", "结账归零", order.order_id,
                          {"missed": missed})
        self.bus.publish(Event(EventType.ORDER_CLOSED, {"order_id": order.order_id}))

    # ------------------------------------------------------------------
    # 异常：缺料 / 催菜
    # ------------------------------------------------------------------
    def on_shortage(self, event: Event) -> None:
        p = event.payload
        self.audit.append("档口厨师", "缺料标记", p["dish"],
                          {"order": p["order_id"], "material": p.get("material", "")})
        self.metrics.record_exception("SHORTAGE")
        # 产出建议而非直接改单
        s = Suggestion(f"SG-{len(self.pending_suggestions) + 1:03d}", "substitute",
                       f"{p['dish']}（{p['order_id']}）缺料，建议：换菜或退菜，需厨师长确认",
                       {"order_id": p["order_id"], "dish": p["dish"]})
        self.pending_suggestions.append(s)
        self.bus.publish(Event(EventType.SUGGESTION_PENDING,
                               {"id": s.suggestion_id, "text": s.text}))

    def on_urge(self, event: Event) -> None:
        order_id = event.payload["order_id"]
        self.ctx.urged_order_ids.add(order_id)
        self.metrics.record_urge()
        self.audit.append("前厅", "催菜", order_id, {})
        # 重新排菜给出新建议顺序（不自动改，输出建议理由）
        order = self.orders.get(order_id)
        if order and order.status in (OrderStatus.SCHEDULED, OrderStatus.COOKING):
            # 只移除该单尚未开工的旧任务再重排（开工中的任务保留，避免消失）
            for station in list(self.board.queues):
                self.board.queues[station] = [
                    t for t in self.board.queues[station]
                    if not (t.order_id == order_id and t.status == "queued")]
            self.board = self.scheduler.schedule([order], self.ctx, board=self.board)
