# -*- coding: utf-8 -*-
"""
cardio_score.py — CardioMCD 心血管友好度评分内核

定位
----
在"均衡膳食"前提下，帮助用户在外食场景中做出**相对更优**的选择。
本模块只做营养数值的相对比较与预算核算，**不构成医疗建议**。

指南锚点（已核对原文，非模型记忆）
----------------------------------
1. 《中国高血压防治指南(2024年修订版)》：每日食盐摄入量 < 5 g
   → 换算为钠 < 2000 mg/d（5 g 食盐 × 1000 mg/g ÷ 2.54 ≈ 1969 mg，取 2000 mg）
2. DASH 饮食：钠 2300 mg/d（标准版）／1500 mg/d（低钠版）
3. 脂肪供能比参考《中国居民膳食指南》20%–30%，按上限 30% 推导单餐脂肪预算

⚠️ 数据可用性限制（重要，不得回避）
----------------------------------
麦当劳 MCP 的 `list-nutrition-foods` 仅返回以下字段：
    productName, nutritionDescription, energyKj, energyKcal,
    protein, fat, carbohydrate, sodium, calcium
其中 **fat 是"总脂肪"**，接口**不提供饱和脂肪酸与反式脂肪酸的拆分值**。
因此本模块：
  * 可评分维度：钠(sodium)、能量(energyKcal)、总脂肪(fat)、碳水(carbohydrate)
  * 正向参考项：蛋白质(protein)、钙(calcium)
  * **不评分**：饱和脂肪、反式脂肪 —— 接口无数据，不臆造、不以总脂肪替代

若未来 MCP 增加字段拆分，只需在 `NutritionItem` 增加字段并在
`score_item()` 的 risk 字典中登记即可，无需改动判定逻辑。

评定原则
--------
采用"短板原则"（木桶取最低）：单项餐品的最终等级 = 各风险维度中的**最差等级**。
理由：钠已达红灯的餐品，不因蛋白质含量高而被评为绿灯。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from itertools import combinations
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 1. 指南常量（可被引擎调用方覆盖，但默认值须有据可查）
# ---------------------------------------------------------------------------

# 《中国高血压防治指南(2024年修订版)》：食盐 < 5 g/d → 钠 < 2000 mg/d
CN_HTN_2024_DAILY_SODIUM_MG = 2000
# DASH 标准版
DASH_STANDARD_DAILY_SODIUM_MG = 2300
# DASH 低钠版
DASH_LOWER_DAILY_SODIUM_MG = 1500

# --- 每日总量（再由"餐次权重"分配到各餐）-----------------------------------
# 说明：指南给出的是"每日"目标，并没有规定三餐各占多少。
# 因此本模块以"每日"为锚，餐次权重改为**显式可调参数**，
# 不再隐含"三餐均分 ÷3"这个无依据的假设。

DEFAULT_DAILY_ENERGY_KCAL = 2000.0
# 脂肪：2000 kcal/d × 30% ÷ 9 kcal/g ≈ 66.7 g/d（供能比上限 30%）
DEFAULT_DAILY_FAT_G = 2000.0 * 0.30 / 9.0
# 碳水：2000 kcal/d × 55% ÷ 4 kcal/g ≈ 275 g/d
DEFAULT_DAILY_CARB_G = 2000.0 * 0.55 / 4.0

# --- 餐次权重（⚠️ 工程默认值，非指南硬性数值，可覆盖）----------------------
#
# 为什么钠和能量的权重**不相等**：
#   早餐通常有能量（主食、蛋、奶），但钠负荷明显低于午晚餐。
#   我国膳食钠 78.34% 来自食盐及调味品（国家食品安全风险评估中心
#   《中国居民膳食钠摄入风险评估》），而早餐一般不含炒菜、酱料等高盐烹调食物。
#   若把钠按三餐均分（各 1/3），会**系统性高估早餐的钠空间、低估午晚餐**。
#   这是本模块早期版本的错误，特此修正。
#
# 能量分配 30/40/30 参考《中国居民膳食指南》三餐供能比建议区间。
#
# --- 钠分配 20/40/40 的证据强度（2026-10-10 国际核查后修订：不得再写成"弱证据"）---
# ⚠️ 前提事实：**没有任何成人高血压指南规定"每餐钠"**。
#    WHO 2012/2023（钠 <2000 mg/d）、2025 ACC/AHA（≤2300，理想 <1500 mg/d）、
#    2024 ESC / 2023 ESH（钠 ≤2 g/d）—— **全部只给每日目标**。
#    故本表的餐次权重**不是指南数值，是本模块的工程默认值**（可覆盖）。
#
# ✅ 但有**三个独立来源**与之高度一致，这是它可以被接受的理由：
#    1. 美国 USDA 学校餐：早餐 540 mg ／ 午餐 1,110 mg（K-5）→ 比值 0.49 ≈ 本表 20:40 = 0.50
#    2. DASH 官方 2000 kcal / 2300 mg 菜单实测：早餐 508 mg（22%）／午餐 905 mg（39%）
#    3. 中国沈阳市卫健委官方科普：「午餐占三餐的 40%，一餐食盐 5 g × 40% = 2 g」
#
# ❌ 不得采用的旧口径：国内流行的「每餐盐不超过 2 g、每天不超过 6 g」——
#    源自 6 g/d 的**旧版**指南且按三餐均分（2×3=6），指南已更新为 5 g/d；
#    均分假设正是本模块已废弃的错误。
#
# 使用者仍可按实际情况调整或通过 profile 覆盖。

MEAL_SODIUM_SHARE = {"breakfast": 0.20, "lunch": 0.40, "dinner": 0.40, "snack": 0.10}
MEAL_ENERGY_SHARE = {"breakfast": 0.30, "lunch": 0.40, "dinner": 0.30, "snack": 0.10}
MEAL_FAT_SHARE = {"breakfast": 0.30, "lunch": 0.40, "dinner": 0.30, "snack": 0.10}
MEAL_CARB_SHARE = {"breakfast": 0.30, "lunch": 0.40, "dinner": 0.30, "snack": 0.10}

VALID_MEALS = ("breakfast", "lunch", "dinner", "snack")

# --- 未指定餐次时的兜底阈值（直接对"每日预算"取比例，不做餐次分配）---------
# 单一餐品占每日钠预算：≤10% 绿 ／ >10% 且 ≤40% 黄 ／ >40% 红。
# 40% 这个红灯起点由使用者指定：含义是「一件东西就吃掉全天钠预算的四成」，
# 已足以单独构成一餐的全部钠负荷，再往上没有讨论空间。
# ⚠️ 这是本模块自定的分级阈值，不是指南数值（指南从未定义"单品占每日比例"分级）。
SODIUM_DAILY_SHARE_GREEN = 0.10   # ≤10% 每日钠预算 → 绿灯
SODIUM_DAILY_SHARE_AMBER = 0.40   # ≤40% 每日钠预算 → 黄灯；>40% → 红灯
# 能量/脂肪按"单餐约占 1/3 日均"折算，再用三分档
ENERGY_DAILY_SHARE_GREEN = 1.0 / 9.0
ENERGY_DAILY_SHARE_AMBER = 2.0 / 9.0
FAT_DAILY_SHARE_GREEN = 1.0 / 9.0
FAT_DAILY_SHARE_AMBER = 2.0 / 9.0

# 高脂血症画像下脂肪预算的收紧系数（本模块自定，非指南数值）
HYPERLIPIDEMIA_FAT_FACTOR = 0.8

GREEN, AMBER, RED = "green", "amber", "red"
SEVERITY = {GREEN: 0, AMBER: 1, RED: 2}
_LIGHT_CN = {GREEN: "绿灯", AMBER: "黄灯", RED: "红灯"}


# ---------------------------------------------------------------------------
# 1b. 数字来源标注（每个数是谁给的，可打印、可审计）
# ---------------------------------------------------------------------------
# 使用者要求「写个标注，我们按多少给的」。这里把**每一个**参与判定的数值都登记，
# 明确区分三类：
#   GUIDELINE  —— 有指南/权威机构原文，可直接溯源
#   DERIVED    —— 由 GUIDELINE 值经明确算式推导
#   ENGINEERING —— 本模块自定，无指南依据（必须显式承认，不得伪装成指南数值）
#
# 用 `python src/cardio_score.py --sources` 打印本表。

GUIDELINE = "GUIDELINE"
DERIVED = "DERIVED"
ENGINEERING = "ENGINEERING"

SOURCE_ANNOTATION: List[dict] = [
    {
        "name": "CN_HTN_2024_DAILY_SODIUM_MG",
        "value": "2000 mg/d",
        "kind": GUIDELINE,
        "source": "《中国高血压防治指南(2024年修订版)》食盐 <5 g/d；WHO 成人钠 <2000 mg/d（两者一致）",
    },
    {
        "name": "DASH_STANDARD_DAILY_SODIUM_MG",
        "value": "2300 mg/d",
        "kind": GUIDELINE,
        "source": "DASH 标准版；2025 ACC/AHA 高血压指南 ≤2300 mg/d",
    },
    {
        "name": "DASH_LOWER_DAILY_SODIUM_MG",
        "value": "1500 mg/d",
        "kind": GUIDELINE,
        "source": "DASH 低钠版；2025 ACC/AHA 理想目标 <1500 mg/d（高血压/心衰/CKD/盐敏感采用）",
    },
    {
        "name": "DEFAULT_DAILY_ENERGY_KCAL",
        "value": "2000 kcal/d",
        "kind": ENGINEERING,
        "source": "成人参考摄入量，工程默认；可按体重管理目标覆盖",
    },
    {
        "name": "DEFAULT_DAILY_FAT_G",
        "value": "66.7 g/d",
        "kind": DERIVED,
        "source": "2000 kcal/d × 30% ÷ 9 kcal/g；供能比上限 30% 出自《中国居民膳食指南》",
    },
    {
        "name": "DEFAULT_DAILY_CARB_G",
        "value": "275 g/d",
        "kind": DERIVED,
        "source": "2000 kcal/d × 55% ÷ 4 kcal/g",
    },
    {
        "name": "MEAL_ENERGY_SHARE",
        "value": "早30% / 午40% / 晚30% / 加餐10%",
        "kind": GUIDELINE,
        "source": "《中国居民膳食指南》三餐供能比建议区间",
    },
    {
        "name": "MEAL_SODIUM_SHARE",
        "value": "早20% / 午40% / 晚40% / 加餐10%",
        "kind": ENGINEERING,
        "source": (
            "无成人指南规定餐次钠分配（2026-10-10 国际核查确认）。"
            "取值与三个独立来源一致：USDA 学校餐早/午=540/1110=0.49；"
            "DASH 官方菜单早餐22%/午餐39%；沈阳市卫健委科普「午餐占40%」"
        ),
    },
    {
        "name": "SODIUM_DAILY_SHARE_GREEN",
        "value": "≤10% 每日钠预算 → 绿灯",
        "kind": ENGINEERING,
        "source": "本模块自定的分级阈值；仅在未指定餐次时启用",
    },
    {
        "name": "SODIUM_DAILY_SHARE_AMBER",
        "value": "≤40% 每日钠预算 → 黄灯；>40% → 红灯",
        "kind": ENGINEERING,
        "source": "由使用者指定（2026-10-10）：单品占全天钠四成即红灯；非指南数值",
    },
    {
        "name": "ENERGY/FAT_DAILY_SHARE_GREEN, _AMBER",
        "value": "1/9 与 2/9（≈11.1%、22.2%）",
        "kind": ENGINEERING,
        "source": "等价于『单餐约占日均 1/3』后再取三分档；用于未指定餐次时的能量/脂肪判定",
    },
    {
        "name": "HYPERLIPIDEMIA_FAT_FACTOR",
        "value": "× 0.8",
        "kind": ENGINEERING,
        "source": "高脂血症画像下脂肪预算收紧 20%；本模块自定，非指南数值",
    },
    {
        "name": "_light_by_thirds 分档",
        "value": "≤1/3 绿 ／ ≤2/3 黄 ／ >2/3 红",
        "kind": ENGINEERING,
        "source": "本模块自定的分级规则，非指南原文；可覆盖",
    },
    {
        "name": "参考：中国临床限钠膳食分级",
        "value": "低盐 ~2000 / 无盐 ~1000 / 低钠 ≤500 mg/d",
        "kind": GUIDELINE,
        "source": "中国临床营养限钠膳食分级（列出备查；本模块当前未直接采用）",
    },
]


def _display_width(s: str) -> int:
    """按终端显示宽度计算（中文/全角字符占 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    """按显示宽度左对齐填充，避免中文列错位。"""
    return s + " " * max(0, width - _display_width(s))


