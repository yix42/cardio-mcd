# -*- coding: utf-8 -*-
"""
CardioMCD · 点餐链路单元测试（品名映射 + 只读安全边界）

运行：
    python tests/test_ordering.py

夹具取自麦当劳 MCP `query-meals` 的真实返回（长沙华远华时代餐厅，storeCode=3390240）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "demo"))

from cardio_score import (  # noqa: E402
    AMBER,
    GREEN,
    RED,
    NutritionItem,
    resolve_budgets,
    score_item,
)
from run_demo import (  # noqa: E402
    EXTRA_FLAGS,
    PRESET_LABEL,
    PROFILE_PRESETS,
    build_profile,
)
from run_ordering import (  # noqa: E402
    READ_ONLY_TOOLS,
    _is_combo,
    _norm,
    build_menu_index,
    match_product,
    mcp_call,
)

# 真实菜单片段（含带规格后缀的品名）
MENU_SNIPPET = {
    "meals": {
        "515837": {"name": "圆筒冰淇淋", "currentPrice": "5", "originalPrice": "5"},
        "6102": {"name": "苹果片一袋(60g)", "currentPrice": "7", "originalPrice": "7"},
        "3755": {"name": "纯牛奶", "currentPrice": "11", "originalPrice": "11"},
        "3757": {"name": "纯悦", "currentPrice": "7.5", "originalPrice": "7.5"},
        "1440": {"name": "麦辣鸡腿汉堡", "currentPrice": "22.5", "originalPrice": "22.5"},
        "1406": {"name": "板烧鸡腿堡", "currentPrice": "23.5", "originalPrice": "23.5"},
        "520445": {"name": "薄皮焦香V翅", "currentPrice": "15.5", "originalPrice": "15.5"},
    }
}


class TestNormalization(unittest.TestCase):
    def test_strip_parenthetical_suffix(self):
        self.assertEqual(_norm("苹果片一袋(60g)"), "苹果片一袋")
        self.assertEqual(_norm("纯牛奶（盒装）"), "纯牛奶")
        self.assertEqual(_norm("纯牛奶"), "纯牛奶")

    def test_strip_spaces(self):
        self.assertEqual(_norm(" 板烧 鸡腿堡 "), "板烧鸡腿堡")


class TestMatchProduct(unittest.TestCase):
    def setUp(self):
        self.index = build_menu_index(MENU_SNIPPET)

    def test_index_built(self):
        self.assertEqual(len(self.index), 7)
        self.assertEqual(self.index["圆筒冰淇淋"]["code"], "515837")

    def test_exact_match(self):
        e = match_product("板烧鸡腿堡", self.index)
        self.assertIsNotNone(e)
        self.assertEqual(e["code"], "1406")

    def test_match_after_stripping_nutrition_suffix(self):
        """营养表"纯牛奶（盒装）" ↔ 菜单"纯牛奶"：靠去括号后缀命中。"""
        e = match_product("纯牛奶（盒装）", self.index)
        self.assertIsNotNone(e)
        self.assertEqual(e["code"], "3755")

    def test_match_menu_spec_suffix(self):
        """营养表"苹果片" ↔ 菜单"苹果片一袋(60g)"：靠双向前缀包含命中。"""
        e = match_product("苹果片", self.index)
        self.assertIsNotNone(e)
        self.assertEqual(e["code"], "6102")

    def test_no_match_returns_none(self):
        """不在当前菜单里的品名必须返回 None —— 这是"当前时段不可售"，不是匹配失败。"""
        self.assertIsNone(match_product("麦香鱼", self.index))
        self.assertIsNone(match_product("儿童鱼排堡", self.index))
        self.assertIsNone(match_product("玉米杯", self.index))

    def test_empty_name_returns_none(self):
        self.assertIsNone(match_product("", self.index))

    def test_single_char_does_not_fuzzy_match(self):
        """单字不做前缀模糊匹配，避免误命中。"""
        index = {"薯条": {"code": "4810", "name": "薯条"}}
        self.assertIsNone(match_product("薯", index))

    def test_prefers_single_item_over_combo(self):
        """真实踩坑：「吉士汉堡包」不要被匹配到「吉士汉堡包套餐」（¥26 套餐）。
        同批前缀命中时取名字最短者 —— 单品名通常比套餐名短。"""
        menu = {
            "meals": {
                "9900005449": {"name": "吉士汉堡包套餐", "currentPrice": "26.5"},
                "1451": {"name": "吉士汉堡包", "currentPrice": "14"},
            }
        }
        e = match_product("吉士汉堡包", build_menu_index(menu))
        self.assertIsNotNone(e)
        self.assertEqual(e["code"], "1451")
        self.assertEqual(e["price"], "14")

    def test_single_item_does_not_fall_back_to_combo(self):
        """更要命的一条：菜单里**只有套餐**时，绝不能拿套餐顶替单品。

        营养表"吉士汉堡包"673 mg 只描述汉堡本身，不含薯条与可乐；
        映射到"吉士汉堡包套餐"后评分会说 673 mg，用户实际吃下去远不止。
        宁可报"当前时段不可售"，也不给一个营养数据对不上的价格。
        """
        menu = {
            "meals": {
                "9900005449": {"name": "吉士汉堡包套餐", "currentPrice": "26.5"},
            }
        }
        self.assertIsNone(match_product("吉士汉堡包", build_menu_index(menu)))

    def test_combo_name_can_still_match_combo(self):
        """反过来：营养表里若本身就是套餐条目，仍应能匹配到菜单套餐。"""
        menu = {
            "meals": {
                "9900005449": {"name": "吉士汉堡包套餐", "currentPrice": "26.5"},
            }
        }
        e = match_product("吉士汉堡包套餐", build_menu_index(menu))
        self.assertIsNotNone(e)
        self.assertEqual(e["code"], "9900005449")

    def test_combo_markers_recognized(self):
        for name in ("吉士汉堡包套餐", "超值午餐组合", "欢享盒", "麦辣鸡翅桶"):
            self.assertTrue(_is_combo(name), f"{name} 应识别为套餐")
        for name in ("吉士汉堡包", "板烧鸡腿堡", "薄皮焦香V翅"):
            self.assertFalse(_is_combo(name), f"{name} 不应识别为套餐")


class TestGeneralPopulationEntry(unittest.TestCase):
    """正常人群必须是一等入口：不声明任何病史也能直接用。"""

    def test_general_preset_exists_and_is_empty(self):
        self.assertIn("general", PROFILE_PRESETS)
        self.assertEqual(PROFILE_PRESETS["general"], {},
                         "正常人群预设必须为空 —— 不要求用户声明任何病史")

    def test_every_preset_has_a_label(self):
        for key in PROFILE_PRESETS:
            self.assertIn(key, PRESET_LABEL, f"预设 {key} 缺少说明文案")
            self.assertTrue(PRESET_LABEL[key].strip())

    def test_general_budget_is_2000_not_1500(self):
        """正常人群按 2000 mg/d，不得被误当成高血压的 1500 mg/d。"""
        b = resolve_budgets(PROFILE_PRESETS["general"])
        self.assertEqual(b.daily_sodium_mg, 2000)
        b_htn = resolve_budgets(PROFILE_PRESETS["hypertension"])
        self.assertEqual(b_htn.daily_sodium_mg, 1500)

    def test_general_lunch_leaves_real_options(self):
        """正常人群 + 午餐（800 mg）下，儿童鱼排堡应为黄灯以内 —— 有得选。
        这是与"高血压早餐全部红灯"的关键对比，改动阈值若破坏它测试会失败。"""
        b = resolve_budgets({**PROFILE_PRESETS["general"], "meal": "lunch"})
        item = NutritionItem(
            productName="儿童鱼排堡", nutritionDescription=None,
            energyKj=1125, energyKcal=269, protein=15, fat=6,
            carbohydrate=30, sodium=442, calcium=0,
        )
        s = score_item(item, b, {**PROFILE_PRESETS["general"], "meal": "lunch"})
        self.assertIn(s.dims["sodium"], (GREEN, AMBER))
        self.assertLess(s.ratios["sodium"], 2.0 / 3.0)

    def test_hypertension_breakfast_has_no_good_option(self):
        """高血压 + 早餐（300 mg）下，同一餐品应红灯 —— 不假装有解。"""
        b = resolve_budgets({**PROFILE_PRESETS["hypertension"], "meal": "breakfast"})
        item = NutritionItem(
            productName="儿童鱼排堡", nutritionDescription=None,
            energyKj=1125, energyKcal=269, protein=15, fat=6,
            carbohydrate=30, sodium=442, calcium=0,
        )
        s = score_item(item, b, {**PROFILE_PRESETS["hypertension"], "meal": "breakfast"})
        self.assertEqual(s.dims["sodium"], RED)
        self.assertGreater(s.ratios["sodium"], 1.0)


class TestSharedProfileBuilder(unittest.TestCase):
    """run_demo.py（评分）与 run_ordering.py（点单）必须共用同一份画像构造逻辑。

    抽 build_profile 出来就是为了消灭"两个入口行为不一致"这类最难发现的 bug。
    """

    def test_general_defaults_to_no_history(self):
        p = build_profile("general")
        self.assertFalse(any(p.get(k) for k in EXTRA_FLAGS))

    def test_preset_flags_are_applied(self):
        p = build_profile("cardio")
        self.assertTrue(p["hypertension"])
        self.assertTrue(p["hyperlipidemia"])
        self.assertFalse(p["diabetes"])
        self.assertFalse(p["ckd"])

    def test_extra_flags_union_with_preset(self):
        """预设 + 开关是并集，不是覆盖：hypertension 预设追加 diabetes 后两者都应成立。"""
        p = build_profile("hypertension", extra=["diabetes"])
        self.assertTrue(p["hypertension"])
        self.assertTrue(p["diabetes"])

    def test_meal_is_passed_through(self):
        self.assertEqual(build_profile("general", meal="breakfast")["meal"], "breakfast")
        self.assertIsNone(build_profile("general")["meal"])

    def test_every_preset_resolves_to_a_distinct_sodium_budget(self):
        """每个预设都能解析出预算，且钠收紧组确实是 1500 mg/d。"""
        for key in PROFILE_PRESETS:
            b = resolve_budgets(build_profile(key, meal="lunch"))
            self.assertIn(b.daily_sodium_mg, (1500, 2000), f"预设 {key} 钠预算异常")
        tight = {"hypertension", "renal", "cardio"}
        for key in PROFILE_PRESETS:
            b = resolve_budgets(build_profile(key, meal="lunch"))
            expected = 1500 if key in tight else 2000
            self.assertEqual(b.daily_sodium_mg, expected, f"预设 {key} 钠预算应为 {expected}")
            self.assertIsNotNone(b.meal_sodium_mg, f"预设 {key} 指定了餐次就应有单餐预算")

    def test_builder_matches_manual_dict(self):
        """构造结果必须与手写 profile 完全一致（防止引入默认值漂移）。"""
        self.assertEqual(
            build_profile("cardio", meal="lunch"),
            {"hypertension": True, "diabetes": False,
             "hyperlipidemia": True, "ckd": False, "meal": "lunch"},
        )


class TestReadOnlyGuard(unittest.TestCase):
    def test_create_order_is_rejected(self):
        """安全边界：绝不允许调用 create-order。"""
        with self.assertRaises(RuntimeError):
            mcp_call("dummy-token", "create-order", {})

    def test_auto_bind_coupons_is_rejected(self):
        """安全边界：绝不允许自动领券（会改动账户）。"""
        with self.assertRaises(RuntimeError):
            mcp_call("dummy-token", "auto-bind-coupons", {})

    def test_draw_lottery_is_rejected(self):
        with self.assertRaises(RuntimeError):
            mcp_call("dummy-token", "draw-lottery", {})

    def test_read_only_whitelist_contains_expected_tools(self):
        for t in ("list-nutrition-foods", "query-nearby-stores", "query-meals",
                  "calculate-price", "available-coupons", "query-my-coupons"):
            self.assertIn(t, READ_ONLY_TOOLS)

    def test_read_only_whitelist_excludes_mutating_tools(self):
        for t in ("create-order", "auto-bind-coupons", "draw-lottery",
                  "mall-create-order", "cancel-order"):
            self.assertNotIn(t, READ_ONLY_TOOLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
