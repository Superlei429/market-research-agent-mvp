# -*- coding: utf-8 -*-
"""
agent.py —— 宠物食品市场调研 Agent（DeepSeek + Tavily）

v2 升级说明（相对 MVP 的三大改动）：
1. 分维度多轮搜索（替代旧版 1-2 次宽泛搜索）
   - config.search 定义 7 个搜索维度，每维度 2 个关键词组合 = 14+ 次搜索
   - seen_urls 集合对 URL 去重
   - snippet 按维度归类，汇总为「市场调研数据档案」喂给模型
2. 竞品锚点库 data/competitor_anchors.yaml 作为模型对比基准
3. Prompt 升级：<analysis> 思维链 + JSON + <self_check> 自我反思
   输出 Schema 全面升级，对齐下游 Agent②（配方 Agent）输入

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

from web_search import search as web_search_func

# 日志目录
LOG_DIR = BASE_DIR / "logs" / "market_research"

# 重试时追加给模型的提示语
RETRY_PROMPT = "上次输出不是合法JSON，请严格按Schema重新生成，不要包含任何额外文字"

# <analysis> / <self_check> 标签提取
ANALYSIS_TAG_RE = re.compile(r"<analysis>(.*?)</analysis>", re.DOTALL | re.IGNORECASE)
SELF_CHECK_TAG_RE = re.compile(r"<self_check>(.*?)</self_check>", re.DOTALL | re.IGNORECASE)

# 搜索维度中文标签（config 未提供时兜底 / 供档案可读性）
DEFAULT_DIMENSION_LABELS = {
    "market_size": "市场规模与增速",
    "consumer_trends": "消费者需求趋势",
    "ingredient_trends": "热门成分/原料趋势",
    "competitor_analysis": "主要竞品分析",
    "pain_points": "消费者痛点与差评",
    "regulatory": "法规与标准动态",
    "functional_needs": "功能性需求",
}

# 各维度默认关键词模板（config.search.dimension_queries 缺失时兜底）
DEFAULT_DIMENSION_QUERIES = {
    "market_size": ["中国猫粮市场规模 {year}", "中国宠物食品市场 规模 增速 {year}"],
    "consumer_trends": ["猫粮消费者需求 趋势 {year}", "养猫 消费趋势 主粮 偏好 {year}"],
    "ingredient_trends": ["猫粮 热门成分 冻干 鲜肉 {year}", "宠物食品 成分趋势 高蛋白 无谷 {year}"],
    "competitor_analysis": ["中高端猫粮 品牌 测评 排行", "国产猫粮 品牌 对比 高端 价格"],
    "pain_points": ["猫粮 差评 问题 投诉", "猫粮 黑榜 翻车 召回 品质"],
    "regulatory": ["宠物食品 国标 新规 {year}", "宠物饲料 管理办法 标准 更新 {year}"],
    "functional_needs": ["猫粮 功能 泌尿 肠胃 美毛 需求", "功能性猫粮 需求 增长 品类"],
}


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
# 分维度多轮搜索
# ------------------------------------------------------------
def build_search_queries(search_cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    根据 config.search 构建待执行搜索词列表。
    每个维度取前 queries_per_dimension 个关键词模板，并将 {year} 渲染为配置年份。
    """
    year = search_cfg.get("year", datetime.now().year)
    dimensions: List[str] = search_cfg.get("dimensions") or list(DEFAULT_DIMENSION_QUERIES.keys())
    qpd = max(1, int(search_cfg.get("queries_per_dimension", 2)))
    template_map: Dict[str, Any] = search_cfg.get("dimension_queries") or {}

    queries: List[Dict[str, str]] = []
    for dim in dimensions:
        templates = template_map.get(dim) or DEFAULT_DIMENSION_QUERIES.get(dim, [])
        for i in range(min(qpd, len(templates))):
            query = str(templates[i]).format(year=year)
            queries.append({"dimension": dim, "query": query})
    return queries


