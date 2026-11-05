"""规则解析器与订单归一的测试（unittest 风格，兼容 pytest 运行）。"""

import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kitchen_collab.core.parser import parse_line_text, RuleParser, normalize_orders
from kitchen_collab.core.models import Order, OrderLine, OrderSource


class TestLineParser(unittest.TestCase):
    def test_parse_line_basic(self):
        line = parse_line_text("宫保鸡丁 x2 中辣")
        self.assertEqual(line.dish_name, "宫保鸡丁")
        self.assertEqual(line.qty, 2)
        self.assertEqual(line.spec, "中辣")

    def test_parse_line_allergen_note_kept(self):
        line = parse_line_text("清蒸鲈鱼 过敏:鱼")
        self.assertTrue(any("过敏" in n for n in line.notes), "过敏备注必须随行保留")


class TestRuleParser(unittest.TestCase):
    def test_structured_items(self):
        result = RuleParser().parse({
            "source": "pos", "table_no": "T05",
            "items": [{"dish": "红烧肉", "qty": 1, "spec": "", "notes": []}],
        })
        self.assertEqual(result.method, "rule")
        self.assertEqual(result.order.lines[0].dish_name, "红烧肉")
        self.assertGreaterEqual(result.confidence, 0.9)


class TestNormalize(unittest.TestCase):
    def test_duplicate_flagged(self):
        a = Order(table_no="T05", source=OrderSource.POS,
                  lines=[OrderLine(dish_name="宫保鸡丁")])
        b = Order(table_no="T05", source=OrderSource.PAPER,
                  lines=[OrderLine(dish_name="宫保鸡丁")])
        out = normalize_orders([a, b])
        hints = [h for _, hs in out for h in hs]
        self.assertTrue(any("撞单" in h for h in hints), "多源撞单必须给出人工确认提示")


if __name__ == "__main__":
    unittest.main()
