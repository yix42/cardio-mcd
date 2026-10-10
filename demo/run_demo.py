#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_demo.py — 真实调用麦当劳 MCP 的 `list-nutrition-foods`，输出心血管红绿灯报告。

用法（**正常人群是默认入口**，不需要声明任何病史）：

    export MCD_TOKEN="你的MCP Token"

    # ① 正常人群 —— 直接跑，零门槛
    python demo/run_demo.py --meal lunch                     # 默认 --profile general
    python demo/run_demo.py --meal breakfast

    # ② 心血管风险人群 —— 用预设或单独开关
    python demo/run_demo.py --profile hypertension --meal lunch
    python demo/run_demo.py --profile cardio   --meal breakfast   # 高血压 + 高血脂
    python demo/run_demo.py --meal breakfast --hypertension --diabetes

    # ③ 不指定餐次 / 不联网
    python demo/run_demo.py                                  # 只按占每日钠预算判定
    python demo/run_demo.py --offline                        # 用 tests/fixture_nutrition.txt

⚠️ 餐次很重要：指南只给"每日"钠目标，不规定三餐占比，而早餐的钠负荷
明显低于午晚餐。不传 --meal 时本脚本不做任何餐次分配，只报告"占每日预算比例"；
传了 --meal 才核算单餐钠预算，也才能给出组合推荐。

本脚本只做查询与核算，不下单、不改动账户数据。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cardio_score import (  # noqa: E402
    VALID_MEALS,
    best_main,
    parse_nutrition_payload,
    recommend,
    render_report,
    resolve_budgets,
    score_menu,
)

MCP_URL = "https://mcp.mcd.cn"
ORIGINAL_RESPONSE_MARKER = "## Original Response"

# --- 人群预设：正常人群是默认入口，不要求用户声明任何病史 -------------------
PROFILE_PRESETS = {
    "general":      {},
    "hypertension": {"hypertension": True},
    "diabetes":     {"diabetes": True},
    "lipid":        {"hyperlipidemia": True},
    "renal":        {"ckd": True},
    "cardio":       {"hypertension": True, "hyperlipidemia": True},
}

PRESET_LABEL = {
    "general":      "正常人群（未声明任何病史；钠 2000 mg/d）",
    "hypertension": "高血压（钠收紧至 DASH 低钠版 1500 mg/d）",
    "diabetes":     "糖尿病（钠 2000 mg/d，启用碳水维度）",
    "lipid":        "高脂血症（钠 2000 mg/d，脂肪预算 ×0.8）",
    "renal":        "慢性肾病（钠收紧至 1500 mg/d）",
    "cardio":       "高血压 + 高脂血症（钠 1500 mg/d，脂肪 ×0.8）",
}

# 可在预设基础上叠加的独立开关
EXTRA_FLAGS = ("hypertension", "diabetes", "hyperlipidemia", "ckd")

FLAG_LABEL = {
    "hypertension": "高血压",
    "diabetes": "糖尿病",
    "hyperlipidemia": "高脂血症",
    "ckd": "慢性肾病",
}


def build_profile(preset: str = "general", extra=None, meal: str = None) -> dict:
    """人群预设 + 追加开关 + 餐次 → 标准 profile 字典。

    抽成函数的唯一理由：**run_demo.py（评分）与 run_ordering.py（点单）必须共用
    同一份画像构造逻辑**。否则会出现「评分脚本认得 --profile cardio、点单脚本不认」
    这类两个入口对不上的问题，而这类不一致在演示时最难发现。
    """
    p = dict(PROFILE_PRESETS[preset])
    for flag in (extra or ()):
        p[flag] = True
    return {
        "hypertension": bool(p.get("hypertension")),
        "diabetes": bool(p.get("diabetes")),
        "hyperlipidemia": bool(p.get("hyperlipidemia")),
        "ckd": bool(p.get("ckd")),
        "meal": meal,
    }


def _extract_json(raw: str) -> dict:
    """Streamable HTTP 可能返回 SSE（`data: {...}`）或裸 JSON，两种都要能处理。"""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    return json.loads(raw)


def _unwrap_mcp_text(text: str) -> str:
    """MCP 返回的 content[0].text 是一段带说明的 Markdown，真正的业务 JSON 在
    `## Original Response` 之后。这里把它解出来并取出 data 字符串。"""
    idx = text.find(ORIGINAL_RESPONSE_MARKER)
    tail = text[idx + len(ORIGINAL_RESPONSE_MARKER):] if idx >= 0 else text
    start = tail.find("{")
    if start < 0:
        raise RuntimeError("未在 MCP 返回中定位到业务 JSON")
    obj, _ = json.JSONDecoder().raw_decode(tail[start:])
    data = obj.get("data")
    if not isinstance(data, str):
        raise RuntimeError(f"data 字段类型异常：{type(data)}")
    return data