def run_multi_dim_search(
    tavily_api_key: str,
    search_cfg: Dict[str, Any],
    logger: logging.Logger,
) -> Dict[str, Any]:
    """
    执行分维度多轮搜索：
    - 每个维度执行 queries_per_dimension 次不同关键词搜索
    - seen_urls 集合去重（跳过已见过的 URL）
    - snippet 按维度归类存储
    返回 {"queries", "buckets", "sources", "errors"}。
    """
    queries = build_search_queries(search_cfg)
    dedup = bool(search_cfg.get("dedup", True))
    max_results = int(search_cfg.get("max_results_per_query", 5))

    seen_urls: set = set()
    buckets: Dict[str, List[Dict[str, str]]] = {}
    sources: List[str] = []
    errors: List[Dict[str, str]] = []

    for item in queries:
        dim, q = item["dimension"], item["query"]
        logger.info("搜索[%s]：%s", dim, q)
        result = web_search_func(query=q, max_results=max_results, api_key=tavily_api_key)

        if "error" in result:
            errors.append({"dimension": dim, "query": q, "error": result["error"]})
            logger.warning("  维度 %s 搜索失败：%s", dim, result["error"])
            continue

        bucket = buckets.setdefault(dim, [])
        added = 0
        for r in result.get("results", []):
            url = r.get("url", "")
            if dedup and url and url in seen_urls:
                logger.info("  去重跳过：%s", url)
                continue
            if url:
                seen_urls.add(url)
                sources.append(url)
            bucket.append(
                {
                    "title": r.get("title", ""),
                    "url": url,
                    "content": r.get("content", ""),
                    "query": q,
                }
            )
            added += 1
        logger.info("  维度 %s 本次新增 %d 条，累计 %d 条", dim, added, len(bucket))

    logger.info(
        "搜索完成：执行 %d 次查询，去重后独立来源 %d 个，失败 %d 次",
        len(queries),
        len(sources),
        len(errors),
    )
    return {"queries": queries, "buckets": buckets, "sources": sources, "errors": errors}