def render_source_table() -> str:
    """打印"我们按多少给的"标注表 —— 每个数值的来源与证据性质。"""
    kind_cn = {GUIDELINE: "指南锚定", DERIVED: "推导", ENGINEERING: "本模块自定"}
    cols = (32, 26, 12)
    out = [
        "=" * 108,
        "CardioMCD · 数字来源标注（我们按多少给的）",
        "=" * 108,
        _pad("常量", cols[0]) + _pad("取值", cols[1]) + _pad("性质", cols[2]) + "来源 / 依据",
        "-" * 108,
    ]
    for row in SOURCE_ANNOTATION:
        out.append(
            _pad(row["name"], cols[0])
            + _pad(row["value"], cols[1])
            + _pad(kind_cn[row["kind"]], cols[2])
            + row["source"]
        )
    out.append("-" * 108)
    out.append(
        "说明：GUIDELINE=有指南原文可溯源；DERIVED=由 GUIDELINE 值经算式推导；"
        "ENGINEERING=本模块自定，无指南依据，使用者可覆盖。"
    )
    out.append("免责声明：本模块输出为营养数值的相对比较，不构成医疗建议。")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 2. 数据结构
# ---------------------------------------------------------------------------

@dataclass
class NutritionItem:
    """单条餐品营养记录。字段与 MCP `list-nutrition-foods` 返回一致。"""
    productName: str
    nutritionDescription: Optional[str]
    energyKj: float
    energyKcal: float
    protein: float
    fat: float
    carbohydrate: float
    sodium: float
    calcium: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Budgets:
    """按用户画像与餐次解析出的预算。

    `meal` 为 None 表示"未指定餐次"，此时 `meal_sodium_mg` 也为 None ——
    钠改为按"占每日预算比例"判定，不再做任何餐次分配假设。
    """
    daily_sodium_mg: float
    daily_energy_kcal: float
    daily_fat_g: float
    daily_carb_g: float
    meal: Optional[str]                # breakfast / lunch / dinner / snack / None
    meal_sodium_mg: Optional[float]    # None = 未指定餐次，不分配
    meal_energy_kcal: float
    meal_fat_g: float
    meal_carb_g: float
    sodium_anchor: str                 # 预算来源说明，便于溯源