def call_list_nutrition_foods(token: str, timeout: int = 30) -> str:
    """调用 tools/call → list-nutrition-foods，返回 data 字符串。"""
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list-nutrition-foods", "arguments": {}},
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
        raise RuntimeError(f"MCP 返回为空：{raw[:300]}")
    return _unwrap_mcp_text(content[0].get("text", ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="CardioMCD 真实 MCP 演示")
    ap.add_argument("--offline", action="store_true", help="不联网，使用本地夹具")
    ap.add_argument("--profile", choices=sorted(PROFILE_PRESETS), default="general",
                    help="人群预设，默认 general（正常人群，无需声明病史）")
    ap.add_argument("--hypertension", action="store_true", help="在预设基础上追加")
    ap.add_argument("--diabetes", action="store_true", help="在预设基础上追加")
    ap.add_argument("--hyperlipidemia", action="store_true", help="在预设基础上追加")
    ap.add_argument("--ckd", action="store_true", help="在预设基础上追加")
    ap.add_argument("--meal", choices=list(VALID_MEALS), default=None,
                    help="餐次；不传则不分配钠预算，改按占每日预算比例判定")
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args()

    # 预设 + 开关取并集：既不强迫健康人声明病史，也允许风险人群细调
    extra = [f for f in EXTRA_FLAGS if getattr(args, f)]
    profile = build_profile(args.profile, extra, meal=args.meal)

    if args.offline:
        fixture = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "tests", "fixture_nutrition.txt"
        )
        with open(fixture, "r", encoding="utf-8") as f:
            payload = f.read()
        source = "本地夹具 tests/fixture_nutrition.txt"
    else:
        token = os.environ.get("MCD_TOKEN")
        if not token:
            print("错误：未设置环境变量 MCD_TOKEN。", file=sys.stderr)
            print("  export MCD_TOKEN='你的MCP Token'  或加 --offline 用夹具演示", file=sys.stderr)
            return 1
        payload = call_list_nutrition_foods(token)
        source = "麦当劳 MCP · list-nutrition-foods（实时）"

    items = parse_nutrition_payload(payload)
    budgets = resolve_budgets(profile)
    scores = score_menu(items, budgets, profile)

    print(f"数据来源：{source}")
    print(f"人群画像：{PRESET_LABEL[args.profile]}")
    print(f"解析餐品数：{len(items)}")
    print()
    print(render_report(scores, budgets, profile, limit=args.limit))

    # ---- 预算内组合推荐（必须指定餐次；否则无单餐钠预算可比）----
    print()
    print("=" * 78)
    if budgets.meal_sodium_mg is None:
        print("组合推荐：跳过")
        print("=" * 78)
        print("未指定餐次 → 没有单餐钠预算，无法判断是否超标。")
        print("加 --meal breakfast|lunch|dinner|snack 后再运行即可获得组合推荐。")
        print("（这是刻意设计：不指定餐次就偷偷按三餐均分，会系统性高估早餐钠空间。）")
    else:
        print("预算内组合推荐（蛋白 ≥ 12 g、能量 ≥ 250 kcal；钠占单餐预算升序）")
        print("=" * 78)
        top = recommend(items, budgets, profile, size=3, top_n=3, require_protein_g=12.0)
        if not top:
            print("未找到同时满足预算与蛋白门槛的组合，可下调 require_protein_g。")
        for ev, protein in top:
            names = " + ".join(i.productName for i in ev.items)
            print(
                f"  {names}\n"
                f"    钠 {ev.total_sodium_mg:.0f} mg（占单餐预算 {ev.sodium_pct_of_meal_budget*100:.0f}%）｜"
                f" 能量 {ev.total_energy_kcal:.0f} kcal｜"
                f" 脂肪 {ev.total_fat_g:.0f} g｜"
                f" 蛋白 {ev.total_protein_g:.0f} g"
            )

    # ---- 相对最低钠的主食 ----
    sodium_ref = "占单餐预算" if budgets.meal_sodium_mg is not None else "占每日预算"
    print()
    print("=" * 78)
    print(f"相对最低钠的主食（蛋白 ≥ 15 g，按钠升序；{sodium_ref}）")
    print("=" * 78)
    if budgets.meal_sodium_mg is not None:
        print("说明：收紧钠预算后，多数主菜单品即超出单餐预算；此处如实给出相对最优解，")
        print("      占比 >100% 表示『单品已超单餐钠预算』。")
    else:
        print("说明：未指定餐次，此处占比为『占每日钠预算』，不涉及单餐是否超标。")
    for s in best_main(items, budgets, profile, min_protein_g=15.0, top_n=5):
        it = s.item
        pct = s.ratios["sodium"] * 100
        print(
            f"  {it.productName}\n"
            f"    钠 {it.sodium:.0f} mg（{pct:.0f}%）｜"
            f" 能量 {it.energyKcal:.0f} kcal｜"
            f" 脂肪 {it.fat:.0f} g｜"
            f" 蛋白 {it.protein:.0f} g"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
