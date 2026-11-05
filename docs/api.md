# 接口说明

面向两类使用者：**想把原型嵌进自己流程的集成者**（Python API），和**想改引擎行为的贡献者**。

## 快速入口

```python
import sys; sys.path.insert(0, "src")

from kitchen_collab.core.menu import MenuBook
from kitchen_collab.core.models import AuditLog, Metrics
from kitchen_collab.core.scheduler import Scheduler
from kitchen_collab.core.agents import KitchenSystem
from kitchen_collab.utils.config import load_config

cfg = load_config("config/default.yaml")
menu = MenuBook.from_config(cfg)
system = KitchenSystem(
    menu=menu,
    scheduler=Scheduler(menu, cfg.get("weights")),
    audit=AuditLog("data/audit_log.jsonl"),   # 传 None 则只留内存
    metrics=Metrics(cfg.get("targets")),
    valid_tables={"T01", "T02", "T05"},        # 堂食合法桌号
)
```

## KitchenSystem（编排器）

| 方法 | 签名 | 说明 |
|---|---|---|
| `submit_raw_orders` | `(raw_list: list[dict]) -> list[str]` | 多源订单入口。每个 dict：`{source, table_no, guest_count, items:[{dish, qty, spec, notes, from_set}]}`。返回撞单/差异提示（是否并单由人工决定）。内部依次触发解析 → 校验，过敏冲突自动挂起。 |
| `food_safety_sign` | `(order_id: str, by: str = "食品安全主管") -> bool` | 食安签核。核验备注随行完整性，签核后自动放行排菜。 |
| `suggest_reassign` | `(order_id, dish, from_station, to_station) -> Suggestion` | 产出改派**建议**，不执行。 |
| `confirm_suggestion` | `(suggestion_id, by: str) -> str` | 人工确认建议，确认后才真正改看板并留痕。 |
| `serve_order` | `(order_id, by: str = "传菜") -> str` | 上桌回写：served → closed（结账归零），统计漏菜。 |
| `pending_suggestions` | 属性 `list[Suggestion]` | 当前全部待确认建议。 |
| `board` | 属性 `StationBoard` | 档口看板（KDS 三端的数据源）。 |
| `audit` / `metrics` | 属性 | 审计日志与指标看板。 |

## Order / OrderLine（models.py)

```python
order.table_no: str              # 桌号；外卖单不校验桌号
order.source: OrderSource        # pos / dine_scan / takeout / paper / phone
order.status: OrderStatus        # 状态机，非法迁移抛 TransitionError
order.timestamps: dict           # 各状态到达时间（口径埋点的唯一依据）
order.transition(OrderStatus.X)  # 唯一的迁移入口

line.dish_name / qty / spec / notes
line.allergen_notes              # 从 notes 中提取的过敏/忌口条目（食安链路专用）
line.from_set                    # 套餐拆单标记（备注逐行传递的检查依据）
```

## validate（validator.py）

```python
validate(order, menu, valid_tables=None, confidence=1.0,
         confidence_threshold=0.6) -> ValidationResult
```

- `ValidationResult.ok`：存在 blocker 即 False
- blocker 类型：`UNKNOWN_DISH`（带相似候选）、`ALLERGEN_CONFLICT`（确定性拦截）、`BAD_TABLE`
- warning 类型：`BAD_SPEC`、`LOW_CONFIDENCE`（进人工修复队列的信号）

## Scheduler（scheduler.py)

```python
Scheduler(menu, weights)   # weights: allergen / urge / wait_per_min / takeout
sched.schedule(orders, ctx, board=None)  # board 传 None 新建；传入则增量合并
sched.rebalance(board, station, target)  # 返回改派建议文本（不执行）
```

`Task.reasons` 是可解释排序理由列表，直接用于 KDS 展示。

## LLM 解析器（parser.py）

环境变量驱动，零代码接入：

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `KITCHEN_LLM_API_KEY` | 否 | 不设则全程使用规则解析 |
| `KITCHEN_LLM_BASE_URL` | 否 | OpenAI 兼容接口地址，默认官方 |
| `KITCHEN_LLM_MODEL` | 否 | 默认 gpt-4o-mini |
| `KITCHEN_LLM_MIN_CONFIDENCE` | 否 | 低置信阈值，默认 0.6 |

任何 LLM 异常（超时/格式错/配额）都自动降级规则解析，并在 `ParseResult.warnings` 留痕。

## 审计与指标

```python
audit.append(actor, action, target, detail)   # JSONL 追加写；每条含 ts/actor/action/target/detail
metrics.summary() -> str                      # 班次看板：出餐时长/漏菜/催菜/过敏传递率
```

`AuditLog` 满足「可重建」标准：给定日志片段能还原「谁、何时、对哪笔业务、
基于什么信息、做了什么、改前改后是什么」。
