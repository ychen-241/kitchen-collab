"""HTML 报告渲染：把一次演示流程生成自包含的单文件 HTML（离线可开，可放 GitHub Pages）。

设计约束：
- 零外部依赖：不引 CDN 字体/JS，断网也能完整渲染；
- 浅色主题，跟随系统字体栈；
- 全部动态值经 html.escape 转义。
"""

from __future__ import annotations

import html as _html
import os
import time
from datetime import datetime

from .core.demo_flow import FlowResult
from .core.models import OrderStatus


def _e(text) -> str:
    return _html.escape(str(text), quote=False)


_CSS = """
:root{--bg:#f6f8fa;--card:#fff;--ink:#1f2328;--muted:#656d76;--line:#d8dee4;
--blue:#2563eb;--green:#059669;--red:#dc2626;--amber:#d97706;--cyan:#0891b2}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.65;padding:32px 16px}
.wrap{max-width:960px;margin:0 auto}
header.hero{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;
border-radius:14px;padding:28px 32px;margin-bottom:24px}
header.hero h1{font-size:26px;letter-spacing:.5px}
header.hero p{opacity:.85;margin-top:6px;font-size:14px}
.pills{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap}
.pill{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.35);
border-radius:999px;padding:3px 12px;font-size:12px}
h2{font-size:17px;margin:32px 0 12px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:4px;height:18px;background:var(--blue);border-radius:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.metric{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.metric .label{font-size:13px;color:var(--muted)}
.metric .value{font-size:26px;font-weight:700;margin:4px 0}
.metric .verdict{font-size:12.5px;border-radius:6px;padding:2px 8px;display:inline-block}
.v-ok{background:#ecfdf5;color:var(--green)}
.v-bad{background:#fef2f2;color:var(--red)}
.v-info{background:#eff6ff;color:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{background:#f0f3f6;text-align:left;padding:8px 10px;font-weight:600;color:var(--muted)}
td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
.tag{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600}
.t-ok{background:#ecfdf5;color:var(--green)}
.t-bad{background:#fef2f2;color:var(--red)}
.t-warn{background:#fffbeb;color:var(--amber)}
.t-p0{background:#fee2e2;color:var(--red)}
.t-mid{background:#fffbeb;color:var(--amber)}
.issue{color:var(--muted);font-size:12.5px;margin-top:2px}
.issue b{font-weight:600}
.station{margin-bottom:18px}
.station h3{font-size:14px;color:var(--blue);margin-bottom:8px}
.hint{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
padding:8px 12px;font-size:13.5px;margin-bottom:8px}
.ok-line{color:var(--green);font-size:13.5px;margin-bottom:6px}
.timeline{border-left:2px solid var(--line);margin-left:8px;padding-left:20px}
.tl-item{position:relative;padding-bottom:16px}
.tl-item::before{content:"";position:absolute;left:-26px;top:5px;width:10px;height:10px;
border-radius:50%;background:var(--blue);border:2px solid #fff;box-shadow:0 0 0 2px var(--blue)}
.tl-item .t{font-size:12px;color:var(--muted)}
footer{margin-top:36px;color:var(--muted);font-size:12.5px;text-align:center}
code{background:#eff1f3;border-radius:4px;padding:1px 6px;font-size:12.5px}
"""


def _prio_tag(priority: float) -> str:
    if priority >= 100:
        return f'<span class="tag t-p0">P0 · {priority:.0f}分</span>'
    if priority >= 40:
        return f'<span class="tag t-mid">{priority:.0f}分</span>'
    return f'<span class="tag" style="background:#eff6ff;color:var(--blue)">{priority:.0f}分</span>'


