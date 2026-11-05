"""程序入口：演示与批处理两种运行方式。

用法：
    python -m kitchen_collab.main demo           # 内置晚市高峰演示
    python -m kitchen_collab.main run <orders.json>   # 从 JSON 文件读取订单跑一遍
    python -m kitchen_collab.main board          # 只打印当前档口看板（demo 数据）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .core.agents import KitchenSystem
from .core.audit import Metrics
from .core.menu import MenuBook
from .core.models import AuditLog, OrderStatus
from .core.scheduler import Scheduler
from .utils.config import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_system(config_path: str | None = None) -> KitchenSystem:
    cfg = load_config(config_path)
    menu = MenuBook.from_config(cfg)
    audit = AuditLog(os.path.join(ROOT, "data", "audit_log.jsonl"))
    metrics = Metrics(cfg.get("targets", {}))
    scheduler = Scheduler(menu, cfg.get("weights", {}))
    return KitchenSystem(menu, scheduler, audit, metrics,
                         valid_tables=set(cfg.get("valid_tables", [])))


def cook_through(system: KitchenSystem) -> list[str]:
    """模拟档口把所有已排菜任务做完、传菜上桌（演示用）。返回传菜回执。"""
    from .core.models import Event, EventType
    receipts: list[str] = []

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
            receipts.append(system.serve_order(order.order_id))
    return receipts


def order_status(system: KitchenSystem, order_id: str) -> OrderStatus:
    return system.orders[order_id].status


def run_demo() -> None:
    """内置演示：还原「纸质加单 + 过敏套餐 + 缺料 + 催菜」的晚市高峰（控制台渲染）。"""
    from .core.demo_flow import run_flow
    from .utils import style

    def banner() -> str:
        w = 56
        lines = [
            style.bold(style.cyan("┌" + "─" * w + "┐")),
            style.bold(style.cyan("│")) + style.bold(style.pad("后厨出品协同系统 · 晚市高峰演示", w, "center")) + style.bold(style.cyan("│")),
            style.bold(style.cyan("│")) + style.dim(style.pad("离线规则模式 · 建议→人工确认→留痕 · 不做静默执行", w, "center")) + style.bold(style.cyan("│")),
            style.bold(style.cyan("└" + "─" * w + "┘")),
        ]
        return "\n".join(lines)

    print(banner())
    system, flow = run_flow()

    def section(title: str) -> None:
        print("\n" + style.bold(style.cyan(f"▸ {title}")))

    section("点品员 · 接单归一，撞单/差异提示：")
    for h in flow.hints or [style.dim("（无）")]:
        print(f"  {style.DOT} {h}")

    section("校验结果：")
    for v in flow.validations:
        flag = style.green(f"{style.OK} 通过") if v["ok"] else style.red(f"{style.NO} 拦截")
        print(f"  {flag} {style.dim(v['order_id'])}（{v['source']} / {v['table']}）")
        for issue in v["issues"]:
            cand = f"，候选：{'/'.join(issue['candidates'])}" if issue["candidates"] else ""
            mark = style.red(f"[{issue['code']}]") if issue["level"] == "blocker" else style.yellow(f"[{issue['code']}]")
            print(f"     {mark} {issue['message']}{style.dim(cand)}")

    section("食品安全主管 · 过敏单签核：")
    for s in flow.signings:
        mark = style.green(f"{style.SIGN} 签核通过") if s["ok"] else style.red(f"{style.NO} 签核失败")
        print(f"  {mark} {style.dim(s['order_id'])}（备注随单传递已核）")

    section("厨师长 · 档口看板（可解释排序）：")
    print("\n".join("  " + l for l in flow.board_lines))

    section("厨师长 · 负载均衡建议（待确认，不自动执行）：")
    print(f"  {style.DOT} {flow.suggestion['text']}")
    print(f"  {style.GEAR} {style.green(flow.suggestion['confirmed'])}")

    section("传菜 · 上桌回写：")
    print("  " + ("；".join(style.green(r) for r in flow.receipts) or style.dim("（无可上桌订单）")))

    print("\n" + system.metrics.summary())
    print(style.dim(f"\n{style.DOT} 审计留痕 {flow.audit_count} 条，已写入 data/audit_log.jsonl（可重建处理链）"))
    print(style.dim(f"{style.DOT} 生成 HTML 报告：python -m kitchen_collab.main report"))
    print(style.dim(f"{style.DOT} 配置 KITCHEN_LLM_API_KEY 后重启，自由文本订单将走 LLM 解析"))


def run_from_file(path: str) -> None:
    system = build_system()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    orders = raw["orders"] if isinstance(raw, dict) else raw
    hints = system.submit_raw_orders(orders)
    for h in hints:
        print("提示:", h)
    from .core.models import Event, EventType
    for oid, order in list(system.orders.items()):
        if any(l.allergen_notes for l in order.lines):
            system.food_safety_sign(oid)
        if order.status.value == "validated":
            system.bus.publish(Event(EventType.ORDER_VALIDATED, {"order_id": oid}))
    cook_through(system)
    print(system.metrics.summary())


def run_report(out_path: str) -> None:
    """跑一遍演示流程并生成自包含 HTML 报告。"""
    from .core.demo_flow import run_flow
    from .report import write_report
    system, flow = run_flow(audit_path=None)   # 报告模式不落审计文件
    path = write_report(system, flow, out_path)
    print(f"✔ HTML 报告已生成：{path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="kitchen-collab 后厨出品协同系统原型")
    ap.add_argument("command", choices=["demo", "run", "board", "report", "serve"],
                    help="demo=内置演示; run=读JSON订单; board=打印看板; report=生成HTML报告; serve=启动交互网页")
    ap.add_argument("file", nargs="?", help="run 命令的订单 JSON 文件；report 命令的输出路径（默认 data/report.html）")
    ap.add_argument("--config", default=None, help="配置文件路径（YAML/JSON）")
    args = ap.parse_args()

    if args.command == "demo":
        run_demo()
    elif args.command == "serve":
        from .webapp import serve
        port = int(args.file) if args.file and args.file.isdigit() else 8000
        serve(port=port)
    elif args.command == "report":
        run_report(args.file or os.path.join(ROOT, "data", "report.html"))
    elif args.command == "run":
        if not args.file:
            sys.exit("run 命令需要提供订单 JSON 文件路径")
        run_from_file(args.file)
    else:
        system = build_system(args.config)
        print("\n".join(system.board.queue_lines() or ["（看板为空，先跑 demo）"]))


if __name__ == "__main__":
    main()