@dataclass
class ItemScore:
    item: NutritionItem
    dims: Dict[str, str] = field(default_factory=dict)
    overall: str = GREEN
    # 各维度占单餐预算的比例（用于解释，而非判定）
    ratios: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "productName": self.item.productName,
            "overall": self.overall,
            "overall_cn": _LIGHT_CN[self.overall],
            "dims": {k: {"light": v, "cn": _LIGHT_CN[v]} for k, v in self.dims.items()},
            "ratios": {k: round(v, 3) for k, v in self.ratios.items()},
            "energyKcal": self.item.energyKcal,
            "protein": self.item.protein,
            "fat": self.item.fat,
            "carbohydrate": self.item.carbohydrate,
            "sodium": self.item.sodium,
            "calcium": self.item.calcium,
        }


# ---------------------------------------------------------------------------
# 3. 解析 MCP 返回的 data 字符串
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*\[\d+\]\s*\{(?P<fields>[^}]*)\}\s*:\s*$")


def parse_nutrition_payload(payload: str) -> List[NutritionItem]:
    """解析 `list-nutrition-foods` 返回的 data 字符串。

    真实格式示例：::

        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          中薯条,null,1210,289,4,12,38,165,18

    注意：
      * 首行为头部，含条目数与字段名，需跳过；
      * 数值字段固定为后 8 个，品名可能含逗号，故用尾部切分保证稳健；
      * 'null' 描述归一为 None；
      * 存在重复品名（如"纯牛奶（盒装）"出现两次），按品名去重保留首条。
    """
    if not payload or not payload.strip():
        return []

    lines = payload.replace("\\n", "\n").split("\n")

    items: List[NutritionItem] = []
    seen: set = set()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if _HEADER_RE.match(line):
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            # 数据行字段不足，跳过而非猜测
            continue

        # 数值固定取尾部 8 个字段，品名占据剩余前段（可含逗号）
        name = ",".join(parts[:-8])
        nums = parts[-8:]

        desc = nums[0]
        if desc.lower() in ("null", "none", ""):
            desc = None

        def _num(s: str) -> float:
            s = s.strip()
            if s.lower() in ("null", "none", ""):
                return 0.0
            try:
                return float(s)
            except ValueError:
                return 0.0

        item = NutritionItem(
            productName=name,
            nutritionDescription=desc,
            energyKj=_num(nums[1]),
            energyKcal=_num(nums[2]),
            protein=_num(nums[3]),
            fat=_num(nums[4]),
            carbohydrate=_num(nums[5]),
            sodium=_num(nums[6]),
            calcium=_num(nums[7]),
        )

        if item.productName in seen:
            continue
        seen.add(item.productName)
        items.append(item)

    return items


