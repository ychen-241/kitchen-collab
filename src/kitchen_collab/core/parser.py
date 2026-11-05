"""多源订单解析与归一。

流程图节点 1-2（订单接入 / 订单归一）：
- 堂食扫码、POS、外卖、电话/纸质补录统一进入；
- LLM 做自然语言→结构化解析（可选，配置 API 后启用）；
- 未配置或调用失败时回退到规则解析器，保证离线可用（兜底设计：系统不可用时业务不能停）；
- 归一阶段做撞单/差异提示（同一桌号多源并单、桌号不一致等）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field

from .models import Order, OrderLine, OrderSource


@dataclass
class ParseResult:
    order: Order
    confidence: float          # 0-1，低于阈值进人工修复队列，不进自动推荐样本
    method: str                # llm / rule
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 规则解析器（默认，零依赖、离线可用）
# ---------------------------------------------------------------------------

_QTY_PAT = re.compile(r"(?:[xX×*]\s*)?(\d+)\s*(?:份|个|份量)?")
_SPEC_WORDS = ("微辣", "中辣", "特辣", "不辣", "免葱", "免蒜", "免香菜",
               "少盐", "清淡", "热", "冷")
_ALLERGEN_PAT = re.compile(r"(过敏|忌)[：:]?\s*([\u4e00-\u9fa5A-Za-z]+)")


def parse_line_text(text: str, from_set: bool = False) -> OrderLine:
    """解析单行菜品文本，如「宫保鸡丁 x2 中辣 过敏:花生」。"""
    line = OrderLine(dish_name=text.strip(), from_set=from_set)

    # 过敏/忌口备注（必须随行保留 —— 拆单不丢备注）
    for m in _ALLERGEN_PAT.finditer(text):
        line.notes.append(f"过敏:{m.group(2)}")

    # 规格
    for w in _SPEC_WORDS:
        if w in text:
            line.spec = w
            break

    # 数量
    m = _QTY_PAT.search(text)
    if m:
        line.qty = max(1, int(m.group(1)))

    # 菜名 = 去掉数量/规格/过敏片段后的剩余部分
    cleaned = text
    cleaned = _ALLERGEN_PAT.sub("", cleaned)
    cleaned = re.sub(r"(?:[xX×*]\s*)?\d+\s*(?:份|个)?", "", cleaned)
    for w in _SPEC_WORDS:
        cleaned = cleaned.replace(w, "")
    line.dish_name = cleaned.strip(" ，,。;；") or text.strip()
    return line


class RuleParser:
    """规则解析：按分隔符切行。适合 POS/补录单等结构化程度较高的来源。"""

    name = "rule"

    def parse(self, raw: dict) -> ParseResult:
        source = OrderSource(raw.get("source", "pos"))
        order = Order(
            table_no=str(raw.get("table_no", "")),
            source=source,
            guest_count=int(raw.get("guest_count", 0)),
            remark=raw.get("remark", ""),
        )
        warnings: list[str] = []
        for item in raw.get("items", []):
            if isinstance(item, dict):
                line = OrderLine(
                    dish_name=item.get("dish", item.get("dish_name", "")),
                    qty=int(item.get("qty", 1)),
                    spec=item.get("spec", ""),
                    notes=list(item.get("notes", [])),
                    from_set=bool(item.get("from_set", False)),
                )
            else:
                line = parse_line_text(str(item))
            if not line.dish_name:
                warnings.append(f"空菜品行已丢弃: {item!r}")
                continue
            order.lines.append(line)

        # 置信度：结构化 dict 行高于自由文本行
        conf = 0.95 if all(isinstance(i, dict) for i in raw.get("items", [])) else 0.75
        return ParseResult(order=order, confidence=conf, method="rule", warnings=warnings)


# ---------------------------------------------------------------------------
# LLM 解析器（可选；需要 OpenAI 兼容接口）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是餐饮订单结构化助手。把用户输入的订单文本解析为 JSON："
    '{"table_no": str, "items": [{"dish": str, "qty": int, "spec": str, '
    '"notes": [str], "from_set": bool}]}。'
    "过敏、忌口信息必须完整保留在 notes 中，一字不漏。只输出 JSON。"
)


class LLMParser:
    """OpenAI 兼容接口解析器。环境变量：KITCHEN_LLM_BASE_URL / KITCHEN_LLM_API_KEY / KITCHEN_LLM_MODEL。"""

    name = "llm"
    CONFIDENCE_THRESHOLD = float(os.environ.get("KITCHEN_LLM_MIN_CONFIDENCE", "0.6"))

    def __init__(self) -> None:
        self.base_url = os.environ.get("KITCHEN_LLM_BASE_URL", "https://api.openai.com/v1")
        self.api_key = os.environ.get("KITCHEN_LLM_API_KEY", "")
        self.model = os.environ.get("KITCHEN_LLM_MODEL", "gpt-4o-mini")

    def available(self) -> bool:
        return bool(self.api_key)

    def _chat(self, text: str) -> dict:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                         {"role": "user", "content": text}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return json.loads(data["choices"][0]["message"]["content"])

    def parse(self, raw: dict) -> ParseResult:
        if not self.available():
            return RuleParser().parse(raw)
        try:
            text = raw.get("text") or json.dumps(raw, ensure_ascii=False)
            parsed = self._chat(text)
            parsed.setdefault("table_no", raw.get("table_no", ""))
            parsed.setdefault("source", raw.get("source", "pos"))
            result = RuleParser().parse(parsed)
            result.method = "llm"
            result.confidence = 0.9
            return result
        except Exception as exc:  # 任何异常都降级到规则解析（兜底设计）
            out = RuleParser().parse(raw)
            out.warnings.append(f"LLM 解析失败已降级: {exc}")
            out.confidence = min(out.confidence, 0.5)
            return out


def get_parser() -> LLMParser | RuleParser:
    """按环境自动选择解析器：配置了 API Key 用 LLM，否则规则回退。"""
    llm = LLMParser()
    return llm if llm.available() else RuleParser()


# ---------------------------------------------------------------------------
# 订单归一：多源订单的合并与撞单差异提示
# ---------------------------------------------------------------------------

def normalize_orders(orders: list[Order]) -> list[tuple[Order, list[str]]]:
    """归并同一桌号的多源订单，生成差异提示（不静默合并过敏信息）。

    返回 [(订单, 差异提示列表)]。撞单只做提示 + 留痕，是否并单由人工确认 —— 写操作闸门。
    """
    out: list[tuple[Order, list[str]]] = []
    index: dict[str, Order] = {}
    for order in orders:
        hints: list[str] = []
        base = index.get(order.table_no)
        if base is None:
            index[order.table_no] = order
            out.append((order, hints))
            continue
        # 撞单：不合并，仅提示差异，由点品员人工确认
        dup = {l.dish_name for l in base.lines} & {l.dish_name for l in order.lines}
        if dup:
            hints.append(f"与单 {base.order_id} 撞单：{'、'.join(sorted(dup))} 重复，需人工确认是否并单")
        if base.source != order.source:
            hints.append(f"多来源（{base.source.value} + {order.source.value}），注意桌号/时长差异")
        out.append((order, hints))
    return out
