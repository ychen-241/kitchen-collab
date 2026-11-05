"""交互式网页后端：标准库 http.server + 真正的 kitchen_collab 引擎。

零新增依赖（不引 Flask），浏览器只是操作界面，所有业务规则都在引擎里跑：
- GET  /                交互页面（web/index.html）
- GET  /api/state       全量状态快照（菜单/订单/看板/建议/指标/审计条数）
- POST /api/orders      下单（多源：pos/dine_scan/takeout/paper/phone）
- POST /api/sign        食安签核（含过敏行的单未签核不得排菜）
- POST /api/urge        催菜（查权重库加分 → 看板重排）
- POST /api/shortage    缺料标记（产出建议，不直接改单）
- POST /api/cook        档口开工/完工上报
- POST /api/serve       传菜上桌回写
- POST /api/suggest     厨师长改派建议（待确认）
- POST /api/confirm     人工确认建议（建议→确认→留痕）
- POST /api/reset       重置演示环境

启动：python -m kitchen_collab.main serve --port 8000
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core.agents import KitchenSystem
from .core.demo_flow import build_system
from .core.models import Event, EventType, OrderStatus, now

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "web")

_LOCK = threading.Lock()
_STATE: dict = {"system": None}


def _system() -> KitchenSystem:
    if _STATE["system"] is None:
        _STATE["system"] = build_system()
    return _STATE["system"]


def reset_system() -> None:
    _STATE["system"] = build_system()


# --------------------------------------------------------------------------
# 状态快照
# --------------------------------------------------------------------------
def _snapshot() -> dict:
    s = _system()
    orders = []
    for o in s.orders.values():
        # 已完成（已上桌/已结账/已作废）的订单自动清除，空出桌号与看板位置
        if o.status in (OrderStatus.SERVED, OrderStatus.CLOSED, OrderStatus.VOID):
            continue
        v = s.validations.get(o.order_id)
        orders.append({
            "order_id": o.order_id,
            "table_no": o.table_no,
            "source": o.source.value,
            "status": o.status.value,
            "guest_count": o.guest_count,
            "wait_min": round(max(0.0, (now() - o.created_at) / 60.0), 1),
            "signed": o.order_id in s.signed_orders,
            "needs_sign": any(l.allergen_notes for l in o.lines)
                          and o.order_id not in s.signed_orders,
            "issues": [{"code": i.code, "level": i.level, "message": i.message,
                        "candidates": list(i.candidates or [])}
                       for i in (v.issues if v else [])],
            "lines": [{"dish_name": l.dish_name, "qty": l.qty, "spec": l.spec,
                       "notes": l.notes, "station": l.station, "status": l.status,
                       "allergens": sorted(l.allergen_notes)}
                      for l in o.lines],
        })
    board = []
    for station, tasks in s.board.queues.items():
        board.append({"station": station, "tasks": [{
            "order_id": t.order_id, "table_no": t.table_no,
            "dish_name": t.dish_name, "qty": t.qty, "spec": t.spec,
            "priority": round(t.priority, 1), "eta_minutes": t.eta_minutes,
            "reasons": t.reasons, "status": t.status,
        } for t in tasks]})
    menu = [{"name": r.name, "station": r.station,
             "total_minutes": r.total_minutes,
             "allergens": sorted(r.allergens), "spec_options": r.spec_options}
            for r in s.menu.recipes]
    m = s.metrics
    avg = sum(m.served_durations) / len(m.served_durations) if m.served_durations else None
    return {
        "menu": menu,
        "orders": orders,
        "board": board,
        "suggestions": [{"id": x.suggestion_id, "kind": x.kind, "text": x.text,
                         "confirmed": x.confirmed} for x in s.pending_suggestions],
        "metrics": {
            "avg_serve_min": avg,
            "target_serve": m.targets.get("serve_minutes"),
            "missed": m.missed_dishes,
            "target_missed": m.targets.get("missed_per_night"),
            "urge_count": m.urge_count,
            "exceptions": m.exception_count,
            "allergen_rate": round(m.allergen_orders_passed /
                                   (m.allergen_orders_total or 1) * 100),
        },
        "audit_count": len(s.audit),
        "weights": {"allergen": s.scheduler.w_allergen, "urge": s.scheduler.w_urge,
                    "wait_per_min": s.scheduler.w_wait, "takeout": s.scheduler.w_takeout},
    }


# --------------------------------------------------------------------------
# 动作处理（每个返回 (http_status, payload)）
# --------------------------------------------------------------------------
def _act_order(body: dict) -> tuple[int, dict]:
    raw = {"source": body.get("source", "pos"),
           "table_no": str(body.get("table_no", "")).strip(),
           "items": body.get("items", [])}
    if not raw["table_no"]:
        return 400, {"error": "缺少桌号"}
    if not raw["items"]:
        return 400, {"error": "至少点一道菜"}
    with _LOCK:
        s = _system()
        # 桌号跟随订单：一张桌同时只有一格（进行中的订单占用桌号）
        occupied = {o.table_no for o in s.orders.values()
                    if o.status not in (OrderStatus.CLOSED, OrderStatus.VOID,
                                        OrderStatus.SERVED)}
        if raw["table_no"] in occupied:
            return 409, {"error": f"桌号 {raw['table_no']} 已有进行中的订单，"
                                  f"完成并上桌后才能重新开单"}
        hints = s.submit_raw_orders([raw])
        oid = next(iter(s.orders))          # submit 后取最新一笔
        oid = max(s.orders, key=lambda k: s.orders[k].created_at)
        v = s.validations.get(oid)
        return 200, {"order_id": oid, "hints": hints,
                     "ok": bool(v.ok if v else False),
                     "issues": [{"code": i.code, "level": i.level,
                                 "message": i.message,
                                 "candidates": list(i.candidates or [])}
                                for i in (v.issues if v else [])]}


def _act_sign(body: dict) -> tuple[int, dict]:
    oid = body.get("order_id", "")
    with _LOCK:
        ok = _system().food_safety_sign(oid)
        return (200, {"ok": ok}) if ok else (404, {"error": f"订单不存在: {oid}"})


def _act_urge(body: dict) -> tuple[int, dict]:
    oid = body.get("order_id", "")
    with _LOCK:
        s = _system()
        if oid not in s.orders:
            return 404, {"error": f"订单不存在: {oid}"}
        s.bus.publish(Event(EventType.URGE, {"order_id": oid}))
        return 200, {"ok": True, "message": "已催菜：权重库加分，看板已重排"}


def _act_shortage(body: dict) -> tuple[int, dict]:
    oid, dish = body.get("order_id", ""), body.get("dish", "")
    with _LOCK:
        s = _system()
        if oid not in s.orders:
            return 404, {"error": f"订单不存在: {oid}"}
        s.bus.publish(Event(EventType.SHORTAGE,
                            {"order_id": oid, "dish": dish, "material": body.get("material", "")}))
        return 200, {"ok": True, "message": "已标记缺料，生成建议待厨师长确认"}


def _act_cook(body: dict) -> tuple[int, dict]:
    oid, dish, action = body.get("order_id", ""), body.get("dish", ""), body.get("action", "")
    with _LOCK:
        s = _system()
        order = s.orders.get(oid)
        if order is None:
            return 404, {"error": f"订单不存在: {oid}"}
        task = next((t for tasks in s.board.queues.values() for t in tasks
                     if t.order_id == oid and t.dish_name == dish), None)
        if task is None:
            return 404, {"error": f"看板上没有 {dish}（{oid}）的任务"}
        if action == "start":
            if task.status != "queued":
                return 400, {"error": f"{dish} 已在制作中或已完成"}
            task.status = "started"
            if order.status == OrderStatus.SCHEDULED:
                order.transition(OrderStatus.COOKING)
            for line in order.lines:
                if line.dish_name == dish and line.status == "pending":
                    line.status = "cooking"
            s.bus.publish(Event(EventType.TASK_STARTED,
                                {"order_id": oid, "dish": dish, "station": task.station}))
            return 200, {"ok": True, "message": f"{dish} 开工（{task.station}）"}
        elif action == "finish":
            if task.status == "finished":
                return 400, {"error": f"{dish} 已完成"}
            task.status = "finished"
            s.bus.publish(Event(EventType.TASK_FINISHED,
                                {"order_id": oid, "dish": dish}))
            all_done = all(l.status in ("ready", "served", "void") for l in order.lines)
            if not all_done:
                return 200, {"ok": True, "all_ready": False,
                             "message": f"{dish} 完工"}
            # 整单出齐：自动传菜上桌回写（订单结账、桌号释放、看板清空）
            if order.status == OrderStatus.SCHEDULED:
                order.transition(OrderStatus.COOKING)
            if order.status == OrderStatus.COOKING:
                order.transition(OrderStatus.READY)
            s.serve_order(oid)
            return 200, {"ok": True, "all_ready": True,
                         "message": f"「{order.table_no}」全部出齐，已自动传菜上桌结账，桌号已释放"}
        return 400, {"error": "action 必须是 start 或 finish"}


def _act_serve(body: dict) -> tuple[int, dict]:
    oid = body.get("order_id", "")
    with _LOCK:
        s = _system()
        order = s.orders.get(oid)
        if order is None:
            return 404, {"error": f"订单不存在: {oid}"}
        if order.status != OrderStatus.READY:
            return 400, {"error": "该单还有菜品未出品，不能上桌"}
        msg = s.serve_order(oid)
        return 200, {"ok": True, "message": msg}


def _act_suggest(body: dict) -> tuple[int, dict]:
    with _LOCK:
        s = _system()
        sug = s.suggest_reassign(body.get("order_id", ""), body.get("dish", ""),
                                 body.get("from", ""), body.get("to", ""))
        return 200, {"ok": True, "suggestion_id": sug.suggestion_id, "text": sug.text}


def _act_confirm(body: dict) -> tuple[int, dict]:
    with _LOCK:
        msg = _system().confirm_suggestion(body.get("suggestion_id", ""))
        return 200, {"ok": "已确认" in msg, "message": msg}


_ACTIONS = {
    "orders": _act_order, "sign": _act_sign, "urge": _act_urge,
    "shortage": _act_shortage, "cook": _act_cook, "serve": _act_serve,
    "suggest": _act_suggest, "confirm": _act_confirm,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "kitchen-collab/0.1"

    def _send(self, code: int, payload: dict | bytes, ctype: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(_WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, {"error": "web/index.html 缺失"})
        elif self.path == "/api/state":
            with _LOCK:
                self._send(200, _snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/reset":
            with _LOCK:
                reset_system()
            self._send(200, {"ok": True, "message": "已重置演示环境"})
            return
        action = _ACTIONS.get(self.path.removeprefix("/api/"))
        if action is None:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            code, payload = action(body)
            self._send(code, payload)
        except Exception as exc:  # 引擎异常也以 JSON 返回，前端可读
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        pass


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"kitchen-collab 交互系统已启动: http://{host}:{port}")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
