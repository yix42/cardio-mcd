# MCP_INTEGRATION

本文档说明 CardioMCD 实际使用的麦当劳 MCP Server、Tools、调用流程与业务价值。

## 1. 使用的 MCP Server

| 项目 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer ${MCD_TOKEN}`（环境变量注入，仓库内不含真实 Token） |
| 官方指南 | https://github.com/M-China/mcd-mcp-server |
| 限流 | 600 次/分钟 |

配置文件见 [`mcp-config.example.json`](./mcp-config.example.json)。

---

## 2. 使用的 Tools

### 2.1 代码直接调用（已实现并验证）

| Tool | 用途 | 在本项目中的作用 |
|---|---|---|
| `list-nutrition-foods` | 获取餐品营养成分列表 | **核心数据源**。返回全部餐品的能量、蛋白质、脂肪、碳水、钠、钙，是红绿灯评分的唯一输入 |

调用实现见 [`demo/run_demo.py`](./demo/run_demo.py) 的 `call_list_nutrition_foods()`：

```python
body = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/call",
    "params": {"name": "list-nutrition-foods", "arguments": {}}
})
# POST https://mcp.mcd.cn
# headers: Authorization: Bearer ${MCD_TOKEN}
#          Accept: application/json, text/event-stream
```

**返回结构的关键点**（踩过的坑，写在这里免得后人重踩）：

1. `result.content[0].text` **不是**业务数据本身，而是一段带字段说明的 Markdown；
   真正的业务 JSON 在 `## Original Response` 标记之后，需要用 `raw_decode` 解出。
2. 业务 JSON 的 `data` 字段是 **string**，不是数组。格式为：

   ```
   [158]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
     猪柳麦满分,null,1288,308,16,16,24,781,213
     中薯条,null,1210,289,4,12,38,165,18
   ```

   → 首行是头部（含条目数与字段名），必须跳过；
   → 数值固定为尾部 8 个字段，品名可能含逗号，因此**必须用尾部切分**而非 `split(",")[0]`；
   → 存在**重复品名**（如"纯牛奶（盒装）"出现两次），需按品名去重。
   解析器见 [`src/cardio_score.py`](./src/cardio_score.py) 的 `parse_nutrition_payload()`，
   并有对应单元测试覆盖。

3. **字段只有总脂肪，没有饱和/反式脂肪拆分** —— 本项目因此不评这两个维度，
   不以总脂肪替代，见 README「关于缺失维度」。

### 2.2 已实调验证（2026-10-10）

以下工具已通过 `demo/run_ordering.py` **真实调用并跑通**，返回结构与踩坑均已核对。

| Tool | 用途 | 实调结果 |
|---|---|---|
| `query-nearby-stores` | 查询附近门店，拿 `storeCode` | ✅ 长沙湘雅附近返回 5 家门店；最近「长沙华远华时代餐厅」`storeCode=3390240`，307 m |
| `query-meals` | 查询门店当前可售餐品 + `productCode` + 现价 | ✅ 该门店返回 **91 件**（分 12 个分类） |
| `calculate-price` | 真实计价（含优惠） | ✅ 4 件合计 **3050 分 = ¥30.50**，优惠 ¥0 |
| `available-coupons` | 查询可领取券 | ✅ 返回 7 张券（巧克力味厚松饼猪柳蛋套餐、免费脆薯饼、9.9 元中杯冰美式等） |
| `query-my-coupons` | 查询卡包中的券 | ✅ 当前卡包为空 → 无券可带入计价 |

实调中发现的四个真实坑（已写进代码注释，勿重踩）：

1. **营养表品名 ≠ 菜单品名，且不是一一对应。**
   - 营养表 `list-nutrition-foods` 返回 **158 条**（全国全时段全集）；
   - 门店 `query-meals` 只返回**当前时段可售**的子集（实测 91 件）。
   - 实测：凌晨时段「麦香鱼」「儿童鱼排堡」「玉米杯」等**均不可售**，
     最低钠的 8 个主食里**只有 1 件能点单** —— 这是真实情况，不是匹配失败。
   - 品名还带规格后缀：营养表「苹果片」↔ 菜单「苹果片一袋(60g)」；
     营养表「纯牛奶（盒装）」↔ 菜单「纯牛奶」。
   → 因此映射**不能用字符串精确相等**，本项目用三级匹配：
     精确 → 去括号后缀后精确 → 双向前缀包含（长度 ≥2，**命中多个时取名字最短**）。
     取最短这一步是必要的：否则「吉士汉堡包」会匹配到「吉士汉堡包套餐」（¥26 套餐），
     而用户要的是单品。见 `demo/run_ordering.py` 的 `match_product()`，
     `tests/test_ordering.py` 有 29 项测试覆盖，并额外锁死"单品不能落到套餐上"的虚假匹配。

2. **`calculate-price` 返回的金额单位是「分」**，展示需 ÷100。

3. **`query-meals` / `calculate-price` 都必须先拿到 `storeCode`**，
   需先调 `query-nearby-stores`（到店，beType=1）或 `delivery-query-stores`（外送）。

4. **`available-coupons` 只返回券标题 / 图片 / 状态，不含 `couponId`**，
   无法直接带入 `calculate-price`。本项目**不自动领券**（`auto-bind-coupons` 会改动账户），
   因此当前"优惠券优化"为**只展示不可用**——如实写明，不假装能用。

