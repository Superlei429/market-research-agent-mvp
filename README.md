# 市场调研 Agent（Agent① · v2 分维度多轮搜索）

> Agent① 市场调研 Agent · DeepSeek + Tavily
> 宠物食品（中国猫粮市场）智能调研，采用**分维度多轮搜索**策略自动联网检索，输出对齐下游 Agent② 的严格结构化 JSON。

## 功能特性

- **分维度多轮搜索**：7 个搜索维度 × 每维度 2 个关键词组合 = **14+ 次搜索**（不再依赖模型自行决定搜索 1-2 次）
  - `market_size` 市场规模 / `consumer_trends` 消费趋势 / `ingredient_trends` 成分趋势 / `competitor_analysis` 竞品 / `pain_points` 痛点差评 / `regulatory` 法规 / `functional_needs` 功能需求
  - 搜索词模板支持 `{year}` 占位符，由 `config.search.year` 渲染
- **URL 去重**：`seen_urls` 集合跳过重复来源，避免信息冗余
- **调研档案（Dossier）**：snippet 按维度归类汇总，连同**竞品锚点库**一起喂给模型
- **思维链升级**：Prompt 强制 `<analysis>` 推理（数据盘点→交叉验证→趋势判断→机会识别→风险标注→配方推导）+ `<self_check>` 自我反思
- **输出 Schema 升级**：`meta` / `market_overview` / `consumer_insights` / `ingredient_trends` / `competitor_landscape` / `formulation_brief` / `data_gaps` / `confidence_notes`，对齐 Agent② 输入
- **严格 JSON 输出**：平衡括号算法提取 JSON 块 + 最多 3 次重试；`<analysis>` / `<self_check>` 单独落盘留痕
- **完整可观测**：控制台 + 文件双重日志，原始输出 / 思维链 / 自省 / 最终结果均保存到 `logs/market_research/`

## 搜索策略（config.search）

```yaml
search:
  dimensions: [market_size, consumer_trends, ingredient_trends, competitor_analysis, pain_points, regulatory, functional_needs]
  queries_per_dimension: 2      # 每维度搜索次数
  max_results_per_query: 5      # 每次搜索返回条数
  dedup: true                   # 是否 URL 去重
  year: 2026                    # 目标年份（渲染 {year}）
  dimension_queries: {...}      # 各维度关键词模板
```

## 输出 Schema

```json
{
  "meta": {"generated_at": "", "search_queries_used": [], "sources_count": 0, "confidence_level": ""},
  "market_overview": {"total_market_size": {}, "growth_rate": {}, "premium_segment_share": {}, "key_drivers": [], "key_challenges": []},
  "consumer_insights": {"top_needs": [], "pain_points": [], "purchase_decision_factors": [], "price_sensitivity": {}, "information_channels": []},
  "ingredient_trends": {"rising": [], "stable": [], "declining": [], "functional_demand": []},
  "competitor_landscape": {"top_players": [], "market_gaps": [], "differentiation_opportunities": []},
  "formulation_brief": {"recommended_positioning": "", "target_price_per_kg": {}, "must_have_features": [], "nice_to_have_features": [], "avoid": [], "target_cat_profile": {}, "differentiation_statement": ""},
  "data_gaps": [],
  "confidence_notes": []
}
```

`formulation_brief` 是下游配方 Agent② 的核心输入，Agent 会重点产出该字段。

## 快速开始

```bash
# 1. 准备环境（Python 3.12+）
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 和 TAVILY_API_KEY

# 3. 运行调研
./.venv/bin/python agent.py --query "分析2026年中国中高端猫粮市场机会"
```

## 目录结构

```
market_research/
├── agent.py                    # Agent 主逻辑（分维度搜索 + 档案构建 + JSON 重试）
├── config.yaml                 # DeepSeek 模型 / 搜索策略 / 竞品锚点路径 / 执行参数
├── prompt.md                   # System Prompt（思维链 + 自省 + 升级版 Schema）
├── data/
│   └── competitor_anchors.yaml # 竞品锚点库（人工维护，模型对比基准）
├── tools/
│   └── web_search.py           # Tavily 搜索工具封装
├── requirements.txt
├── .env.example                # 环境变量模板
└── logs/market_research/       # 运行日志与输出（已 gitignore）
```

## 运行产物

每次运行会在 `logs/market_research/` 生成：
- `agent_*.log` — 完整运行日志（搜索执行 / 去重 / 重试）
- `raw_output_attempt_*.txt` — 模型原始输出
- `analysis_attempt_*.md` — `<analysis>` 思维链
- `self_check_attempt_*.md` — `<self_check>` 自我反思
- `result_*.json` — 最终结构化结果

## 免责声明

- 所有数据均标注来源与时间，无可靠来源标注 `data_not_found`，不编造统计
- 竞品锚点库数据来自公开测评与电商页面，需定期人工更新
- 项目仅供研究参考，不构成投资或商业决策建议
