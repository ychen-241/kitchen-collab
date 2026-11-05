"""校验器测试：过敏冲突必须 BLOCK 而不是提示（unittest 风格，兼容 pytest）。"""

import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kitchen_collab.core.menu import MenuBook
from kitchen_collab.core.models import Order, OrderLine, OrderSource
from kitchen_collab.core.validator import validate
from kitchen_collab.utils.config import load_config


def make_menu() -> MenuBook:
    return MenuBook.from_config(load_config())


class TestValidator(unittest.TestCase):
    def test_allergen_conflict_blocks(self):
        order = Order(table_no="T05", source=OrderSource.POS,
                      lines=[OrderLine(dish_name="宫保鸡丁", notes=["过敏:花生"])])
        result = validate(order, make_menu())
        self.assertFalse(result.ok, "过敏冲突必须拦截")
        self.assertTrue(result.need_food_safety_sign)
        self.assertTrue(any(i.code == "ALLERGEN_CONFLICT" for i in result.issues))

    def test_non_conflicting_allergen_passes(self):
        order = Order(table_no="T05", source=OrderSource.POS,
                      lines=[OrderLine(dish_name="酸辣土豆丝", notes=["过敏:花生"])])
        result = validate(order, make_menu())
        self.assertTrue(result.ok, "土豆丝不含花生，不应拦截")

    def test_unknown_dish_gives_candidates(self):
        order = Order(table_no="T07", source=OrderSource.PHONE,
                      lines=[OrderLine(dish_name="宫保鸡丁面")])
        result = validate(order, make_menu())
        issue = next(i for i in result.issues if i.code == "UNKNOWN_DISH")
        self.assertIn("宫保鸡丁", issue.candidates, "未知菜品应给出相似候选供人工选择")

    def test_bad_table_blocks(self):
        order = Order(table_no="T99", source=OrderSource.POS,
                      lines=[OrderLine(dish_name="红烧肉")])
        result = validate(order, make_menu(), valid_tables={"T01", "T05"})
        self.assertTrue(any(i.code == "BAD_TABLE" for i in result.issues))


if __name__ == "__main__":
    unittest.main()