def load_competitor_anchors(anchors_path: Path) -> List[Dict[str, Any]]:
    """加载竞品锚点库，文件不存在或解析失败时返回空列表。"""
    if not anchors_path.exists():
        return []
    try:
        with open(anchors_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("competitors", [])
    except Exception:
        return []


def _truncate(text: Optional[str], max_len: int = 350) -> str:
    """截断长文本，控制送入模型的档案体积。"""
    text = (text or "").strip()
    return text if len(text) <= max_len else text[:max_len] + "…"


def build_dossier(
    query: str,
    research: Dict[str, Any],
    competitors: List[Dict[str, Any]],
    search_cfg: Dict[str, Any],
) -> str:
    """将检索结果按维度归类汇总为「市场调研数据档案」文本。"""
    year = search_cfg.get("year", "")
    labels = DEFAULT_DIMENSION_LABELS
    lines: List[str] = []

    lines.append("【市场调研数据档案】")
    lines.append(f"调研问题：{query}")
    lines.append(f"检索时间：{datetime.now():%Y-%m-%d %H:%M:%S}（目标年份 {year}）")
    lines.append(f"执行搜索词（{len(research['queries'])} 个）：")
    for item in research["queries"]:
        lines.append(f"  - [{item['dimension']}] {item['query']}")
    lines.append(f"去重后独立来源数：{len(research['sources'])}")
    lines.append("")

    # 按维度归类展示 snippet
    for dim, items in research["buckets"].items():
        label = labels.get(dim, dim)
        lines.append(f"## 维度：{dim}（{label}）")
        if not items:
            lines.append("  （该维度未检索到有效结果）")
            lines.append("")
            continue
        for s in items:
            lines.append(f"- 标题：{s['title']}")
            lines.append(f"  链接：{s['url']}")
            lines.append(f"  检索词：{s['query']}")
            lines.append(f"  摘要：{_truncate(s['content'])}")
        lines.append("")

    # 竞品锚点库
    if competitors:
        lines.append("## 竞品锚点库（人工维护静态基准，与实时检索交叉印证）")
        for c in competitors:
            ing = "、".join(c.get("key_ingredients", []) or [])
            lines.append(
                f"- {c.get('brand', '')} | {c.get('product', '')} | "
                f"{c.get('price_per_kg', '')}元/kg | 粗蛋白{c.get('protein_pct', '')}% | "
                f"定位：{c.get('positioning', '')} | 原料：{ing}"
            )
        lines.append("")

    # 检索异常
    if research.get("errors"):
        lines.append("## 检索异常记录")
        for e in research["errors"]:
            lines.append(f"- [{e['dimension']}] {e['query']}：{e['error']}")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------
# JSON 解析（支持 <analysis>/<self_check> 标签格式）
# ------------------------------------------------------------
def extract_json_block(text: str) -> Optional[str]:
    """
    从文本中提取第一个平衡的 {...} JSON 对象块。
    相比贪婪正则，能正确处理嵌套对象与字符串内的 {}。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def parse_json(text: str) -> Optional[Dict[str, Any]]:
    """
    将模型输出解析为 JSON dict：
      a. 优先直接 json.loads（纯 JSON 场景）
      b. 若失败，用平衡括号算法提取 JSON 块再解析
    返回 None 表示解析失败。
    """
    if not text or not text.strip():
        return None

    # a. 直接解析
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # b. 平衡括号提取
    block = extract_json_block(text)
    if block:
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def extract_tag_section(text: str, pattern: "re.Pattern[str]") -> Optional[str]:
    """提取 <tag>...</tag> 内容（best-effort），找不到返回 None。"""
    match = pattern.search(text or "")
    if match:
        return match.group(1).strip()
    return None


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
    search_cfg: Dict[str, Any] = config.get("search", {})

    timeout_seconds = exec_cfg["timeout_seconds"]
    max_retries = exec_cfg["max_retries"]
    max_tokens = model_cfg["max_tokens"]
    temperature = model_cfg["temperature"]
    # 默认关闭 json_object 模式：prompt.md 要求 <analysis>/<self_check> 标签
    use_json_mode = bool(model_cfg.get("response_format", {}).get("enabled", False))

    # 校验 API Key
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

    # 计算总搜索次数用于启动日志
    total_queries = len(build_search_queries(search_cfg))
    logger.info("=" * 60)
    logger.info("市场调研 Agent 启动（v2 分维度多轮搜索）")
    logger.info("调研问题：%s", query)
    logger.info("模型名称：%s", model_cfg["name"])
    logger.info("接口地址：%s", model_cfg["base_url"])
    logger.info("搜索策略：%d 个维度 × %d 次/维度 = %d 次搜索",
                len(search_cfg.get("dimensions", [])),
                search_cfg.get("queries_per_dimension", 2),
                total_queries)
    logger.info("JSON模式：%s（False 时模型输出 <analysis>/<self_check> 标签）", use_json_mode)
    logger.info("=" * 60)

    # ---------- Phase 1：分维度多轮搜索 ----------
    research = run_multi_dim_search(tavily_key, search_cfg, logger)

    # 竞品锚点库
    anchors_rel = config.get("competitor_anchors", "data/competitor_anchors.yaml")
    anchors_path = BASE_DIR / anchors_rel
    competitors = load_competitor_anchors(anchors_path)
    logger.info("加载竞品锚点库：%s（%d 条）", anchors_path, len(competitors))

    # 构建调研档案
    dossier = build_dossier(query, research, competitors, search_cfg)

    # ---------- Phase 2：读取提示词 + 生成 ----------
    prompt_path = BASE_DIR / "prompt.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")

    user_prompt = (
        f"调研问题：{query}\n\n"
        f"{dossier}\n\n"
        "请基于以上市场调研数据档案，严格按系统提示的思维链要求完成分析，"
        "并输出 <analysis> + JSON + <self_check>。"
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    client = OpenAI(api_key=deepseek_key, base_url=model_cfg["base_url"])

    # JSON 解析重试循环：1 次初始 + max_retries 次重试
    for attempt in range(1, max_retries + 2):
        logger.info("---- 第 %d 次生成 ----", attempt)

        kwargs: Dict[str, Any] = dict(
            model=model_cfg["name"],
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        )
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""

        # 记录原始输出
        raw_file = LOG_DIR / f"raw_output_attempt_{attempt}_{datetime.now():%Y%m%d_%H%M%S}.txt"
        raw_file.write_text(content, encoding="utf-8")
        logger.info("原始输出已记录：%s", raw_file)

        # 单独记录 <analysis> 与 <self_check>
        analysis = extract_tag_section(content, ANALYSIS_TAG_RE)
        if analysis:
            a_file = LOG_DIR / f"analysis_attempt_{attempt}_{datetime.now():%Y%m%d_%H%M%S}.md"
            a_file.write_text(analysis, encoding="utf-8")
            logger.info("思维链 <analysis> 已记录：%s", a_file)
        self_check = extract_tag_section(content, SELF_CHECK_TAG_RE)
        if self_check:
            s_file = LOG_DIR / f"self_check_attempt_{attempt}_{datetime.now():%Y%m%d_%H%M%S}.md"
            s_file.write_text(self_check, encoding="utf-8")
            logger.info("自我反思 <self_check> 已记录：%s", s_file)

        # 解析 JSON
        parsed = parse_json(content)
        if parsed is not None:
            logger.info("JSON 解析成功（第 %d 次）", attempt)
            # 用真实检索元数据回填 meta，保证可信度
            parsed["meta"] = parsed.get("meta", {})
            parsed["meta"]["generated_at"] = datetime.now().isoformat()
            parsed["meta"]["search_queries_used"] = [q["query"] for q in research["queries"]]
            parsed["meta"]["sources_count"] = len(research["sources"])
            parsed["meta"].setdefault("confidence_level", "low")

            result_file = LOG_DIR / f"result_{datetime.now():%Y%m%d_%H%M%S}.json"
            result_file.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("最终结果已保存：%s", result_file)
            return parsed

        # 解析失败 -> 准备重试
        logger.warning("第 %d 次输出不是合法 JSON", attempt)
        if attempt <= max_retries:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": RETRY_PROMPT})
            time.sleep(0.5)  # 轻微退避，避免限流

    raise RuntimeError(f"连续 {max_retries + 1} 次尝试均未获得合法 JSON，已放弃")


# ------------------------------------------------------------
# 命令行入口
# ------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="宠物食品市场调研 Agent（DeepSeek + Tavily，分维度多轮搜索）"
    )
    parser.add_argument(
        "--query",
        required=True,
        help="调研问题，例如：2026年中国中高端猫粮市场机会",
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
