#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_ordering.py — 端到端真实链路：营养评分 → 门店 → 菜单 → 品名映射 → 真实计价

链路
----
    list-nutrition-foods   （营养数据，158 条）
        ↓ 按画像 + 餐次打分
    query-nearby-stores    （拿 storeCode；到店自取 beType=1）
        ↓
    query-meals            （拿可售餐品与 productCode、现价）
        ↓ 品名映射（营养表品名 ≠ 菜单品名，见下）
    calculate-price        （真实计价，单位：分）

⚠️ 安全边界（重要）
------------------
本脚本**只做只读调用**，绝不调用以下会改动账户/产生交易的接口：
    create-order（下单）、auto-bind-coupons（领券）、draw-lottery（抽奖）、
    mall-create-order（商城下单）、cancel-order（取消订单）
即：**不领券、不下单、不改动账户任何数据**。

用法
----
    export MCD_TOKEN="你的MCP Token"

    # 正常人群（默认入口，零门槛）
    python demo/run_ordering.py --meal lunch
    python demo/run_ordering.py --meal breakfast

    # 心血管风险人群
    python demo/run_ordering.py --profile hypertension --meal breakfast
    python demo/run_ordering.py --profile cardio     --meal dinner
    python demo/run_ordering.py --meal lunch --hypertension --diabetes

    # 不联网，只演示品名映射逻辑
    python demo/run_ordering.py --offline

真实踩坑记录（写在代码里免得后人重踩）
--------------------------------------
1. **营养表品名 ≠ 菜单品名**，且不是一一对应：
   - 营养表 158 条，但**同一时段可售的只有其中一部分**
     （例：夜市/宵夜时段没有"麦香鱼""玉米杯"，早餐时段才有麦满分系列）；
   - 菜单品名常带规格后缀：营养表"苹果片" ↔ 菜单"苹果片一袋(60g)"；
     营养表"纯牛奶（盒装）" ↔ 菜单"纯牛奶"。
   → 因此映射**不能用字符串精确相等**，本脚本用三级匹配：
      精确 → 去括号后缀后精确 → 双向前缀包含（长度 ≥2）。
2. `calculate-price` 返回的**金额单位是「分」**，展示需 ÷100。
3. `query-meals` / `calculate-price` 都需要 `storeCode`，必须先调
   `query-nearby-stores`（到店）或 `delivery-query-stores`（外送）拿到。
4. `available-coupons` 只返回券标题/图片/状态，**不含 couponId**，
   无法直接带入 `calculate-price`；`query-my-coupons` 若为空则当前无券可用
   （本项目不自动领券，因为那会改动账户）。
5. **门店会打烊**：21:00 实测"长沙华远华时代餐厅"已打烊，`query-meals` 返回 0 件。
   因此必须遍历候选门店，取第一家仍有可售餐品的（本例会切到"培元桥地铁站餐厅"）。
6. **单品不能落到套餐上**（最容易骗过自己的一条）：菜单里可能只有"吉士汉堡包套餐"
   而没有"吉士汉堡包"，前缀匹配会命中套餐，看起来"可点单、有价格"，
   但营养表的 673 mg 只描述汉堡本身，不含薯条与可乐 —— 评分与实付商品对不上。
   `match_product` 因此要求**套餐性一致**：单品只配单品，套餐只配套餐；
   配不上就如实报"当前时段不可售"。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cardio_score import (  # noqa: E402
    VALID_MEALS,
    best_main,
    parse_nutrition_payload,
    resolve_budgets,
    score_item,
)
from run_demo import (  # noqa: E402
    EXTRA_FLAGS,
    FLAG_LABEL,
    PRESET_LABEL,
    PROFILE_PRESETS,
    build_profile,
    call_list_nutrition_foods,
)

MCP_URL = "https://mcp.mcd.cn"
ORIGINAL_RESPONSE_MARKER = "## Original Response"

# 只读白名单：白名单外的工具一律拒绝调用（防止误触下单/领券/抽奖）
READ_ONLY_TOOLS = {
    "list-nutrition-foods",
    "query-nearby-stores",
    "query-meals",
    "query-meal-detail",
    "calculate-price",
    "available-coupons",
    "query-my-coupons",
    "query-store-coupons",
    "query-promotions",
    "campaign-calendar",
    "query-my-account",
    "now-time-info",
}


def _extract_json(raw: str) -> dict:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    return json.loads(raw)