# ---------------------------------------------------------------------------
# 4. 用户画像 → 单餐预算
# ---------------------------------------------------------------------------

def resolve_budgets(profile: Optional[dict] = None) -> Budgets:
    """按用户画像与餐次解析预算。

    profile 可含布尔键：
        hypertension / heart_failure / ckd / salt_sensitive / diabetes / hyperlipidemia
    餐次键：
        meal ∈ {"breakfast", "lunch", "dinner", "snack"}，缺省 None
    数值覆盖键：
        daily_sodium_mg / daily_energy_kcal / daily_fat_g / daily_carb_g
        meal_sodium_share / meal_energy_share / meal_fat_share（餐次权重覆盖）

    收紧规则：
        合并高血压／心衰／慢性肾病／盐敏感 → 采用 DASH 低钠版 1500 mg/d
        其余 → 采用《中国高血压防治指南(2024)》2000 mg/d

    ⚠️ 关于餐次权重：
        指南只给"每日"目标，不规定三餐占比。本模块的餐次权重是**可调参数**，
        默认钠 20/40/40、能量 30/40/30（两者不等，因为早餐有能量但钠负荷低）。
        若未指定 `meal`，则**不做任何餐次分配**，钠改按占每日预算比例判定。
    """
    profile = profile or {}

    sodium_sensitive = any(
        bool(profile.get(k))
        for k in ("hypertension", "heart_failure", "ckd", "salt_sensitive")
    )

    if profile.get("daily_sodium_mg") is not None:
        daily_sodium = float(profile["daily_sodium_mg"])
        anchor = "用户指定值"
    elif sodium_sensitive:
        daily_sodium = float(DASH_LOWER_DAILY_SODIUM_MG)
        anchor = "DASH 低钠版 1500 mg/d（高血压/心衰/肾病/盐敏感）"
    else:
        daily_sodium = float(CN_HTN_2024_DAILY_SODIUM_MG)
        anchor = "《中国高血压防治指南(2024年修订版)》食盐<5g/d → 钠<2000 mg/d"

    daily_energy = float(profile.get("daily_energy_kcal") or DEFAULT_DAILY_ENERGY_KCAL)
    daily_fat = float(profile.get("daily_fat_g") or DEFAULT_DAILY_FAT_G)
    daily_carb = float(profile.get("daily_carb_g") or DEFAULT_DAILY_CARB_G)

    # 高脂血症：脂肪预算收紧 20%（本模块自定，非指南数值）
    if profile.get("hyperlipidemia"):
        daily_fat *= HYPERLIPIDEMIA_FAT_FACTOR

    meal = profile.get("meal")
    if meal is not None:
        meal = str(meal).lower()
        if meal not in VALID_MEALS:
            raise ValueError(f"未知餐次：{meal}，可选 {VALID_MEALS}")

    if meal:
        s_share = float(profile.get("meal_sodium_share") or MEAL_SODIUM_SHARE[meal])
        e_share = float(profile.get("meal_energy_share") or MEAL_ENERGY_SHARE[meal])
        f_share = float(profile.get("meal_fat_share") or MEAL_FAT_SHARE[meal])
        c_share = float(profile.get("meal_carb_share") or MEAL_CARB_SHARE[meal])
        meal_sodium = daily_sodium * s_share
        meal_energy = daily_energy * e_share
        meal_fat = daily_fat * f_share
        meal_carb = daily_carb * c_share
    else:
        # 未指定餐次：钠不分配（None）；能量/脂肪/碳水按通用 1/3 估算
        meal_sodium = None
        meal_energy = daily_energy / 3.0
        meal_fat = daily_fat / 3.0
        meal_carb = daily_carb / 3.0

    return Budgets(
        daily_sodium_mg=daily_sodium,
        daily_energy_kcal=daily_energy,
        daily_fat_g=daily_fat,
        daily_carb_g=daily_carb,
        meal=meal,
        meal_sodium_mg=meal_sodium,
        meal_energy_kcal=meal_energy,
        meal_fat_g=meal_fat,
        meal_carb_g=meal_carb,
        sodium_anchor=anchor,
    )


