"""排序引擎与状态机测试（unittest 风格，兼容 pytest）。"""

import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kitchen_collab.core.menu import MenuBook
from kitchen_collab.core.models import (Order, OrderLine, OrderSource,
                                        OrderStatus, TransitionError)
from kitchen_collab.core.scheduler import Scheduler
from kitchen_collab.utils.config import load_config


def make_env():
    menu = MenuBook.from_config(load_config())
    return menu, Scheduler(menu)


class TestScheduler(unittest.TestCase):
    def test_allergen_line_tops_queue(self):
        menu, sched = make_env()
        allergen_order = Order(table_no="T05", source=OrderSource.POS, lines=[
            OrderLine(dish_name="宫保鸡丁", notes=["过敏:花生"])])
        normal_order = Order(table_no="T06", source=OrderSource.POS, lines=[
            OrderLine(dish_name="酸辣土豆丝")])
        for o in (allergen_order, normal_order):
            o.transition(OrderStatus.PARSED)
            o.transition(OrderStatus.VALIDATED)
        board = sched.schedule([normal_order, allergen_order])
        queue = board.queues["炒锅"]
        self.assertEqual(queue[0].dish_name, "宫保鸡丁", "P0 过敏单必须置顶")
        self.assertTrue(any("过敏" in r for r in queue[0].reasons), "排序理由必须可解释")

    def test_rebalance_returns_suggestion_only(self):
        _, sched = make_env()
        board = sched.schedule([])
        self.assertEqual(sched.rebalance(board, "炒锅", "备用灶"), [])


class TestStateMachine(unittest.TestCase):
    def test_illegal_transition_raises(self):
        order = Order(table_no="T05", source=OrderSource.POS)
        with self.assertRaises(TransitionError):
            order.transition(OrderStatus.SERVED)  # created -> served 不合法


if __name__ == "__main__":
    unittest.main()