def _unwrap(text: str):
    """MCP 的 content[0].text 是带说明的 Markdown，业务 JSON 在
    `## Original Response` 之后。取出 data 字段（可能是 dict 或 string）。"""
    idx = text.find(ORIGINAL_RESPONSE_MARKER)
    tail = text[idx + len(ORIGINAL_RESPONSE_MARKER):] if idx >= 0 else text
    start = tail.find("{")
    if start < 0:
        raise RuntimeError(f"未在返回中定位到业务 JSON：{text[:200]}")
    obj, _ = json.JSONDecoder().raw_decode(tail[start:])
    return obj.get("data")


def mcp_call(token: str, tool: str, arguments: dict, timeout: int = 40):
    """通用 MCP 调用（只读白名单内）。"""
    if tool not in READ_ONLY_TOOLS:
        raise RuntimeError(
            f"拒绝调用非只读工具：{tool}。"
            "本脚本不领券、不下单、不改动账户数据。"
        )
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    result = _extract_json(raw).get("result", {})
    content = result.get("content") or []
    if not content:
        raise RuntimeError(f"{tool} 返回为空：{raw[:300]}")
    return _unwrap(content[0].get("text", ""))


# ---------------------------------------------------------------------------
# 品名映射：营养表品名 → 菜单 productCode
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r"[（(][^）)]*[）)]\s*$")

# 套餐标记：营养表的"吉士汉堡包"673 mg 只描述汉堡本身，不含薯条与可乐。
# 把它映射到菜单里的"吉士汉堡包套餐"虽然能点单，但**营养数据与实付商品对不上** ——
# 评分会说 673 mg，用户实际吃下去的是汉堡+薯条+可乐。宁可报"当前时段不可售"，
# 也不用一个对不上的价格去冒充"可点单"。
COMBO_MARKERS = ("套餐", "组合", "超值", "欢享", "全家桶", "分享盒", "桶")


def _is_combo(s: str) -> bool:
    return any(m in s for m in COMBO_MARKERS)


def _norm(s: str) -> str:
    """归一化：去括号后缀、去空格、全角括号统一。"""
    s = (s or "").strip()
    s = _PAREN_RE.sub("", s)
    return s.replace(" ", "")


def build_menu_index(menu_data: dict) -> dict:
    """把 query-meals 返回压成 {品名: {code, name, price}}。"""
    meals = (menu_data or {}).get("meals") or {}
    index = {}
    for code, m in meals.items():
        if not isinstance(m, dict):
            continue
        index[m.get("name", "")] = {
            "code": code,
            "name": m.get("name", ""),
            "price": m.get("currentPrice"),
            "originalPrice": m.get("originalPrice"),
        }
    return index


def match_product(nutrition_name: str, menu_index: dict):
    """三级匹配：精确 → 去括号后缀后精确 → 双向前缀包含。命中返回菜单条目，否则 None。"""
    if nutrition_name in menu_index:
        return menu_index[nutrition_name]

    n = _norm(nutrition_name)
    if not n:
        return None
    for name, entry in menu_index.items():
        if _norm(name) == n:
            return entry

    # 双向前缀包含（长度 ≥2，避免单字误匹配）
    # ⚠️ 两条约束，都是真实踩出来的：
    #   ① 命中多个时取**名字最短**的：否则「吉士汉堡包」会被匹配到「吉士汉堡包套餐」；
    #   ② **套餐性必须一致**：营养表单品不能落到菜单套餐上（反之亦然）。
    #      单品只有汉堡的营养数据，套餐还含薯条+可乐，拿它凑"可点单"是虚假匹配。
    if len(n) >= 2:
        hits = [
            entry for name, entry in menu_index.items()
            if len(_norm(name)) >= 2
            and _is_combo(_norm(name)) == _is_combo(n)
            and (n.startswith(_norm(name)) or _norm(name).startswith(n))
        ]
        if hits:
            return min(hits, key=lambda e: len(_norm(e["name"])))
    return None


