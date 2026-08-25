# 市场调研 Agent（MVP）

> Agent① 市场调研 Agent MVP · DeepSeek + Tavily
> 宠物食品（中国猫粮市场）智能调研，基于 function calling 自动联网检索，输出严格结构化 JSON。

## 功能特性

- **DeepSeek 驱动**：通过 OpenAI SDK 兼容接口调用 `deepseek-chat`，强制 `json_object` 输出
- **Tavily 联网检索**：function calling 自动调用 `web_search` 工具，实时获取最新行业数据
- **严格 JSON 输出**：失败自动正则提取 + 最多 3 次重试，每次重试的原始输出落盘留痕
- **完整可观测**：控制台 + 文件双重日志，最终结果与原始输出保存到 `logs/market_research/`

## 调研框架（System Prompt）

1. **市场趋势**：近 6 个月增长最快的细分品类及驱动因素
2. **消费者画像**：核心购买人群特征、痛点、决策路径
3. **价格带分析**：市场份额、代表品牌、利润空间估算
4. **热门成分**：TOP5 功能性成分及科学依据强度
5. **赛道推荐**：最优切入赛道及理由

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
├── agent.py            # Agent 主逻辑（function calling + JSON 重试）
├── config.yaml         # DeepSeek 模型 / Tavily 工具 / 执行参数
├── prompt.md           # System Prompt 模板
├── tools/
│   └── web_search.py   # Tavily 搜索工具封装
├── requirements.txt
├── .env.example        # 环境变量模板
└── logs/market_research/   # 运行日志与输出（已 gitignore）
```

## 输出示例

运行后会打印完整 JSON，包含 `trends` / `personas` / `price_bands` / `hot_ingredients` / `recommendation` / `data_gaps` 六个字段，示例结果保存在 `logs/market_research/result_*.json`。

## 免责声明

- 所有数据均标注来源与时间，无可靠来源标注"数据缺失"，不编造统计
- 项目仅供研究参考，不构成投资或商业决策建议
