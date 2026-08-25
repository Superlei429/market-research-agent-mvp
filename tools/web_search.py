# -*- coding: utf-8 -*-
"""
web_search.py —— Tavily 网页搜索工具封装

将 Tavily Search API 封装为可供大模型 function calling 调用的工具函数，
返回结构化摘要（标题 / 链接 / 正文片段），不返回原始 HTML。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from tavily import TavilyClient

# 模块级日志器
logger = logging.getLogger("market_research.web_search")

# 单次搜索默认超时（秒）
DEFAULT_TIMEOUT = 30


def search(
    query: str,
    max_results: int = 5,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    执行一次网页搜索并返回结构化摘要。

    参数:
        query:       搜索关键词
        max_results: 返回结果条数（默认 5）
        api_key:     Tavily API Key；为 None 时从环境变量 TAVILY_API_KEY 读取
        timeout:     单次请求超时秒数（含异常保护）

    返回:
        dict，结构如下：
        {
            "query":   "原始搜索词",
            "results": [
                {"title": "标题", "url": "链接", "content": "正文摘要片段"},
                ...
            ],
            "total_results": 实际返回条数
        }

    说明:
        正常情况下不抛异常；任何失败都会返回带 "error" 字段的 dict，
        由调用方自行判断。
    """
    # 优先使用显式传入的 key，其次环境变量
    resolved_api_key = api_key or os.getenv("TAVILY_API_KEY")
    if not resolved_api_key:
        return {
            "query": query,
            "results": [],
            "error": "缺少 Tavily API Key（参数未传，环境变量 TAVILY_API_KEY 也未设置）",
        }

    try:
        # 使用 tavily-python SDK 创建客户端
        client = TavilyClient(api_key=resolved_api_key)

        # timeout 由 SDK 内部控制，超出后抛出异常进入 except 分支
        response = client.search(
            query=query,
            max_results=max_results,
            timeout=timeout,
        )

        # 提取结构化摘要，只保留标题/链接/正文片段，丢弃原始 HTML 等冗余字段
        results: list[Dict[str, str]] = []
        for item in (response or {}).get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                }
            )

        logger.info("搜索成功：query=%r 返回 %d 条结果", query, len(results))
        return {
            "query": query,
            "results": results,
            "total_results": len(results),
        }

    except Exception as exc:
        # 异常处理：超时、网络错误、鉴权失败等统一捕获
        logger.exception("搜索失败：query=%r", query)
        return {
            "query": query,
            "results": [],
            "error": f"搜索异常：{type(exc).__name__}: {exc}",
        }


# 工具声明：供 OpenAI 兼容的 function calling 使用
TOOL_DEFINITION: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "使用 Tavily 搜索引擎检索最新网页信息，返回结构化结果"
            "（标题、链接、正文摘要），适用于市场趋势、行业数据等外部信息查询。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要搜索的关键词或问题",
                },
                "max_results": {
                    "type": "integer",
                    "description": "返回结果条数，默认 5",
                },
            },
            "required": ["query"],
        },
    },
}