5. **门店会打烊，菜单可能返回 0 件。** 实测凌晨 01:00 后「长沙华远华时代餐厅」
   （营业至 01:00）菜单为空。本项目逐家试候选门店，取第一家仍有可售餐品的
   （自动切到 24h 的「培元桥地铁站餐厅」）。

### 2.3 Skill 编排层调用（由 Agent 按需触发）

以下工具在 WorkBuddy Skill 编排中被调用。编排提示词见 [`src/skill_prompt.md`](./src/skill_prompt.md)。

| Tool | 用途 | 触发时机 |
|---|---|---|
| `query-store-coupons` | 查询当前门店/订单可用券 | 在预算内组合中再做一次花费优化 |
| `now-time-info` | 获取当前时间 | 判断是否处于早餐/正餐时段，用于选择对应菜单 |
| `query-meal-detail` | 查询餐品详情与可替换项 | 需要替换套餐内单品（如去掉酱料）时 |
| `create-order` | 下单 | **仅在用户明确确认后**，由 Agent 调用；本仓库任何脚本都不会自动调用 |

### 2.4 只读安全边界

`demo/run_ordering.py` 内置 `READ_ONLY_TOOLS` 白名单，**白名单外的工具一律拒绝调用**：

```
允许：list-nutrition-foods / query-nearby-stores / query-meals / query-meal-detail /
      calculate-price / available-coupons / query-my-coupons / query-store-coupons /
      query-promotions / campaign-calendar / query-my-account / now-time-info
拒绝：create-order / auto-bind-coupons / draw-lottery / mall-create-order / cancel-order
```

**本项目的任何脚本都不领券、不下单、不改动账户数据。**
`TestReadOnlyGuard` 用 5 条测试把这条边界锁死。

---

## 3. 调用流程

```
用户画像（高血压 / 高血脂 / 糖尿病 / 肾病 / 盐敏感）
        │
        ▼
用户餐次（breakfast / lunch / dinner / snack）── 必须显式指定
        │
        ▼
resolve_budgets()
        ├─ 每日锚点：一般人群 钠 2000 mg/d；高血压/心衰/肾病/盐敏感 → DASH 低钠版 1500 mg/d
        └─ 餐次权重：钠 20/40/40/10，能量 30/40/30/10  ⚠️ 两者不相等
              例：高血压 + 早餐 → 1500 × 20% = 300 mg
                  高血压 + 午餐 → 1500 × 40% = 600 mg
        │
        │  （未指定餐次 → 不做餐次分配，钠按占每日预算判定，拒绝给出组合推荐）
        ▼
list-nutrition-foods  ──► parse_nutrition_payload()  (158 条真实餐品)
        │
        ▼
score_menu()  ── 逐项判定：钠 / 能量 / 脂肪（+ 碳水，仅糖尿病）
        │        短板原则取最差维度 → 🟢 / 🟡 / 🔴
        ├──► render_report()        红绿灯清单（同时给出占单餐 / 占每日两个比例）
        ├──► recommend()            预算内组合（需指定餐次；满足蛋白与能量下限）
        └──► best_main()            相对最低钠的主食（如实标注 >100%）
        │
        ▼
（已实调）query-nearby-stores → query-meals → 品名三级匹配 → calculate-price  (91 件可售)
        │
        ▼
输出：推荐组合 + 风险提示 + 真实价格（¥，只读，不下单）
```

> ⚠️ **营养表与菜单之间存在真实的"时段缺口"**：
> 营养表是全国全时段全集（158 条），门店菜单是分时段子集（实测 91 件）。
> 实测凌晨时段，最低钠的 8 个主食里只有 1 件能点单。
> 本项目如实报告"当前时段不可售"，而不是假装它可点。

---

## 4. 业务价值

1. **把"查营养"变成"管风险"**
   现有工具回答"这个多少卡"，CardioMCD 回答"这一份就占掉你早餐钠预算的 403%"。
   对高血压人群，后者才是决策依据。

2. **动态预算而非固定阈值**
   同一份餐品，在一般人群画像下可能是黄灯，在高血压画像下直接变红灯 —— 阈值随画像收紧，
   而不是给所有人同一个数字。

3. **餐次显式化，拒绝无依据的均分假设**
   指南只给每日目标，不规定三餐占比；而早餐钠负荷远低于午晚餐。
   本项目让餐次成为必填维度，钠与能量使用不同权重，未指定餐次时拒绝给组合推荐。
   实测差异：儿童鱼排堡 442 mg —— 早餐 147%（红灯）／午餐 74%（黄灯）。
   按 ÷3 均分只会得出"88%"这个对现实没有指导价值的中间数。

4. **如实暴露数据边界**
   MCP 不返回饱和/反式脂肪，就不评分，并且用测试把这件事锁死。
   营养工具最怕的不是少一个维度，而是**用一个维度冒充另一个维度**。

5. **只读、可审计**
   整个评分链路只用了一个只读查询工具，不下单、不改动用户账户数据，
   任何一步的数值都能回溯到 MCP 原始返回与指南原文。

---

## 5. 合规说明

- 仓库内**不含真实 Token**，`mcp-config.example.json` 仅使用 `${MCD_TOKEN}` 占位符。
- 输出定位为「均衡膳食前提下偶尔外食的相对更优选择」，不鼓励过量或高频消费。
- 输出不构成医疗或营养建议，不替代医师诊疗。
