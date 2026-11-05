"""埋点与指标看板（报价单一期第 8 项「基线埋点与指标看板」）。

口径统一（行业分析报告断点三的解法）：
- 出餐时长：接单 → 已上桌（顾客口径，也是验收口径）；
- 漏菜：结账时仍未上桌的菜品行；
- 催菜：人工催菜事件计数；
- 过敏传递率：过敏单中备注完整随行传递的比例（目标 100%，硬性门槛）。
"""

from __future__ import annotations

from .models import Order


class Metrics:
    def __init__(self, targets: dict | None = None) -> None:
        self.targets = targets or {}
        self.served_durations: list[float] = []
        self.missed_dishes: list[str] = []
        self.urge_count = 0
        self.exception_count: dict[str, int] = {}
        self.allergen_orders_total = 0
        self.allergen_orders_passed = 0

    # -- 记录 ------------------------------------------------------------
    def record_served(self, order: Order) -> None:
        start = order.timestamps.get("created", order.created_at)
        end = order.timestamps.get("served")
        if end:
            self.served_durations.append(round((end - start) / 60.0, 1))

    def record_closed(self, order: Order, missed: list[str]) -> None:
        self.missed_dishes.extend(missed)

    def record_urge(self) -> None:
        self.urge_count += 1

    def record_exception(self, code: str) -> None:
        self.exception_count[code] = self.exception_count.get(code, 0) + 1

    def record_allergen_passed(self, order_id: str) -> None:
        self.allergen_orders_total += 1
        self.allergen_orders_passed += 1

    def record_allergen_failed(self) -> None:
        self.allergen_orders_total += 1

    # -- 输出 ------------------------------------------------------------
    def summary(self) -> str:
        from ..utils import style

        avg = sum(self.served_durations) / len(self.served_durations) if self.served_durations else 0.0
        total = self.allergen_orders_total or 1
        rate = self.allergen_orders_passed / total * 100
        target_serve = self.targets.get("serve_minutes", "—")
        target_miss = self.targets.get("missed_per_night", "—")

        def _verdict(ok: bool, text: str) -> str:
            return style.green(f"{style.OK} {text}") if ok else style.red(f"{style.NO} {text}")

        rows: list[tuple[str, str, str]] = [
            ("出餐时长(接单→上桌)", f"{avg:.1f} 分钟", _verdict(avg <= float(target_serve), f"目标 ≤ {target_serve}")),
            ("漏菜数", str(len(self.missed_dishes)), _verdict(len(self.missed_dishes) <= float(target_miss), f"目标 ≤ {target_miss}")),
            ("催菜查询次数", str(self.urge_count), style.dim(f"目标降 {self.targets.get('urge_drop_pct', '—')}%+")),
            ("过敏备注传递完整率", f"{rate:.0f}%",
             _verdict(rate == 100, "硬性门槛 100%")),
        ]

        w_name, w_val, w_tgt = 22, 10, 18
        width = w_name + w_val + w_tgt + 8
        out = [
            style.bold(style.cyan(f"┌{'─' * width}┐")),
            style.bold(style.cyan("│")) + style.bold(style.pad("班次指标看板（口径统一版）", width, "center")) + style.bold(style.cyan("│")),
            style.bold(style.cyan("│")) + style.dim("  " + style.pad("指标", w_name) + style.pad("实测", w_val, "right") + "  " + style.pad("判定", w_tgt)) + style.bold(style.cyan("│")),
            style.bold(style.cyan(f"├{'─' * width}┤")),
        ]
        for name, val, verdict in rows:
            out.append(
                style.bold(style.cyan("│"))
                + "  " + style.pad(name, w_name)
                + style.pad(val, w_val, "right") + "  "
                + style.pad(verdict, w_tgt)
                + style.bold(style.cyan("│")))
        out.append(style.bold(style.cyan(f"├{'─' * width}┤")))
        exc = self.exception_count or {}
        exc_text = "、".join(f"{k}×{v}" for k, v in exc.items()) or "无"
        # 变长行：不画右边框，避免长文本顶破面板
        out.append(style.bold(style.cyan("│")) + "  " + style.pad("异常拦截明细", w_name)
                   + style.yellow(exc_text))
        if self.missed_dishes:
            out.append(style.bold(style.cyan("│")) + "  " + style.red("漏菜明细: " + "、".join(self.missed_dishes)))
        out.append(style.bold(style.cyan(f"└{'─' * width}┘")))
        return "\n".join(out)
