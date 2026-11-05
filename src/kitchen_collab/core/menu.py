"""菜品库 / 工艺卡：档口、工序链、过敏原与规格。

相当于报价单二期「知识库与 RAG」的最小实现：
按别名 + 字符相似度检索工艺卡，检索结果只作为解析与排菜的参考依据，
过敏原库的准确性由食品安全负责人维护（系统只做传递与校验，不做判断）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    """单道工序：如「蒸 8 分钟 → 改刀 → 摆盘」。工序链决定排菜的最早可开工时间。"""

    name: str
    minutes: float


@dataclass
class Recipe:
    """一道菜的工艺卡。"""

    name: str
    station: str                     # 归属档口：蒸/炒/凉菜/汤/主食
    steps: list[Step] = field(default_factory=list)
    allergens: set[str] = field(default_factory=set)   # 含有的过敏原
    spec_options: list[str] = field(default_factory=list)  # 合法规格（口味）
    aliases: list[str] = field(default_factory=list)

    @property
    def total_minutes(self) -> float:
        return sum(s.minutes for s in self.steps)

    def matches(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        return text == self.name or text in self.aliases


class MenuBook:
    """菜品库，支持按名称/别名/相似度检索（最小版 RAG：字面 n-gram 相似度）。"""

    def __init__(self, recipes: list[Recipe]) -> None:
        self.recipes = recipes

    @classmethod
    def from_config(cls, cfg: dict) -> "MenuBook":
        recipes = []
        for item in cfg.get("menu", []):
            recipes.append(Recipe(
                name=item["name"],
                station=item["station"],
                steps=[Step(**s) for s in item.get("steps", [])],
                allergens=set(item.get("allergens", [])),
                spec_options=list(item.get("spec_options", [])),
                aliases=list(item.get("aliases", [])),
            ))
        return cls(recipes)

    def exact(self, name: str) -> Recipe | None:
        for r in self.recipes:
            if r.matches(name):
                return r
        return None

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """字符 bigram Dice 相似度，纯标准库实现。"""
        if len(a) < 2 or len(b) < 2:
            return 1.0 if a == b else 0.0
        ga = {a[i:i + 2] for i in range(len(a) - 1)}
        gb = {b[i:i + 2] for i in range(len(b) - 1)}
        if not ga or not gb:
            return 0.0
        return 2 * len(ga & gb) / (len(ga) + len(gb))

    def search(self, query: str, top_k: int = 3) -> list[tuple[Recipe, float]]:
        """按相似度返回候选工艺卡，供解析纠错与人工修复队列使用。"""
        scored = [(r, self._similarity(query, r.name)) for r in self.recipes]
        for r in self.recipes:
            for alias in r.aliases:
                scored.append((r, self._similarity(query, alias) * 0.95))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def stations(self) -> list[str]:
        return sorted({r.station for r in self.recipes})