# ---------------------------------------------------------------------------
# 5. 判定
# ---------------------------------------------------------------------------

def _light_by_thirds(value: float, budget: float) -> str:
    """按"占预算三分之一 / 三分之二"分档。

    ≤ 1/3 预算 → 绿灯；≤ 2/3 预算 → 黄灯；> 2/3 预算 → 红灯。
    """
    if budget <= 0:
        return GREEN
    if value <= budget / 3.0:
        return GREEN
    if value <= budget * 2.0 / 3.0:
        return AMBER
    return RED


def score_item(item: NutritionItem, budgets: Budgets, profile: Optional[dict] = None) -> ItemScore:
    """对单个餐品打分。最终等级取各风险维度最差值（短板原则）。"""
    profile = profile or {}

    dims: Dict[str, str] = {}
    ratios: Dict[str, float] = {}

    # ---- 钠 ----
    # 无论是否指定餐次，都先算"占每日预算比例"——这是不依赖任何餐次分配假设的主指标。
    sodium_daily_ratio = (
        item.sodium / budgets.daily_sodium_mg if budgets.daily_sodium_mg else 0.0
    )
    ratios["sodium_daily"] = sodium_daily_ratio

    if budgets.meal_sodium_mg is not None:
        # 指定了餐次：按该餐次的钠预算判定
        dims["sodium"] = _light_by_thirds(item.sodium, budgets.meal_sodium_mg)
        ratios["sodium"] = (
            item.sodium / budgets.meal_sodium_mg if budgets.meal_sodium_mg else 0.0
        )
    else:
        # 未指定餐次：不做餐次分配，直接按占每日预算比例判定
        if sodium_daily_ratio <= SODIUM_DAILY_SHARE_GREEN:
            dims["sodium"] = GREEN
        elif sodium_daily_ratio <= SODIUM_DAILY_SHARE_AMBER:
            dims["sodium"] = AMBER
        else:
            dims["sodium"] = RED
        ratios["sodium"] = sodium_daily_ratio

    dims["energy"] = _light_by_thirds(item.energyKcal, budgets.meal_energy_kcal)
    ratios["energy"] = item.energyKcal / budgets.meal_energy_kcal if budgets.meal_energy_kcal else 0.0

    dims["fat"] = _light_by_thirds(item.fat, budgets.meal_fat_g)
    ratios["fat"] = item.fat / budgets.meal_fat_g if budgets.meal_fat_g else 0.0

    # 碳水仅在糖尿病画像下参与判定；否则只记录比例、不判灯
    if profile.get("diabetes"):
        dims["carbohydrate"] = _light_by_thirds(item.carbohydrate, budgets.meal_carb_g)
    ratios["carbohydrate"] = (
        item.carbohydrate / budgets.meal_carb_g if budgets.meal_carb_g else 0.0
    )

    overall = max(dims.values(), key=lambda d: SEVERITY[d])

    return ItemScore(item=item, dims=dims, overall=overall, ratios=ratios)


