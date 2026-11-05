"""确定性校验器（流程图节点 4「校验」）。

关键立场（来自行业分析报告核心发现二）：
- 过敏/忌口等不可逆风险做确定性校验（BLOCK），不依赖「提示」；
- 校验不通过的单不进入排菜，转人工修复队列；
- 未知菜品给出相似候选（RAG 检索），由人工选择而非自动替换。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .menu import MenuBook
from .models import Order, OrderSource


@dataclass
class ValidationIssue:
    level: str            # blocker / warning
    code: str             # UNKNOWN_DISH / BAD_SPEC / ALLERGEN_CONFLICT / BAD_TABLE / LOW_CONFIDENCE
    message: str
    candidates: list[str] = field(default_factory=list)  # 相似菜品候选（人工选择用）


@dataclass
class ValidationResult:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    need_food_safety_sign: bool = False   # 含过敏行，需食安岗签核

    @property
    def blockers(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "blocker"]


def validate(order: Order, menu: MenuBook,
             valid_tables: set[str] | None = None,
             confidence: float = 1.0,
             confidence_threshold: float = 0.6) -> ValidationResult:
    issues: list[ValidationIssue] = []
    need_sign = False

    # 1) 菜品存在性（模糊匹配给候选，不自动改）
    for line in order.lines:
        recipe = menu.exact(line.dish_name)
        if recipe is None:
            cands = list(dict.fromkeys(
                r.name for r, s in menu.search(line.dish_name, top_k=3) if s > 0.2))[:2]
            issues.append(ValidationIssue(
                "blocker", "UNKNOWN_DISH",
                f"未知菜品「{line.dish_name}」，请人工确认", candidates=cands))
            continue

        # 2) 规格合法性
        if line.spec and line.spec not in recipe.spec_options:
            issues.append(ValidationIssue(
                "warning", "BAD_SPEC",
                f"「{line.dish_name}」规格「{line.spec}」不在工艺卡内"
                f"（可选：{'/'.join(recipe.spec_options) or '无'}）"))

        # 3) 过敏冲突：确定性 BLOCK —— 菜含过敏原而顾客注明对该物过敏/忌口
        for note in line.allergen_notes:
            allergen = note.split(":", 1)[-1].strip()
            if allergen and allergen in recipe.allergens:
                need_sign = True
                issues.append(ValidationIssue(
                    "blocker", "ALLERGEN_CONFLICT",
                    f"「{line.dish_name}」含过敏原「{allergen}」，与顾客备注「{note}」冲突；"
                    "已拦截，需食安岗确认替代方案或退菜"))

    # 4) 桌号：外卖单无桌号，跳过校验
    if (valid_tables and order.source is not OrderSource.TAKEOUT
            and order.table_no not in valid_tables):
        issues.append(ValidationIssue(
            "blocker", "BAD_TABLE",
            f"桌号「{order.table_no}」无效（合法：{sorted(valid_tables)}）"))

    # 5) 解析置信度：低置信进人工修复队列，不进自动推荐样本（数据失真防线）
    if confidence < confidence_threshold:
        issues.append(ValidationIssue(
            "warning", "LOW_CONFIDENCE",
            f"解析置信度 {confidence:.2f} 低于阈值 {confidence_threshold:.2f}，建议人工复核"))

    ok = not any(i.level == "blocker" for i in issues)
    return ValidationResult(ok=ok, issues=issues, need_food_safety_sign=need_sign)