def orderable_lowest_sodium(items, menu_index, min_protein_g=15.0, top_n=5):
    """降级路径：在当前时段**可售**的餐品里，找最低钠的主食。

    为什么需要它：营养表是全国全时段全集（158 条），门店菜单是分时段子集
    （实测 91 件）。凌晨时段最低钠的 8 个主食里可能一件都点不到。
    只报"不可售"虽然诚实，但对用户没用；所以这里退一步，
    在"能点到的东西里"找最优，并明确标注这是降级结果。
    """
    cand = [it for it in items if it.protein >= min_protein_g]
    cand.sort(key=lambda i: i.sodium)
    out = []
    for it in cand:
        entry = match_product(it.productName, menu_index)
        if entry:
            out.append((it, entry))
            if len(out) >= top_n:
                break
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CardioMCD 端到端真实链路（只读）")
    ap.add_argument("--profile", choices=sorted(PROFILE_PRESETS), default="general",
                    help="人群预设，默认 general（正常人群，无需声明病史）")
    ap.add_argument("--meal", choices=list(VALID_MEALS), default="lunch")
    ap.add_argument("--hypertension", action="store_true", help="在预设基础上追加")
    ap.add_argument("--diabetes", action="store_true", help="在预设基础上追加")
    ap.add_argument("--hyperlipidemia", action="store_true", help="在预设基础上追加")
    ap.add_argument("--ckd", action="store_true", help="在预设基础上追加")
    ap.add_argument("--city", default="长沙")
    ap.add_argument("--keyword", default="湘雅医院")
    ap.add_argument("--top", type=int, default=8, help="展示最低钠主食的前 N 个")
    ap.add_argument("--offline", action="store_true", help="不联网，只演示映射逻辑")
    args = ap.parse_args()

    # 预设 + 开关取并集（与 run_demo.py 共用 build_profile，保证两个入口行为一致）
    extra = [f for f in EXTRA_FLAGS if getattr(args, f)]
    profile = build_profile(args.profile, extra, meal=args.meal)
    meal_cn = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}

    # ---- 1) 营养数据 ----
    if args.offline:
        token = None
    else:
        token = os.environ.get("MCD_TOKEN")
        if not token:
            print("错误：未设置环境变量 MCD_TOKEN。可加 --offline 演示映射逻辑。",
                  file=sys.stderr)
            return 1

    if args.offline:
        fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "tests", "fixture_nutrition.txt")
        with open(fixture, "r", encoding="utf-8") as f:
            payload = f.read()
        print("数据来源：本地夹具（离线演示）")
    else:
        payload = call_list_nutrition_foods(token)
        print("数据来源：麦当劳 MCP · list-nutrition-foods（实时）")

    items = parse_nutrition_payload(payload)
    budgets = resolve_budgets(profile)
    print(f"解析餐品数：{len(items)}")
    print(f"人群画像：{PRESET_LABEL[args.profile]}"
          + (f" ＋ 追加：{'、'.join(FLAG_LABEL[f] for f in extra)}" if extra else ""))
    print(f"餐次：{meal_cn[args.meal]}｜每日钠预算 {budgets.daily_sodium_mg:.0f} mg"
          f"｜单餐钠预算 {budgets.meal_sodium_mg:.0f} mg")
    print()

    # ---- 2) 门店 + 菜单 ----
    menu_index = {}
    store_name = ""
    if args.offline:
        # 离线：用一份真实菜单快照的最小子集演示映射
        menu_index = {
            "圆筒冰淇淋": {"code": "515837", "name": "圆筒冰淇淋", "price": "5"},
            "苹果片一袋(60g)": {"code": "6102", "name": "苹果片一袋(60g)", "price": "7"},
            "纯牛奶": {"code": "3755", "name": "纯牛奶", "price": "11"},
            "纯悦": {"code": "3757", "name": "纯悦", "price": "7.5"},
            "麦辣鸡腿汉堡": {"code": "1440", "name": "麦辣鸡腿汉堡", "price": "22.5"},
            "板烧鸡腿堡": {"code": "1406", "name": "板烧鸡腿堡", "price": "23.5"},
        }
        print("门店：（离线演示，使用菜单快照子集）")
    else:
        stores = mcp_call(token, "query-nearby-stores",
                          {"beType": 1, "searchType": 2,
                           "city": args.city, "keyword": args.keyword})
        store_list = stores.get("data", stores) if isinstance(stores, dict) else stores
        if isinstance(store_list, dict):
            store_list = store_list.get("data", [])
        if not store_list:
            print(f"未在「{args.city} / {args.keyword}」附近查到门店，改用收藏门店重试。")
            store_list = mcp_call(token, "query-nearby-stores",
                                  {"beType": 1, "searchType": 1})
            if isinstance(store_list, dict):
                store_list = store_list.get("data", [])
        store = store_list[0]
        store_code = store["storeCode"]
        store_name = store.get("storeName", "")
        print(f"最近门店：{store_name}（storeCode={store_code}，"
              f"距离 {store.get('distance', '—')} m）")

        # 门店会打烊：菜单可能为空。逐家试，取第一家仍有可售餐品的门店。
        # （实测：凌晨 01:00 后"长沙华远华时代餐厅"已打烊，菜单返回 0 件）
        menu_index = {}
        for st in store_list:
            menu = mcp_call(token, "query-meals",
                            {"storeCode": st["storeCode"], "orderType": 1, "beType": 1})
            idx = build_menu_index(menu)
            if idx:
                store_code = st["storeCode"]
                store_name = st.get("storeName", "")
                menu_index = idx
                if st is not store_list[0]:
                    print(f"↳ 该门店当前无可售餐品（可能已打烊），改用：{store_name}"
                          f"（storeCode={store_code}）")
                break
        if not menu_index:
            print("所有候选门店当前均无可售餐品（可能均已打烊），流程终止。")
            return 0
        print(f"门店：{store_name}（storeCode={store_code}）｜当前可售餐品：{len(menu_index)} 件")
    print()

    # ---- 3) 最低钠主食 → 映射到可点单商品 ----
    ranked = best_main(items, budgets, profile, min_protein_g=15.0, top_n=args.top)
    print("=" * 88)
    print(f"相对最低钠的主食（蛋白 ≥15 g）→ 是否可点单 → 真实价格")
    print("=" * 88)
    print(f"{'营养表品名':<26}{'钠mg':>7}{'占单餐':>8}   {'菜单品名':<22}{'编码':<12}{'价':>7}")
    print("-" * 88)

    matched = []
    for s in ranked:
        it = s.item
        entry = match_product(it.productName, menu_index)
        if entry:
            matched.append((s, entry))
            print(f"{it.productName[:24]:<26}{it.sodium:>7.0f}"
                  f"{s.ratios['sodium'] * 100:>7.0f}%   "
                  f"{entry['name'][:20]:<22}{entry['code']:<12}¥{entry['price']:>6}")
        else:
            print(f"{it.productName[:24]:<26}{it.sodium:>7.0f}"
                  f"{s.ratios['sodium'] * 100:>7.0f}%   "
                  f"{'— 当前时段不可售':<22}{'—':<12}{'—':>7}")
    print("-" * 88)
    print(f"可点单：{len(matched)}/{len(ranked)} 件"
          f"（其余为当前时段该门店不供应，这是真实情况，不是匹配失败）")

    # ---- 3b) 降级：当前时段可售中的最低钠主食 ----
    if len(matched) < 2 and not args.offline:
        fallback = orderable_lowest_sodium(items, menu_index, 15.0, top_n=args.top)
        if fallback:
            print()
            print("=" * 88)
            print("降级结果：以上多为「当前时段不可售」。以下改为在**当前能点到**的餐品里"
                  "找最低钠的主食")
            print("=" * 88)
            print(f"{'营养表品名':<26}{'钠mg':>7}   {'菜单品名':<22}{'编码':<12}{'价':>7}")
            print("-" * 88)
            for it, entry in fallback:
                print(f"{it.productName[:24]:<26}{it.sodium:>7.0f}   "
                      f"{entry['name'][:20]:<22}{entry['code']:<12}¥{entry['price']:>6}")
            print("-" * 88)
            matched = [(None, e) for _, e in fallback]

    # ---- 4) 真实计价（只读，不下单）----
    if not matched or args.offline:
        print()
        print("跳过计价：离线模式或无可点单商品。")
        return 0

    # 取钠最低的若干件组成一餐（最多 4 件，控制演示规模）
    basket = matched[:4]
    items_arg = [{"productCode": e["code"], "quantity": 1} for _, e in basket]

    print()
    print("=" * 88)
    print("真实计价（calculate-price，只读；不下单）")
    print("=" * 88)
    price = mcp_call(token, "calculate-price",
                     {"storeCode": store_code, "orderType": 1, "beType": 1,
                      "items": items_arg})
    total_fen = price.get("price", 0)
    print(f"门店：{store_name}")
    for p in price.get("productList", []):
        print(f"  {p['productName']:<24} ×{p['quantity']}   ¥{p['subtotal'] / 100:.2f}")
    print("-" * 88)
    print(f"商品原价 ¥{price.get('productOriginalPrice', 0) / 100:.2f} ｜ "
          f"优惠 ¥{price.get('discount', 0) / 100:.2f} ｜ "
          f"应付 ¥{total_fen / 100:.2f}")
    print()
    print("⚠️ 未调用 create-order —— 本脚本不领券、不下单、不改动账户数据。")
    print("免责声明：本输出为营养数值的相对比较，不构成医疗建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