def score_menu(
    items: Sequence[NutritionItem],
    budgets: Budgets,
    profile: Optional[dict] = None,
) -> List[ItemScore]:
    """对整份菜单打分，按"红灯优先、钠占比降序"排序，便于先看风险最高者。"""
    scores = [score_item(it, budgets, profile) for it in items]
    scores.sort(key=lambda s: (-SEVERITY[s.overall], -s.ratios.get("sodium", 0.0)))
    return scores


# ---------------------------------------------------------------------------
# 6. 整餐核算与组合推荐
# ---------------------------------------------------------------------------

@dataclass
class MealEvaluation:
    items: List[NutritionItem]
    total_energy_kcal: float
    total_sodium_mg: float
    total_fat_g: float
    total_carb_g: float
    total_protein_g: float
    within_budget: bool
    over: List[str]
    sodium_pct_of_meal_budget: Optional[float]  # None = 未指定餐次
    sodium_pct_of_daily: float                  # 始终可算，不依赖餐次分配
    meal_unknown: bool

    def as_dict(self) -> dict:
        return {
            "items": [i.productName for i in self.items],
            "total_energy_kcal": round(self.total_energy_kcal, 1),
            "total_sodium_mg": round(self.total_sodium_mg, 1),
            "total_fat_g": round(self.total_fat_g, 1),
            "total_carb_g": round(self.total_carb_g, 1),
            "total_protein_g": round(self.total_protein_g, 1),
            "within_budget": self.within_budget,
            "over": self.over,
            "sodium_pct_of_meal_budget": (
                round(self.sodium_pct_of_meal_budget, 3)
                if self.sodium_pct_of_meal_budget is not None else None
            ),
            "sodium_pct_of_daily": round(self.sodium_pct_of_daily, 3),
            "meal_unknown": self.meal_unknown,
        }


