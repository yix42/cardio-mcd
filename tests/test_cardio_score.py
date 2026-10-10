# -*- coding: utf-8 -*-
"""
CardioMCD 单元测试

运行：
    python tests/test_cardio_score.py
或：
    python -m unittest discover -s tests

夹具 tests/fixture_nutrition.txt 取自麦当劳 MCP `list-nutrition-foods` 的真实返回快照
（节选 18 行，其中"纯牛奶（盒装）"重复出现，用于验证去重逻辑）。

⚠️ 关键约定
------------
指南只给"每日"目标，不规定三餐占比。因此：
  * 未指定 `meal` → 模块**不做任何餐次分配**，钠改按"占每日预算比例"判定；
  * 指定 `meal`   → 按该餐次的权重分配钠预算，且**钠权重与能量权重不相等**
                    （早餐有能量但钠负荷低，这是早期 ÷3 版本的错误所在）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cardio_score import (  # noqa: E402
    AMBER,
    CN_HTN_2024_DAILY_SODIUM_MG,
    DASH_LOWER_DAILY_SODIUM_MG,
    DERIVED,
    ENERGY_DAILY_SHARE_AMBER,
    ENERGY_DAILY_SHARE_GREEN,
    ENGINEERING,
    FAT_DAILY_SHARE_AMBER,
    FAT_DAILY_SHARE_GREEN,
    GREEN,
    GUIDELINE,
    MEAL_ENERGY_SHARE,
    MEAL_SODIUM_SHARE,
    RED,
    SOURCE_ANNOTATION,
    NutritionItem,
    best_main,
    evaluate_meal,
    parse_nutrition_payload,
    recommend,
    render_source_table,
    resolve_budgets,
    score_item,
    score_menu,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture_nutrition.txt")


def load_items():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return parse_nutrition_payload(f.read())


def by_name(items, name):
    for it in items:
        if it.productName == name:
            return it
    raise AssertionError(f"夹具中未找到餐品：{name}")


class TestSourceAnnotation(unittest.TestCase):
    """数字来源标注表：每个参与判定的数值都必须登记，且性质不得含糊。"""

    def test_every_entry_has_required_fields(self):
        for row in SOURCE_ANNOTATION:
            self.assertIn(row["kind"], (GUIDELINE, DERIVED, ENGINEERING))
            self.assertTrue(row["name"].strip())
            self.assertTrue(row["value"].strip())
            self.assertGreater(len(row["source"]), 10)

    def test_guideline_entries_are_real(self):
        """被标为 GUIDELINE 的钠锚点，必须与模块常量一致 —— 防止标注与代码脱节。"""
        by_name = {r["name"]: r for r in SOURCE_ANNOTATION}
        self.assertEqual(by_name["CN_HTN_2024_DAILY_SODIUM_MG"]["kind"], GUIDELINE)
        self.assertEqual(by_name["DASH_LOWER_DAILY_SODIUM_MG"]["kind"], GUIDELINE)
        self.assertEqual(CN_HTN_2024_DAILY_SODIUM_MG, 2000)
        self.assertEqual(DASH_LOWER_DAILY_SODIUM_MG, 1500)

    def test_meal_sodium_share_is_declared_engineering(self):
        """餐次钠权重**不是**指南数值，必须标为 ENGINEERING，不得伪装成 GUIDELINE。
        2026-10-10 国际核查确认：无任何成人指南规定餐次钠分配。"""
        by_name = {r["name"]: r for r in SOURCE_ANNOTATION}
        self.assertEqual(by_name["MEAL_SODIUM_SHARE"]["kind"], ENGINEERING)
        self.assertEqual(by_name["MEAL_ENERGY_SHARE"]["kind"], GUIDELINE)

    def test_render_table_contains_all_entries(self):
        text = render_source_table()
        for row in SOURCE_ANNOTATION:
            self.assertIn(row["name"], text)


class TestParsing(unittest.TestCase):
    def test_count_and_dedupe(self):
        items = load_items()
        # 18 行数据，含 1 条重复（纯牛奶），去重后应为 17
        self.assertEqual(len(items), 17)

    def test_field_values_match_source(self):
        items = load_items()
        it = by_name(items, "麦辣鸡腿汉堡")
        self.assertEqual(it.sodium, 1208)
        self.assertEqual(it.energyKcal, 485)
        self.assertEqual(it.protein, 24)
        self.assertEqual(it.fat, 24)
        self.assertEqual(it.carbohydrate, 42)
        self.assertEqual(it.calcium, 154)
        self.assertEqual(it.energyKj, 2029)

    def test_description_null_and_text(self):
        items = load_items()
        self.assertIsNone(by_name(items, "猪柳麦满分").nutritionDescription)
        self.assertEqual(by_name(items, "纯悦").nutritionDescription, "能量约清水1杯")
        self.assertEqual(by_name(items, "芝士安格斯厚牛堡").nutritionDescription, "能量约鲈鱼1条")

    def test_empty_payload(self):
        self.assertEqual(parse_nutrition_payload(""), [])
        self.assertEqual(parse_nutrition_payload("   "), [])


class TestBudgets(unittest.TestCase):
    def test_default_anchor_is_cn_htn_2024(self):
        b = resolve_budgets({})
        self.assertEqual(b.daily_sodium_mg, CN_HTN_2024_DAILY_SODIUM_MG)  # 2000
        self.assertIn("中国高血压防治指南(2024", b.sodium_anchor)

    def test_no_meal_means_no_sodium_split(self):
        """未指定餐次 → 不得分配单餐钠预算（早期 ÷3 假设已被废弃）。"""
        b = resolve_budgets({})
        self.assertIsNone(b.meal)
        self.assertIsNone(b.meal_sodium_mg)

    def test_meal_sodium_budget(self):
        b = resolve_budgets({"meal": "breakfast"})
        self.assertAlmostEqual(b.meal_sodium_mg, 2000 * MEAL_SODIUM_SHARE["breakfast"], places=4)
        self.assertEqual(b.meal_sodium_mg, 400.0)

    def test_sodium_share_differs_from_energy_share(self):
        """核心修正：早餐钠权重 ≠ 能量权重。早餐有能量，但钠负荷低。"""
        self.assertNotEqual(
            MEAL_SODIUM_SHARE["breakfast"], MEAL_ENERGY_SHARE["breakfast"]
        )
        b = resolve_budgets({"meal": "breakfast"})
        # 钠 20% × 2000 = 400 mg；能量 30% × 2000 = 600 kcal
        self.assertAlmostEqual(b.meal_sodium_mg, 400.0, places=4)
        self.assertAlmostEqual(b.meal_energy_kcal, 600.0, places=4)

    def test_breakfast_lunch_ratio_matches_usda_school_meals(self):
        """外部一致性校验（2026-10-10 国际核查）：
        美国 USDA 学校餐 早餐 540 mg / 午餐 1,110 mg = 0.486；
        本项目 20/40 = 0.500，偏差 < 0.02。
        意义：一旦有人改动餐次权重而偏离外部证据，这条测试会失败。
        """
        b = resolve_budgets({"meal": "breakfast"})
        l = resolve_budgets({"meal": "lunch"})
        ours = b.meal_sodium_mg / l.meal_sodium_mg
        usda = 540.0 / 1110.0
        self.assertAlmostEqual(ours, usda, delta=0.02)

    def test_daily_sodium_anchors(self):
        """成人每日锚点：WHO / 中国高血压指南 2024 = 2000 mg/d；DASH 低钠版 = 1500 mg/d。
        ⚠️ 国际核查确认：没有任何成人指南规定"每餐钠"，故只锁每日值。"""
        self.assertEqual(resolve_budgets({}).daily_sodium_mg, 2000)
        self.assertEqual(resolve_budgets({"hypertension": True}).daily_sodium_mg, 1500)

    def test_breakfast_sodium_stricter_than_lunch(self):
        b = resolve_budgets({"meal": "breakfast"})
        l = resolve_budgets({"meal": "lunch"})
        self.assertLess(b.meal_sodium_mg, l.meal_sodium_mg)
        self.assertLess(b.meal_energy_kcal, l.meal_energy_kcal)

    def test_hypertension_tightens_to_dash_lower(self):
        b = resolve_budgets({"hypertension": True, "meal": "breakfast"})
        self.assertEqual(b.daily_sodium_mg, DASH_LOWER_DAILY_SODIUM_MG)  # 1500
        self.assertAlmostEqual(b.meal_sodium_mg, 300.0, places=4)
        self.assertIn("DASH", b.sodium_anchor)

    def test_ckd_and_heart_failure_also_tighten(self):
        for key in ("ckd", "heart_failure", "salt_sensitive"):
            b = resolve_budgets({key: True})
            self.assertEqual(b.daily_sodium_mg, DASH_LOWER_DAILY_SODIUM_MG)

    def test_explicit_override_wins(self):
        b = resolve_budgets({"hypertension": True, "daily_sodium_mg": 1800})
        self.assertEqual(b.daily_sodium_mg, 1800)

    def test_hyperlipidemia_tightens_fat(self):
        base = resolve_budgets({"meal": "lunch"})
        tight = resolve_budgets({"hyperlipidemia": True, "meal": "lunch"})
        self.assertLess(tight.meal_fat_g, base.meal_fat_g)

    def test_invalid_meal_raises(self):
        with self.assertRaises(ValueError):
            resolve_budgets({"meal": "brunch"})


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.items = load_items()
        # 早餐：钠 400 mg、能量 600 kcal、脂肪 20.0 g
        self.budgets = resolve_budgets({"meal": "breakfast"})

    def test_sodium_light_green(self):
        s = score_item(by_name(self.items, "大杯玉米杯"), self.budgets)
        self.assertEqual(s.dims["sodium"], GREEN)  # 2/400

    def test_sodium_light_amber(self):
        s = score_item(by_name(self.items, "中薯条"), self.budgets)
        self.assertEqual(s.dims["sodium"], AMBER)  # 165/400 = 41%

    def test_sodium_light_red(self):
        s = score_item(by_name(self.items, "汉堡包"), self.budgets)
        self.assertEqual(s.dims["sodium"], RED)  # 492/400 = 123%

    def test_overall_green(self):
        s = score_item(by_name(self.items, "圆筒冰淇淋"), self.budgets)
        self.assertEqual(s.overall, GREEN)

    def test_overall_red(self):
        s = score_item(by_name(self.items, "麦辣鸡腿汉堡"), self.budgets)
        self.assertEqual(s.overall, RED)

    def test_short_board_principle(self):
        """短板原则：钠绿灯但脂肪红灯 → 整体仍为红灯。"""
        synthetic = NutritionItem(
            productName="测试-低钠高脂",
            nutritionDescription=None,
            energyKj=0, energyKcal=100,
            protein=5, fat=30, carbohydrate=10,
            sodium=10, calcium=0,
        )
        s = score_item(synthetic, self.budgets)
        self.assertEqual(s.dims["sodium"], GREEN)
        self.assertEqual(s.dims["fat"], RED)
        self.assertEqual(s.overall, RED)

    def test_no_saturated_fat_dimension(self):
        """MCP 不提供饱和/反式脂肪，模块不得凭空评分。"""
        s = score_item(by_name(self.items, "巨无霸"), self.budgets)
        self.assertNotIn("saturated_fat", s.dims)
        self.assertNotIn("trans_fat", s.dims)

    def test_carbohydrate_only_when_diabetes(self):
        s_default = score_item(by_name(self.items, "巨无霸"), self.budgets, {})
        self.assertNotIn("carbohydrate", s_default.dims)

        s_dm = score_item(
            by_name(self.items, "巨无霸"), self.budgets, {"diabetes": True, "meal": "breakfast"}
        )
        self.assertIn("carbohydrate", s_dm.dims)

    def test_menu_sorted_red_first(self):
        scores = score_menu(self.items, self.budgets)
        self.assertEqual(scores[0].overall, RED)

    def test_daily_ratio_always_reported(self):
        """无论是否指定餐次，'占每日预算比例'都应可算 —— 它不依赖餐次假设。"""
        b_known = resolve_budgets({"meal": "breakfast"})
        b_unknown = resolve_budgets({})
        for b in (b_known, b_unknown):
            s = score_item(by_name(self.items, "麦辣鸡腿汉堡"), b)
            self.assertAlmostEqual(s.ratios["sodium_daily"], 1208 / b.daily_sodium_mg, places=4)


class TestMealUnknown(unittest.TestCase):
    """未指定餐次时的诚实行为：只报占每日比例，不判整餐钠超标。

    未指定餐次时的钠分级（本模块自定，非指南数值）：
        ≤10% 每日钠预算 🟢 ／ >10% 且 ≤40% 🟡 ／ >40% 🔴
    """

    def setUp(self):
        self.items = load_items()
        self.budgets = resolve_budgets({})

    def test_daily_share_thresholds(self):
        """单品占每日钠预算：≤10% 绿 / ≤40% 黄 / >40% 红。

        用 2000 mg/d 的一般人群预算构造三个确定样本：
          150 mg = 7.5% → 绿；600 mg = 30% → 黄；900 mg = 45% → 红。
        """
        b = resolve_budgets({})  # 2000 mg/d
        for sodium_mg, expected in ((150, GREEN), (600, AMBER), (900, RED)):
            it = NutritionItem(
                productName=f"测试-{sodium_mg}mg",
                nutritionDescription=None,
                energyKj=0, energyKcal=0,
                protein=0, fat=0, carbohydrate=0,
                sodium=sodium_mg, calcium=0,
            )
            self.assertEqual(score_item(it, b).dims["sodium"], expected,
                             f"{sodium_mg} mg 判定错误")

    def test_energy_daily_share_constants_are_consistent(self):
        """未指定餐次时能量按 daily/3 判定，其三分档等价于 1/9 与 2/9 的每日占比。
        这条断言防止 ENERGY_DAILY_SHARE_* 常量与实际行为脱节（死常量漂移）。"""
        self.assertAlmostEqual(ENERGY_DAILY_SHARE_GREEN, 1.0 / 3.0 / 3.0, places=6)
        self.assertAlmostEqual(ENERGY_DAILY_SHARE_AMBER, 2.0 / 3.0 / 3.0, places=6)
        self.assertAlmostEqual(FAT_DAILY_SHARE_GREEN, 1.0 / 3.0 / 3.0, places=6)
        self.assertAlmostEqual(FAT_DAILY_SHARE_AMBER, 2.0 / 3.0 / 3.0, places=6)

    def test_meal_unknown_flag(self):
        ev = evaluate_meal([by_name(self.items, "巨无霸")], self.budgets)
        self.assertTrue(ev.meal_unknown)

    def test_sodium_not_judged_when_meal_unknown(self):
        ev = evaluate_meal([by_name(self.items, "巨无霸")], self.budgets)
        self.assertNotIn("sodium", ev.over)
        self.assertIsNone(ev.sodium_pct_of_meal_budget)
        self.assertGreater(ev.sodium_pct_of_daily, 0.0)

    def test_energy_still_judged_when_meal_unknown(self):
        combo = [
            by_name(self.items, "芝士安格斯厚牛堡"),  # 696 kcal
            by_name(self.items, "中薯条"),            # 289 kcal
        ]
        ev = evaluate_meal(combo, self.budgets)
        self.assertIn("energy", ev.over)


class TestMeal(unittest.TestCase):
    def setUp(self):
        self.items = load_items()
        # 高血压 + 早餐：每日 1500 mg × 20% = 300 mg；能量 600；脂肪 20.0
        self.budgets = resolve_budgets({"hypertension": True, "meal": "breakfast"})

    def test_meal_within_budget(self):
        combo = [
            by_name(self.items, "圆筒冰淇淋"),
            by_name(self.items, "中薯条"),
            by_name(self.items, "可乐中杯"),
        ]
        ev = evaluate_meal(combo, self.budgets, {"hypertension": True, "meal": "breakfast"})
        self.assertTrue(ev.within_budget)
        self.assertEqual(ev.over, [])
        self.assertAlmostEqual(ev.total_energy_kcal, 529.0, places=4)
        self.assertAlmostEqual(ev.total_sodium_mg, 201.0, places=4)

    def test_meal_over_sodium(self):
        combo = [
            by_name(self.items, "汉堡包"),   # 492 mg
            by_name(self.items, "中薯条"),   # 165 mg
        ]
        ev = evaluate_meal(combo, self.budgets, {"hypertension": True, "meal": "breakfast"})
        self.assertFalse(ev.within_budget)
        self.assertIn("sodium", ev.over)

    def test_same_combo_passes_at_lunch_but_fails_at_breakfast(self):
        """同一餐品：早餐钠预算 300 mg 超标，午餐 600 mg 合格 —— 餐次必须显式化。

        汉堡包 492 mg：492 > 300（早餐超），492 ≤ 600（午餐合格）。
        """
        combo = [by_name(self.items, "汉堡包")]
        breakfast = resolve_budgets({"hypertension": True, "meal": "breakfast"})
        lunch = resolve_budgets({"hypertension": True, "meal": "lunch"})
        self.assertFalse(evaluate_meal(combo, breakfast).within_budget)
        self.assertTrue(evaluate_meal(combo, lunch).within_budget)


class TestRecommend(unittest.TestCase):
    def test_recommend_requires_meal(self):
        """未指定餐次不得推荐组合 —— 否则等于偷偷假设了一个餐次分配。"""
        items = load_items()
        budgets = resolve_budgets({})
        with self.assertRaises(ValueError):
            recommend(items, budgets, size=3)

    def test_recommend_returns_valid_combos(self):
        items = load_items()
        budgets = resolve_budgets({"meal": "lunch"})
        profile = {"meal": "lunch"}
        results = recommend(items, budgets, profile, size=3, top_n=5, require_protein_g=10.0)

        self.assertGreater(len(results), 0, "应至少找到一个预算内、蛋白达标的组合")
        for ev, protein in results:
            self.assertTrue(ev.within_budget, f"{ev.items} 超出预算")
            self.assertGreaterEqual(ev.total_protein_g, 10.0)

    def test_recommend_respects_min_energy(self):
        """组合须达到最低能量，避免退化成"几样小食也算一餐"。"""
        items = load_items()
        budgets = resolve_budgets({"meal": "lunch"})
        profile = {"meal": "lunch"}
        results = recommend(items, budgets, profile, size=3, top_n=5,
                            require_protein_g=10.0, min_energy_kcal=250.0)
        for ev, _ in results:
            self.assertGreaterEqual(ev.total_energy_kcal, 250.0)

    def test_recommend_sorted_by_sodium_asc(self):
        items = load_items()
        budgets = resolve_budgets({"meal": "lunch"})
        profile = {"meal": "lunch"}
        results = recommend(items, budgets, profile, size=3, top_n=5, require_protein_g=10.0)
        pcts = [ev.sodium_pct_of_meal_budget for ev, _ in results]
        self.assertEqual(pcts, sorted(pcts))

    def test_breakfast_pool_is_stricter_than_lunch(self):
        """早餐钠预算更紧 → 可选组合数不应多于午餐。"""
        items = load_items()
        b = resolve_budgets({"meal": "breakfast"})
        l = resolve_budgets({"meal": "lunch"})
        n_b = len(recommend(items, b, {"meal": "breakfast"}, size=3, top_n=99))
        n_l = len(recommend(items, l, {"meal": "lunch"}, size=3, top_n=99))
        self.assertLessEqual(n_b, n_l)


class TestBestMain(unittest.TestCase):
    def test_sorted_by_sodium_asc(self):
        items = load_items()
        budgets = resolve_budgets({"hypertension": True, "meal": "lunch"})
        mains = best_main(items, budgets, {"meal": "lunch"}, min_protein_g=15.0, top_n=5)
        self.assertGreater(len(mains), 0)
        sodiums = [m.item.sodium for m in mains]
        self.assertEqual(sodiums, sorted(sodiums))

    def test_respects_min_protein(self):
        items = load_items()
        budgets = resolve_budgets({"meal": "breakfast"})
        mains = best_main(items, budgets, {"meal": "breakfast"}, min_protein_g=20.0, top_n=5)
        for m in mains:
            self.assertGreaterEqual(m.item.protein, 20.0)

    def test_reports_over_budget_honestly(self):
        """收紧预算后主菜单品普遍超预算，占比应如实 > 100%，不得截断或美化。"""
        items = load_items()
        budgets = resolve_budgets({"hypertension": True, "meal": "breakfast"})  # 300 mg
        mains = best_main(items, budgets, {"meal": "breakfast"}, min_protein_g=15.0, top_n=10)
        over = [m for m in mains if m.ratios["sodium"] > 1.0]
        self.assertGreater(len(over), 0, "高血压早餐预算下主菜单品应普遍超预算，这是结论不是缺陷")


if __name__ == "__main__":
    unittest.main(verbosity=2)
