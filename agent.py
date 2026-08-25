# -*- coding: utf-8 -*-
"""
agent.py —— 宠物食品市场调研 Agent（DeepSeek + Tavily）

功能：
1. 从 config.yaml 读取 DeepSeek 模型配置（api_key 来自环境变量 DEEPSEEK_API_KEY）
2. 使用 OpenAI Python SDK（兼容 DeepSeek API）进行对话
3. 通过 function calling 调用 Tavily 网页搜索工具
4. 强制 JSON 结构化输出：
   a. 优先使用 response_format=json_object
   b. 若返回非纯 JSON，用正则提取第一个 {...} 块
   c. 解析失败自动重试（最多 3 次），每次重试向 messages 追加提示
   d. 每次重试的原始输出记录到 logs/market_research/ 目录
5. 支持命令行参数 --query 传入调研问题

用法：
    python agent.py --query "近6个月中国猫粮市场增长最快的细分品类"
    python agent.py --query "2025年国产猫粮价格带与品牌格局" --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from openai import OpenAI

# 项目根目录与 tools 目录加入 sys.path，保证可直接运行
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from web_search import TOOL_DEFINITION as WEB_SEARCH_TOOL_DEFINITION
from web_search import search as web_search_func

# 日志目录
LOG_DIR = BASE_DIR / "logs" / "market_research"

# 重试时追加给模型的提示语
RETRY_PROMPT = "上次输出不是合法JSON，请严格按Schema重新生成，不要包含任何额外文字"

# JSON 提取正则：匹配第一个（最外层）{ ... } 块
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# ------------------------------------------------------------
# 环境变量 / .env 加载
# ------------------------------------------------------------
def load_env_file(env_path: Path) -> None:
    """加载 .env 文件到环境变量（若文件存在）。"""
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过空行与注释
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                # setdefault：不覆盖已存在的环境变量
                os.environ.setdefault(key, value)


def expand_env(text: str) -> str:
    """将字符串中的 ${VAR} 占位符替换为环境变量值。"""
    if not text:
        return text

    def _replace(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return re.sub(r"\$\{(\w+)\}", _replace, text)


def _resolve_env(node: Any) -> Any:
    """递归解析配置树中的 ${ENV} 占位符。"""
    if isinstance(node, str):
        return expand_env(node)
    if isinstance(node, dict):
        return {k: _resolve_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_env(v) for v in node]
    return node


# ------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------
def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """加载并解析 config.yaml，自动解析环境变量占位符。"""
    path = config_path or BASE_DIR / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _resolve_env(raw)


# ------------------------------------------------------------
# 日志
# ------------------------------------------------------------
def setup_logging() -> logging.Logger:
    """初始化日志：控制台 + 文件（logs/market_research/）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"agent_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("market_research")
    logger.setLevel(logging.INFO)
    # 清空已有 handler，避免重复输出
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件输出
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.info("日志文件：%s", log_file)
    return logger


