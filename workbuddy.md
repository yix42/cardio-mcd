# workbuddy.md

> 使用腾讯 WorkBuddy 开发「心膳 CardioMCD」的对话上下文记录。
> 用于核验麦当劳程序员节创意开发大赛 WorkBuddy 联动奖励资格。

- **开发工具**：腾讯 WorkBuddy（Agent 模式）
- **开发日期**：2026-10-09
- **项目**：心膳 CardioMCD —— 心血管视角的麦当劳点餐顾问

---

## 一、在 WorkBuddy 中接入麦当劳 MCP

用户提供 MCP 配置模板后，WorkBuddy 将其合并写入 `~/.workbuddy/mcp.json`
（保留已有的 simvascular / zotero / zotero-local / consensus / lighthouse-ops 条目），
并以真实 Token 替换占位符。

随后 WorkBuddy 通过 Bash 发起连通性探针，确认链路可用：

```
POST https://mcp.mcd.cn
Authorization: Bearer <token>
{"jsonrpc":"2.0","id":1,"method":"initialize",...}

→ HTTP 200
→ {"result":{"capabilities":{"tools":{}},
   "protocolVersion":"2024-11-05",
   "serverInfo":{"name":"mcd-mcp","version":"1.0.0"}}}
```

---

## 二、WorkBuddy 完成的调研

1. 拉取并解析活动仓库 README、`activityGuidelines.md`、`RANKING.md`
   → 提炼赛制要点（Star 排名、截止 10/26 00:00、强制文件清单、内容合规红线）
2. 拉取 `M-China/mcd-mcp-server` 工具清单（30+ Tools）
3. 分析当时排行榜 60+ 项目的赛道分布
   → 判定卡路里/省钱/点餐/积分四类已红海，**心血管临床视角是空白**

---

## 三、WorkBuddy 纠正的一处设计错误（重要）

初始方案曾把「饱和脂肪」「反式脂肪」列为红绿灯维度。
WorkBuddy 在真实调用 `list-nutrition-foods` 并解析返回结构后发现：

```
[158]{productName,nutritionDescription,energyKj,energyKcal,
      protein,fat,carbohydrate,sodium,calcium}
```

**接口只提供总脂肪 `fat`，不提供饱和/反式脂肪拆分。**

处理方式：删除这两个维度，改为在代码 docstring、README、MCP_INTEGRATION、
以及单元测试 `test_no_saturated_fat_dimension` 中**显式声明不评分**，
不以总脂肪冒充、不臆造数值。

---

## 四、指南依据核对（WorkBuddy 联网检索，非依赖模型记忆）

| 阈值 | 结论 | 来源 |
|---|---|---|
| 一般人群每日钠 2000 mg | 每日食盐 < 5 g | 《中国高血压防治指南(2024年修订版)》 |
| 高血压等人群 1500 mg/d | DASH 低钠版 | DASH 饮食 |
| 减饱和脂肪、避免反式脂肪 | — | 国家卫健委《高血压营养和运动指导原则(2024年版)》 |

---

## 五、WorkBuddy 产出的代码

- `src/cardio_score.py` —— 解析 / 预算 / 判定 / 组合推荐，零第三方依赖
- `tests/test_cardio_score.py` —— 46 项单元测试
- `tests/test_ordering.py` —— 29 项单元测试（品名映射 / 只读边界 / 正常人群入口 / 共享画像构造）
- 测试合计 **75 项**，全部通过；零第三方依赖，Python 3.9+
- `tests/fixture_nutrition.txt` —— MCP 真实返回快照（18 行，含 1 条重复以测去重）
- `demo/run_demo.py` —— 真实调用 MCP 的可运行演示

真实运行结果（高血压 + 早餐，实时数据）：

```
数据来源：麦当劳 MCP · list-nutrition-foods（实时）
解析餐品数：158

钠预算锚点：DASH 低钠版 1500 mg/d（高血压/心衰/肾病/盐敏感）
每日预算：钠 1500 mg ｜ 能量 2000 kcal ｜ 脂肪 66.7 g ｜ 碳水 275 g
餐次：早餐（钠权重 20%）→ 单餐钠预算 300 mg
单餐预算：能量 600 kcal ｜ 脂肪 20.0 g ｜ 碳水 82 g

芝士双层安格斯厚牛堡  红灯  1373 mg（占单餐 458%｜占每日 92%）
麦辣鸡腿汉堡          红灯  1208 mg（占单餐 403%｜占每日 81%）

相对最低钠的主食（蛋白 ≥ 15 g）：
  冰牛奶大杯    钠 154 mg（51%）
  儿童鱼排堡    钠 442 mg（147%）—— 单品即超早餐预算
  烟肉蛋麦满分  钠 483 mg（161%）
```

测试：`Ran 75 tests ... OK`

> ⚠️ 迭代中修正的一处设计错误：早期版本把每日钠预算 ÷3 得单餐预算。
> 该假设无依据且生理上不成立（早餐有能量但钠负荷远低于午晚餐），
> 已改为「餐次显式化」：钠权重 20/40/40/10 与能量权重 30/40/30/10 分离，
> 未指定餐次时不做任何分配、拒绝给出组合推荐。

---

## 六、WorkBuddy 参与的具体环节

| 环节 | WorkBuddy 是否参与 |
|---|:---:|
| MCP 配置与连通性验证 | ✅ |
| 赛制 / 工具 / 竞争格局调研 | ✅ |
| MCP 返回结构探测与解析实现 | ✅ |
| 指南依据联网核对 | ✅ |
| 评分内核编码 | ✅ |
| 单元测试编写与执行 | ✅ |
| 真实 MCP 联调演示 | ✅ |
| README / MCP_INTEGRATION 文档撰写 | ✅ |

本项目全程在 WorkBuddy 中完成开发、调试与验证。