def render_report(system, flow: FlowResult) -> str:
    """把系统与流程快照渲染为完整 HTML 文档字符串。"""
    metrics = system.metrics
    targets = metrics.targets
    served = metrics.served_durations
    avg = sum(served) / len(served) if served else 0.0
    total = metrics.allergen_orders_total or 1
    rate = metrics.allergen_orders_passed / total * 100

    def metric_card(label: str, value: str, verdict: str, cls: str) -> str:
        return (f'<div class="metric"><div class="label">{_e(label)}</div>'
                f'<div class="value">{_e(value)}</div>'
                f'<span class="verdict {cls}">{verdict}</span></div>')

    cards = [
        metric_card("出餐时长（接单→上桌）", f"{avg:.1f} 分钟",
                    f"{'✔' if avg <= float(targets.get('serve_minutes', 99)) else '✘'} 目标 ≤ {targets.get('serve_minutes', '—')}",
                    "v-ok" if avg <= float(targets.get("serve_minutes", 99)) else "v-bad"),
        metric_card("漏菜数", str(len(metrics.missed_dishes)),
                    f"{'✔' if len(metrics.missed_dishes) <= float(targets.get('missed_per_night', 0)) else '✘'} 目标 ≤ {targets.get('missed_per_night', '—')}",
                    "v-ok" if not metrics.missed_dishes else "v-bad"),
        metric_card("催菜查询次数", str(metrics.urge_count),
                    f"目标降 {targets.get('urge_drop_pct', '—')}%+", "v-info"),
        metric_card("过敏备注传递完整率", f"{rate:.0f}%",
                    "✔ 硬性门槛 100%" if rate == 100 else "✘ 未达标", "v-ok" if rate == 100 else "v-bad"),
    ]

    # 校验表
    vrows = []
    for v in flow.validations:
        tag = '<span class="tag t-ok">✔ 通过</span>' if v["ok"] else '<span class="tag t-bad">✘ 拦截</span>'
        issues = "".join(
            f'<div class="issue"><b class="{"t-bad" if i["level"] == "blocker" else "t-warn"}" '
            f'style="color:{"var(--red)" if i["level"] == "blocker" else "var(--amber)"}">'
            f'[{_e(i["code"])}]</b> {_e(i["message"])}'
            + (f'（候选：{_e("、".join(i["candidates"]))}）' if i["candidates"] else "")
            + "</div>" for i in v["issues"]) or '<div class="issue">—</div>'
        vrows.append(f"<tr><td style='white-space:nowrap'>{_e(v['order_id'])}</td>"
                     f"<td>{_e(v['source'])}</td><td>{_e(v['table'])}</td>"
                     f"<td>{tag}{issues}</td></tr>")

    # 看板
    boards = []
    for station, tasks in flow.board.queues.items():
        rows = "".join(
            f"<tr><td style='white-space:nowrap'>{_prio_tag(t.priority)}</td>"
            f"<td>{_e(t.dish_name)}{' x%d' % t.qty if t.qty > 1 else ''}</td>"
            f"<td>{_e(t.table_no)}</td><td>{t.eta_minutes:.0f} 分钟</td>"
            f"<td class='issue'>{'；'.join(_e(r) for r in t.reasons)}</td></tr>"
            for t in tasks) or "<tr><td colspan=5 class='issue'>（空）</td></tr>"
        boards.append(
            f'<div class="station card"><h3>{_e(station)}档 · {len(tasks)} 单</h3>'
            f"<table><tr><th>优先级</th><th>菜品</th><th>桌号</th><th>工期</th><th>排序理由</th></tr>{rows}</table></div>")

    # 时间线
    hints_html = "".join('<div class="hint">' + _e(h) + "</div>" for h in flow.hints) \
        or '<div class="issue">无撞单提示</div>'
    tl = ['<div class="tl-item"><div class="t">T+0 · 点品员接单归一</div>' + hints_html + "</div>"]
    for s in flow.signings:
        tl.append(f'<div class="tl-item"><div class="t">食品安全主管 · 签核</div>'
                  f'<div class="ok-line">✔ {_e(s["order_id"])} 备注随单传递已核</div></div>')
    tl.append('<div class="tl-item"><div class="t">厨师长 · 负载均衡建议（人工确认）</div>'
              f'<div class="hint">{_e(flow.suggestion.get("text", ""))}</div>'
              f'<div class="ok-line">✔ {_e(flow.suggestion.get("confirmed", ""))}</div></div>')
    tl.append('<div class="tl-item"><div class="t">异常处置 · 催菜 + 缺料标记</div>'
              '<div class="issue">催菜加权已生效；缺料进入「建议→人工确认」队列，系统不自动改单</div></div>')
    for r in flow.receipts:
        tl.append(f'<div class="tl-item"><div class="t">传菜 · 上桌回写</div>'
                  f'<div class="ok-line">✔ {_e(r)}</div></div>')

    exceptions = "、".join(f"{_e(k)} ×{v}" for k, v in metrics.exception_count.items()) or "无"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kitchen-collab · 晚市高峰演示报告</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>后厨出品协同系统 · 晚市高峰演示报告</h1>
    <p>订单归一 → 确定性校验 → 食安签核 → 可解释排菜 → 人工确认 → 全程留痕</p>
    <div class="pills">
      <span class="pill">离线规则模式</span>
      <span class="pill">写操作：建议 → 人工确认 → 留痕</span>
      <span class="pill">过敏逻辑：BLOCK 级确定性校验</span>
    </div>
  </header>

  <h2>班次指标（口径统一版）</h2>
  <div class="metrics">{''.join(cards)}</div>

  <h2>接单与校验</h2>
  <div class="card">
    <table>
      <tr><th>订单</th><th>来源</th><th>桌号</th><th>结果 / 明细</th></tr>
      {''.join(vrows)}
    </table>
  </div>

  <h2>档口看板（可解释排序）</h2>
  {''.join(boards)}

  <h2>协同时间线</h2>
  <div class="card timeline">{''.join(tl)}</div>

  <h2>审计留痕</h2>
  <div class="card">
    <p style="font-size:13.5px">本次演示共写入 <b>{flow.audit_count}</b> 条审计记录
    （<code>data/audit_log.jsonl</code>），每条含时间戳、操作人、业务编号与改动明细，
    满足「能重建这次处理」的可重建性标准。异常拦截：{_e(exceptions)}。</p>
  </div>

  <footer>
    kitchen-collab v0.1.0 · 由 <code>python -m kitchen_collab.main report</code> 生成于
    {datetime.now().strftime('%Y-%m-%d %H:%M')} · MIT License
  </footer>
</div>
</body>
</html>"""


def write_report(system, flow: FlowResult, out_path: str) -> str:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_report(system, flow))
    return out_path