def evaluate_meal(
    items: Sequence[NutritionItem],
    budgets: Budgets,
    profile: Optional[dict] = None,
) -> MealEvaluation:
    """核算一整餐是否落在预算内。

    未指定餐次时（`budgets.meal_sodium_mg is None`），**不判断整餐钠是否超标** ——
    不知道是哪一餐，就没有可比的单餐钠上限。此时只报告占每日预算比例，
    并在结果中置 `meal_unknown=True` 由上层如实告知用户。
    """
    profile = profile or {}

    te = sum(i.energyKcal for i in items)
    ts = sum(i.sodium for i in items)
    tf = sum(i.fat for i in items)
    tc = sum(i.carbohydrate for i in items)
    tp = sum(i.protein for i in items)

    meal_unknown = budgets.meal_sodium_mg is None

    over: List[str] = []
    if not meal_unknown and ts > budgets.meal_sodium_mg:
        over.append("sodium")
    if te > budgets.meal_energy_kcal:
        over.append("energy")
    if tf > budgets.meal_fat_g:
        over.append("fat")
    if profile.get("diabetes") and tc > budgets.meal_carb_g:
        over.append("carbohydrate")

    return MealEvaluation(
        items=list(items),
        total_energy_kcal=te,
        total_sodium_mg=ts,
        total_fat_g=tf,
        total_carb_g=tc,
        total_protein_g=tp,
        within_budget=not over,
        over=over,
        sodium_pct_of_meal_budget=(
            ts / budgets.meal_sodium_mg if budgets.meal_sodium_mg else None
        ),
        sodium_pct_of_daily=(
            ts / budgets.daily_sodium_mg if budgets.daily_sodium_mg else 0.0
        ),
        meal_unknown=meal_unknown,
    )


def recommend(
    candidates: Sequence[NutritionItem],
    budgets: Budgets,
    profile: Optional[dict] = None,
    size: int = 3,
    top_n: int = 5,
    require_protein_g: float = 10.0,
    min_energy_kcal: float = 250.0,
    max_pool: int = 60,
) -> List[Tuple[MealEvaluation, float]]:
    """在预算内搜索最优组合。

    策略（可解释优先，非黑箱）：
      1. 先用 `score_item` 过滤掉任一维度红灯的单品，缩小搜索池；
      2. 若池中不足 `size` 个，放宽到允许黄灯；
      3. 枚举组合，保留"整餐不超预算"、蛋白质 ≥ require_protein_g、
         且能量 ≥ min_energy_kcal（保证是一顿饭而非几样小食）的组合；
      4.     排序：钠占单餐预算比例升序 → 蛋白质降序（同等钠时优先更优蛋白）。

    ⚠️ 必须指定餐次（budgets.meal）：未指定餐次就没有单餐钠预算，
    若此时仍要"推荐组合"，等于偷偷假设了一个餐次分配 —— 这正是本模块早期
    版本的错误。故此处显式拒绝，由上层向用户追问"这是哪一餐"。

    返回 [(MealEvaluation, protein_g), ...]，最多 top_n 条。
    """
    profile = profile or {}
    if budgets.meal is None or budgets.meal_sodium_mg is None:
        raise ValueError(
            "未指定餐次，无法核算单餐钠预算。"
            "请在 profile['meal'] 中指定 breakfast/lunch/dinner/snack 后再调用 recommend()。"
        )
    pool = [it for it in candidates if score_item(it, budgets, profile).overall == GREEN]
    if len(pool) < size:
        pool = [
            it for it in candidates
            if score_item(it, budgets, profile).overall in (GREEN, AMBER)
        ]
    if len(pool) > max_pool:
        # 按钠升序截断，控制组合爆炸
        pool = sorted(pool, key=lambda i: i.sodium)[:max_pool]

    results: List[Tuple[MealEvaluation, float]] = []
    if len(pool) < size:
        size = len(pool)

    for combo in combinations(pool, size):
        ev = evaluate_meal(combo, budgets, profile)
        if not ev.within_budget:
            continue
        if ev.total_protein_g < require_protein_g:
            continue
        if ev.total_energy_kcal < min_energy_kcal:
            continue
        results.append((ev, ev.total_protein_g))

    results.sort(key=lambda r: (r[0].sodium_pct_of_meal_budget, -r[1]))
    return results[:top_n]


def best_main(
    candidates: Sequence[NutritionItem],
    budgets: Budgets,
    profile: Optional[dict] = None,
    min_protein_g: float = 15.0,
    top_n: int = 3,
) -> List[ItemScore]:
    """给出"相对最低钠的主食"。

    存在这样一个临床事实：在高血压／心衰等收紧后的钠预算下，
    **几乎没有主菜单品能装进单餐钠预算**。此时若只给"预算内组合"，
    结果会退化成几样小食，不构成一顿饭。

    本函数因此改为：在蛋白质 ≥ min_protein_g（视为主食/含蛋白质主菜）的条目中，
    按钠升序给出相对最优者，并如实标注其占单餐钠预算的比例
    （该比例通常 > 100%，即"单品即超预算"——这是结论，不是缺陷）。
    """
    profile = profile or {}
    mains = [it for it in candidates if it.protein >= min_protein_g]
    scored = score_menu(mains, budgets, profile)
    # score_menu 按"红灯优先、钠占比降序"排序，这里改为钠升序取相对最优
    scored.sort(key=lambda s: s.item.sodium)
    return scored[:top_n]


