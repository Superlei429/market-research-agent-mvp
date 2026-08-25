# 市场调研 Agent（MVP）

> Agent① 市场调研 Agent MVP · DeepSeek + Tavily
> 宠物食品（中国猫粮市场）智能调研，基于 function calling 自动联网检索，输出严格结构化 JSON。

## 功能特性

- **DeepSeek 驱动**：通过 OpenAI SDK 兼容接口调用 `deepseek-chat`，强制 `json_object` 输出
- **Tavily 联网检索**：function calling 自动调用 `web_search` 工具，实时获取最新行业数据
- **严格 JSON 输出**：失败自动正则提取 + 最多 3 次重试，每次重试的原始输出落盘留痕
- **完整可观测**：控制台 + 文件双重日志，最终结果与原始输出保存到 `logs/market_research/`

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
└── .env.example        # 环境变量模板
```
