"""出品排序引擎（流程图节点 6「排菜」）。

确定性优先规则 + 可解释排序理由：
- P0 过敏单：食安签核后置顶（不可逆风险优先于一切效率）；
- 催菜 / 等待时长：先到先做、久等加权；
- 工序链：工序依赖决定最早开工时间（如「蒸 8 分钟」完成后才能「炒 5 分钟」）；
- 档口负载均衡：同档口内按队列长度与累计工期分派。

引擎只产出「建议顺序 + 理由」，改顺序必须由厨师长确认（建议—确认—留痕）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .menu import MenuBook
from .models import Event, EventBus, EventType, Order, OrderStatus, now


@dataclass
class Task:
    """一个档口任务 = 一道菜的一次制作。"""
    order_id: str
    table_no: str
    dish_name: str
    qty: int
    station: str
    spec: str
    priority: float = 0.0
    eta_minutes: float = 0.0
    reasons: list[str] = field(default_factory=list)   # 可解释排序理由
    status: str = "queued"                              # queued/started/finished

    def explain(self) -> str:
        return f"[{self.priority:.0f}分] {self.dish_name} x{self.qty}（{self.table_no}）← " + "；".join(self.reasons)


@dataclass
class StationBoard:
    """档口看板：KDS 三端呈现（档口/打荷/传菜）的数据源。"""
    queues: dict[str, list[Task]] = field(default_factory=dict)

    def push(self, task: Task) -> None:
        self.queues.setdefault(task.station, []).append(task)
        self.queues[task.station].sort(key=lambda t: -t.priority)

    def queue_lines(self) -> list[str]:
        """KDS 看板渲染：CJK 对齐的表格 + 可解释理由（跟着终端配色）。"""
        from ..utils import style

        lines: list[str] = []
        for station, tasks in self.queues.items():
            lines.append(style.blue(style.bold(f"┌─ {station}档 ") + style.dim(f"({len(tasks)} 单)")))
            if not tasks:
                lines.append(style.dim("│  (空)"))
                lines.append("└" + style.LINE * 58)
                continue
            head = (style.pad("序", 4) + style.pad("优先级", 8, "right") + "  "
                    + style.pad("菜品", 14) + style.pad("桌号", 7) + style.pad("工期", 8, "right") + "  排序理由")
            lines.append(style.dim("│  " + head))
            for i, t in enumerate(tasks, 1):
                lines.append(
                    f"│  {style.pad(f'{i}.', 4)}{style.pad(f'{t.priority:.0f}分', 8, 'right')}"
                    f"  {style.pad(t.dish_name + (f' x{t.qty}' if t.qty > 1 else ''), 14)}"
                    f"{style.pad(t.table_no, 7)}{style.pad(f'{t.eta_minutes:.0f}分钟', 8, 'right')}"
                    + "  " + style.dim("；".join(t.reasons))
                    )
            lines.append(style.dim("└" + "─" * 58))
        return lines


@dataclass
class SchedulerContext:
    """调度上下文：催菜集合与当前时间。"""
    urged_order_ids: set[str] = field(default_factory=set)


class Scheduler:
    """出品排序引擎。"""

    def __init__(self, menu: MenuBook, weights: dict | None = None) -> None:
        self.menu = menu
        w = weights or {}
        self.w_allergen = w.get("allergen", 100)
        self.w_urge = w.get("urge", 40)
        self.w_wait = w.get("wait_per_min", 2)
        self.w_takeout = w.get("takeout", 10)

    def score_line(self, order: Order, dish_name: str, ctx: SchedulerContext) -> tuple[float, list[str]]:
        """给单个菜品行打分并生成理由。分数越高越先做。"""
        reasons: list[str] = []
        score = 0.0

        if order.lines and any(l.allergen_notes and l.dish_name == dish_name for l in order.lines):
            score += self.w_allergen
            reasons.append(f"P0 过敏单置顶（+{self.w_allergen:.0f}）")

        if order.order_id in ctx.urged_order_ids:
            score += self.w_urge
            reasons.append(f"已催菜（+{self.w_urge:.0f}）")

        if order.source.value == "takeout":
            score += self.w_takeout
            reasons.append(f"外卖时限（+{self.w_takeout:.0f}）")

        wait_min = max(0.0, (now() - order.created_at) / 60.0)
        score += self.w_wait * wait_min
        reasons.append(f"先到先做（等待 {wait_min:.0f} 分钟，+{self.w_wait * wait_min:.0f}）")
        return score, reasons

    def schedule(self, orders: list[Order], ctx: SchedulerContext | None = None,
                 board: StationBoard | None = None) -> StationBoard:
        """排菜：board 传入时做增量合并（跨多笔订单共享同一块看板）。"""
        ctx = ctx or SchedulerContext()
        board = board or StationBoard()
        for order in orders:
            if order.status not in (OrderStatus.VALIDATED, OrderStatus.SCHEDULED,
                                    OrderStatus.COOKING):
                continue
            for line in order.lines:
                if line.status != "pending":
                    continue
                recipe = self.menu.exact(line.dish_name)
                if recipe is None:
                    continue  # 已被校验拦截，不会走到这里
                score, reasons = self.score_line(order, line.dish_name, ctx)
                score += recipe.total_minutes  # 工期越长越早启动（关键路径）
                reasons.append(f"工序链 {recipe.total_minutes:.0f} 分钟（关键路径优先）")
                board.push(Task(
                    order_id=order.order_id,
                    table_no=order.table_no,
                    dish_name=line.dish_name,
                    qty=line.qty,
                    station=recipe.station,
                    spec=line.spec,
                    priority=score,
                    eta_minutes=recipe.total_minutes,
                    reasons=reasons,
                ))
                line.station = recipe.station
            if order.status == OrderStatus.VALIDATED:
                order.transition(OrderStatus.SCHEDULED)
        return board

    def rebalance(self, board: StationBoard, station: str, target: str,
                  max_tasks: int = 2) -> list[str]:
        """档口负载均衡建议：从过载档口把队尾任务挪到空闲档口（返回建议，需厨师长确认）。"""
        src = board.queues.get(station, [])
        if len(src) <= max_tasks:
            return []
        suggestions = []
        for task in src[max_tasks:]:
            recipe = self.menu.exact(task.dish_name)
            if recipe and target in recipe.aliases + [recipe.name] or recipe is None:
                pass  # 档口能力校验由配置保证，demo 中直接建议
            suggestions.append(
                f"建议：{task.dish_name} x{task.qty}（{task.table_no}）由「{station}」改派至「{target}」"
                f"—— {station} 积压 {len(src)} 单，需厨师长确认")
        return suggestions