# ------------------------------------------------------------
# JSON 解析
# ------------------------------------------------------------
def parse_json(text: str) -> Optional[Dict[str, Any]]:
    """
    将模型输出解析为 JSON dict。

    流程：
      a. 优先直接 json.loads
      b. 若失败，用正则提取第一个 {...} 块再解析
    返回 None 表示解析失败。
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()

    # a. 直接解析
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # b. 正则提取第一个 JSON 对象块
    try:
        match = JSON_BLOCK_RE.search(stripped)
        if match:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
    except (json.JSONDecodeError, ValueError):
        pass

    return None


# ------------------------------------------------------------
# 工具执行
# ------------------------------------------------------------
def _execute_tool(name: str, args: Dict[str, Any], tavily_api_key: str, max_results: int) -> Dict[str, Any]:
    """根据工具名分发给对应实现，返回供模型读取的结构化结果。"""
    if name == "web_search":
        query = args.get("query", "")
        limit = args.get("max_results", max_results)
        return web_search_func(query=query, max_results=limit, api_key=tavily_api_key)
    return {"error": f"未知工具：{name}"}


# ------------------------------------------------------------
# 对话主循环（function calling）
# ------------------------------------------------------------
def run_conversation(
    client: OpenAI,
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    max_tokens: int,
    temperature: float,
    timeout_seconds: float,
    logger: logging.Logger,
    tavily_api_key: str,
    max_results: int,
) -> str:
    """
    与模型多轮对话，自动处理 function calling 工具调用，
    返回最终纯文本内容（期望为 JSON 字符串）。
    """
    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,                      # 超时控制
            response_format={"type": "json_object"},      # 优先 JSON 模式
        )
        message = response.choices[0].message

        # 模型请求调用工具 -> 执行并把结果回填
        if message.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                }
            )
            for tc in message.tool_calls:
                logger.info("调用工具：%s 参数=%s", tc.function.name, tc.function.arguments)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _execute_tool(tc.function.name, args, tavily_api_key, max_results)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            # 继续对话，让模型基于工具结果生成最终答案
            continue

        # 无工具调用 -> 返回最终内容
        return message.content or ""


# ------------------------------------------------------------
# Agent 主逻辑
# ------------------------------------------------------------
def run_agent(query: str, config_path: Optional[Path] = None) -> Dict[str, Any]:
    """执行一次市场调研，返回结构化 JSON dict。"""
    # 先尝试加载 .env（若存在），保证 key 可直接使用
    load_env_file(BASE_DIR / ".env")

    # 加载配置
    config = load_config(config_path)
    model_cfg: Dict[str, Any] = config["model"]
    tool_cfg: Dict[str, Any] = config["tools"]["web_search"]
    exec_cfg: Dict[str, Any] = config["execution"]

    timeout_seconds = exec_cfg["timeout_seconds"]
    max_retries = exec_cfg["max_retries"]
    max_tokens = model_cfg["max_tokens"]
    temperature = model_cfg["temperature"]

    # 校验 DeepSeek API Key
    deepseek_key = model_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_key:
        raise RuntimeError(
            "未找到 DEEPSEEK_API_KEY：请设置环境变量，或在项目目录放置 .env 文件"
        )
    tavily_key = tool_cfg.get("api_key") or os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        raise RuntimeError(
            "未找到 TAVILY_API_KEY：请设置环境变量，或在项目目录放置 .env 文件"
        )

    logger = setup_logging()

    # 入口打印模型名称与工具列表，便于确认配置生效
    logger.info("=" * 60)
    logger.info("市场调研 Agent 启动")
    logger.info("调研问题：%s", query)
    logger.info("模型名称：%s", model_cfg["name"])
    logger.info("接口地址：%s", model_cfg["base_url"])
    logger.info("工具列表：web_search (provider=%s, max_results=%s)", tool_cfg["provider"], tool_cfg["max_results"])
    logger.info("执行配置：timeout=%ss max_retries=%s json_validation=%s",
                timeout_seconds, max_retries, exec_cfg["json_validation"])
    logger.info("=" * 60)

    # 创建 OpenAI SDK 客户端
    client = OpenAI(api_key=deepseek_key, base_url=model_cfg["base_url"])

    # 读取系统提示词模板
    prompt_path = BASE_DIR / "prompt.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # 初始化消息
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    # 工具列表（function calling）
    tools = [WEB_SEARCH_TOOL_DEFINITION]

    # JSON 解析重试循环：1 次初始 + max_retries 次重试
    for attempt in range(1, max_retries + 2):
        logger.info("---- 第 %d 次生成 ----", attempt)

        # 获取模型原始输出（内部自动处理工具调用）
        content = run_conversation(
            client=client,
            model=model_cfg["name"],
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            logger=logger,
            tavily_api_key=tavily_key,
            max_results=tool_cfg["max_results"],
        )

        # d. 记录每次重试的原始输出
        raw_file = LOG_DIR / f"raw_output_attempt_{attempt}_{datetime.now():%Y%m%d_%H%M%S}.txt"
        raw_file.write_text(content, encoding="utf-8")
        logger.info("原始输出已记录：%s", raw_file)

        # 解析 JSON
        parsed = parse_json(content)
        if parsed is not None:
            logger.info("JSON 解析成功（第 %d 次）", attempt)
            # 保存最终结果
            result_file = LOG_DIR / f"result_{datetime.now():%Y%m%d_%H%M%S}.json"
            result_file.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("最终结果已保存：%s", result_file)
            return parsed

        # 解析失败 -> 准备重试
        logger.warning("第 %d 次输出不是合法 JSON", attempt)
        if attempt <= max_retries:
            # c. 在 messages 末尾追加重试提示
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": RETRY_PROMPT})
            time.sleep(0.5)  # 轻微退避，避免限流

    raise RuntimeError(f"连续 {max_retries + 1} 次尝试均未获得合法 JSON，已放弃")


# ------------------------------------------------------------
# 命令行入口
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="宠物食品市场调研 Agent（DeepSeek + Tavily function calling）"
    )
    parser.add_argument(
        "--query",
        required=True,
        help="调研问题，例如：近6个月中国猫粮市场增长最快的细分品类",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml 路径（默认使用项目根目录下的 config.yaml）",
    )
    args = parser.parse_args()

    try:
        result = run_agent(query=args.query, config_path=args.config)
        print("\n===== 结构化调研结果 =====")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