# ---------------------------------------------------------------------------
# 7. 报告与 CLI
# ---------------------------------------------------------------------------

def render_report(
    scores: Sequence[ItemScore],
    budgets: Budgets,
    profile: Optional[dict] = None,
    limit: int = 20,
) -> str:
    """生成人类可读的红绿灯报告。"""
    meal_cn = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}

    out: List[str] = []
    out.append("=" * 78)
    out.append("CardioMCD · 心血管友好度红绿灯")
    out.append("=" * 78)
    out.append(f"钠预算锚点：{budgets.sodium_anchor}")
    out.append(
        f"每日预算：钠 {budgets.daily_sodium_mg:.0f} mg ｜ "
        f"能量 {budgets.daily_energy_kcal:.0f} kcal ｜ "
        f"脂肪 {budgets.daily_fat_g:.1f} g ｜ 碳水 {budgets.daily_carb_g:.0f} g"
    )
    if budgets.meal:
        share = MEAL_SODIUM_SHARE.get(budgets.meal)
        out.append(
            f"餐次：{meal_cn.get(budgets.meal, budgets.meal)}"
            f"（钠权重 {share:.0%}）→ 单餐钠预算 {budgets.meal_sodium_mg:.0f} mg"
        )
    else:
        out.append("餐次：未指定 → 钠改按『占每日预算比例』判定，不做餐次分配假设")
    out.append(
        f"单餐预算：能量 {budgets.meal_energy_kcal:.0f} kcal ｜ "
        f"脂肪 {budgets.meal_fat_g:.1f} g ｜ 碳水 {budgets.meal_carb_g:.0f} g"
    )
    out.append("说明：fat 为总脂肪；MCP 未提供饱和/反式脂肪拆分，故不评分。")
    out.append("-" * 78)
    out.append(
        f"{'餐品':<20}{'灯':<7}{'钠mg':>7}{'占单餐':>9}{'占每日':>9}"
        f"{'能量':>7}{'脂肪g':>7}{'蛋白g':>7}"
    )
    out.append("-" * 78)

    for s in list(scores)[:limit]:
        it = s.item
        meal_pct = (
            f"{s.ratios['sodium'] * 100:>7.0f}%" if budgets.meal_sodium_mg else f"{'—':>8}"
        )
        out.append(
            f"{it.productName[:18]:<20}"
            f"{_LIGHT_CN[s.overall]:<7}"
            f"{it.sodium:>7.0f}"
            f"{meal_pct}"
            f"{s.ratios['sodium_daily'] * 100:>8.0f}%"
            f"{it.energyKcal:>7.0f}"
            f"{it.fat:>7.0f}"
            f"{it.protein:>7.0f}"
        )
    out.append("-" * 78)
    out.append("免责声明：本输出为营养数值的相对比较，不构成医疗建议。")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CardioMCD 心血管友好度评分")
    ap.add_argument("--payload", help="list-nutrition-foods 返回的 data 字符串文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--hypertension", action="store_true")
    ap.add_argument("--diabetes", action="store_true")
    ap.add_argument("--hyperlipidemia", action="store_true")
    ap.add_argument("--meal", choices=list(VALID_MEALS),
                    help="餐次；不传则不分配钠预算，改按占每日预算比例判定")
    ap.add_argument("--sources", action="store_true",
                    help="打印数字来源标注表（我们按多少给的）后退出")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    if args.sources:
        print(render_source_table())
        return 0

    profile = {
        "hypertension": args.hypertension,
        "diabetes": args.diabetes,
        "hyperlipidemia": args.hyperlipidemia,
        "meal": args.meal,
    }

    if not args.payload:
        ap.print_help()
        return 1

    with open(args.payload, "r", encoding="utf-8") as f:
        payload = f.read()

    items = parse_nutrition_payload(payload)
    budgets = resolve_budgets(profile)
    scores = score_menu(items, budgets, profile)

    if args.json:
        print(json.dumps(
            {
                "budgets": asdict(budgets),
                "items": [s.as_dict() for s in scores],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(render_report(scores, budgets, profile, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
