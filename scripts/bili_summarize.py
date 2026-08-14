"""Summarize a bilibili subtitle file into a structured stock-review note.

Applies a stock-analysis template via the DeepSeek flash API. The template
separates hard market facts from the creator's opinions so claims can be
verified independently. Writes markdown to a caller-provided output path.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

def _load_summarize_config() -> dict:
    """Read LLM provider config from config/multimodal.json (summarize section).

    Model/URL/key-env-var are swappable per config; the key itself only ever
    comes from the environment or user-level files, never from the json.
    """
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "multimodal.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")).get("summarize", {})
    except Exception:
        cfg = {}
    return {
        "model": cfg.get("model", "deepseek-v4-flash"),
        "base_url": cfg.get("base_url", "https://api.deepseek.com/chat/completions"),
        "api_key_env": cfg.get("api_key_env", "DEEPSEEK_API_KEY"),
    }


_SUMMARIZE_CFG = _load_summarize_config()
API_URL = _SUMMARIZE_CFG["base_url"]
MODEL = _SUMMARIZE_CFG["model"]
FALLBACK_MODEL = _SUMMARIZE_CFG["model"]
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"  # Claude Code fallback (optional)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_KEY_PATH = PROJECT_ROOT / "config" / "ds_key.local.json"
REQUEST_TIMEOUT_SECONDS = 120
# 思考型模型长提示先消耗推理 token, 输出额度需留足
MAX_OUTPUT_TOKENS = 8192

TEMPLATES = {
    "stock": """你是一名股票市场视频内容分析助手。你的任务是严格区分"事实数据"与"UP主观点"。

以下是 {owner} 的视频《{title}》字幕全文。请按以下结构输出 markdown 总结:

## 市场数据快照
精确引用字幕中的数字（指数涨跌、成交额、涨跌家数、涨停/跌停数、资金面）。
字幕未提到的数据写"未提及"。不得补充字幕外的数字。

## 板块与热点
当天领涨/领跌板块、热点方向（仅限字幕内容）。

## UP主核心观点
逐条列出，每条用"UP主观点："开头，并附一句原话引用（不超过30字）。
观点必须区分：对当天走势的判断 vs 对后市的预判。

## 操作建议与节奏
UP主建议做什么/不做什么（仓位、节奏、方向），逐条列出。

## 风险提示
UP主提到的风险点。

## 涉及标的
字幕中提到的具体股票名称/代码/行业，逐条列出。

## 数据可信度备注
哪些数字可能听错/需要人工核对（口播数字常见错误）。

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 观点必须标注"UP主观点"，与事实严格分离
3. 数字必须原文保留，不得四舍五入改写
4. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "finance": """你是一名财经新闻分析助手。你的任务是严格区分"事实数据"与"观点解读"。

以下是 {owner} 的视频《{title}》字幕全文。请按以下结构输出 markdown 总结:

## 要闻清单
逐条列出（与视频条目一一对应），每条包含：
- 主题（一句话）
- 关键数据（数字原样保留，不得改写）
- 要点补充（背景、影响等，仅限字幕内容）

## UP主观点
视频中若有观点/解读/预测，逐条列出并用"UP主观点："开头；没有则写"无"。

## 涉及标的与地区
字幕中提到的国家、地区、公司、行业，逐条列出。

## 数据可信度备注
哪些数字可能听错/需要人工核对（口播数字常见错误）。

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 观点与事实严格分离
3. 数字必须原文保留，不得四舍五入改写
4. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "tech": """你是一名科技资讯分析助手。你的任务是提取项目/产品要点并标注存疑内容。

以下是 {owner} 的视频《{title}》字幕全文。请按以下结构输出 markdown 总结:

## 项目/产品清单
逐条列出（与视频条目一一对应），每条包含：
- 名称（字幕音译名+可能的正确名猜测，标注"疑为"）
- 类型/类别
- 关键信息（参数、版本、状态等，数字原样保留）
- UP主评价（若有，用"UP主观点："前缀）

## 数据可信度备注
哪些名称/数字可能听错（口播音译常见错误），需人工核对。

## 对项目的参考
与"视频内容追踪/信息归档"相关的点（可选，没有就不写这一节）。

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 数字必须原文保留，不得四舍五入改写
3. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "general": """你是一名视频内容分析助手。你的任务是严格区分"事实信息"与"讲述者观点"，适用于无特定领域的通用内容（讲座、分享、访谈等）。

以下是 {owner} 的视频《{title}》字幕全文。请按以下结构输出 markdown 总结:

## 内容概要
3-5 句话概括视频核心内容（讲什么、给谁听、解决什么问题）。

## 事实信息
精确引用字幕中的具体信息（数字、时间、人物、事件、数据、定义）。字幕未提到的写"未提及"。不得补充字幕外的信息。

## 讲述者观点
逐条列出，每条用"讲述者观点："开头，并附一句原话引用（不超过30字）。
观点必须区分：对现状的判断 vs 对未来的预判。

## 关键结论
视频最核心的 2-3 个要点。

## 数据可信度备注
字幕中存疑、可能听错、或需人工核对的点。

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 数字必须原文保留，不得四舍五入改写
3. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "lecture": """你是一名讲座内容分析助手。输入是一次学术/技术讲座的完整文本，包含主讲人的连贯叙述报告，以及（若有）专家学者的问答环节。请严格区分"事实信息"与"讲述者观点"，按以下结构输出 markdown:

## 讲座概要
3-6 句话概括整场讲座：主题、核心主张、报告部分讲了什么、问答环节覆盖了哪些方向。

## 报告部分事实
精确引用报告叙述中的具体信息（术语定义、数字、案例、人名、方法）。字幕未提到的写"未提及"。

## 报告部分观点
逐条列出主讲人观点，每条用"讲述者观点："开头，附原话引用（≤30字）。区分：对现状的判断 vs 对未来的预判。

## 问答环节 · 逐条解读
若文本包含问答（提问者问、讲述者答），按问答发生的顺序完整覆盖每一轮问答（含穿插对话，不因聚合而省略任何一轮）。每轮格式：
### Q{{n}}. 本轮问答主题（≤15字）
- **提问意图**：提问者关注什么、为何问这个（结合其领域背景判断）
- **问题还原**：尽量完整还原问题要点，保留关键论点与具体例子
- **回答论证链**：讲述者回答的逻辑链，分步展开（论据→推理→结论），关键处附原话引用（≤30字）
- **思想碰撞**：专家的追问/质疑/补充/让步，双方观点差异，以及由此引起的话题转向
若文本无问答环节，本节写"无问答环节"。

## 问答环节 · 分组解读
若包含问答，基于逐条解读，将各轮问答按主题聚类为 2-4 组，每组：
- **主题**：组内各轮问答的共同关注点
- **递进与冲突**：组内论点如何递进/互补/碰撞，专家分歧的核心是什么
若无问答环节，本节省略。

## 问答环节 · 分析评论
若包含问答，基于分组解读，分析整个问答环节的思想脉络（还原为主，不做价值判断，不补充输入外知识）：
- **思想主线**：问答环节从何起、经何转折、落向何处
- **方法论启示**：问答中暴露或确立的核心方法论（如：形式化 vs 涌现、还原论 vs 整体论）
- **未解问题**：主讲人未正面回应的挑战、专家之间未达成的共识
若无问答环节，本节省略。

## 关键结论
整场讲座（含问答）最核心的 3-5 个要点。

## 画面提取与核验
画面中的标题、数字、图表内容，带时间戳；与口播不一致时标注。

## 数据可信度备注
字幕存疑、听写可能出错、需人工核对的点。

规则：
1. 只基于输入文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 数字必须原文保留，不得四舍五入改写
3. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "keypoints": """你是一名视频内容分析助手。请将以下视频字幕提炼为**要点式摘要**：

## 核心要点
- 每条独立要点（5-10 条），按重要程度排序，每条约 1-2 句

## 关键数据/引用
字幕中的具体数字、名称、原话引用，原文保留（无则写"无"）

## 存疑内容
听错可能/需要人工核验的内容（无则写"无"）

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 事实与观点分离：要点区分"事实陈述"与"讲述者观点"
3. 数字必须原文保留，不得改写
4. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "timeline": """你是一名视频内容分析助手。请将以下视频字幕整理为**时间线式摘要**：

## 内容时间线
- 按内容先后分段：每段标注时间点（字幕带时间戳则用，否则用顺序编号），后跟一段话概括该段内容
- 保持讲述节奏（如"开场引入 → 展开论述 → 案例 → 结论"）

## 核心信息
全片最核心的 3-5 条信息，每条一句话

## 存疑内容
听错可能/需要人工核验的内容（无则写"无"）

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 数字必须原文保留，不得改写
3. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "notes": """你是一名视频内容分析助手。请将以下视频字幕整理为**详细学习笔记**（保留细节，不压缩）：

## 概述
3-5 句话概括全片主题与主线

## 章节笔记
### <按内容自然分段命名章节>
- 要点条目，保留细节、例子、数据（每段 3-8 条）

## 术语与概念
字幕中出现的专有名词/概念及其上下文摘录（无则写"无"）

## 启发与行动项
字幕中值得记录的启发、可执行建议（无则写"无"）

## 存疑内容
听错可能/需要人工核验的内容（无则写"无"）

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 事实与观点分离
3. 数字必须原文保留，不得改写
4. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
    "opinions": """你是一名视频内容分析助手。请从以下视频字幕中**提取全部观点、判断与预测**：

## 观点清单
### 观点 1：<一句话概括>
- 详细内容（讲述者的完整表述）
- 依据（讲述者给出的理由、数据、案例）
（逐条列出，直到提取完所有观点）

## 观点演进与矛盾
讲述者前后观点变化或矛盾（无则写"无"）

## 事实边界备注
区分哪些是事实陈述、哪些是观点（列出关键几处）

规则：
1. 只基于字幕文本（及画面提取摘要，若提供），绝不补充输入外的事实或知识
2. 只提取字幕中明确表达的观点，不推断不补充
3. 数字必须原文保留，不得改写
4. 输出纯 markdown，不要额外解释

字幕全文：
{subtitle}
{vision_section}""",
}

VISION_SECTION = """
画面提取摘要（每帧带时间戳，用于核验口播数据）：
{vision_summary}

额外要求：
1. 新增 "## 画面提取与核验" 一节（位于"数据可信度备注"之前），列出画面中的关键数据
   （指数点位、涨跌幅、表格数字、界面内容等），每条带时间戳
2. 画面数字与字幕/口播数字不一致时，在该节标注"画面与口播不一致"，并同步写进"数据可信度备注"
3. 画面未出现的数字不补充；该节只依赖画面提取摘要内容
4. 画面文字（含英文专有名词原文）与口播不一致时，以画面原文为准：输出正确原文，
   并在"数据可信度备注"标注该音译/识别错误（如口播 "if fc" 画面为 "DeepSeek"）"""


def detect_suspicious(summary: str) -> bool:
    """True when a summary flags misheard terms that vision could resolve.

    AI subtitles misrecognize bilingual proper nouns; when the summary
    itself marks several suspect terms (疑为/音译/误听/需人工核对/无法核验),
    the digest re-runs with on-screen verification instead of guessing.
    """
    score = sum(
        summary.count(m)
        for m in ("疑为", "音译", "误听", "需人工核对", "无法核验")
    )
    return score >= 2


class ApiKeyError(Exception):
    """Raised when the DeepSeek API key cannot be loaded."""


class SummaryEmptyError(Exception):
    """Raised when the model returns an empty summary body."""


def load_api_key() -> str:
    """Load the LLM key — agent-agnostic resolution order:

    1. environment (api_key_env from config, e.g. DEEPSEEK_API_KEY, or
       ANTHROPIC_AUTH_TOKEN) — works in any agent (Claude Code/Codex/etc.)
    2. project-local config/ds_key.local.json (gitignored)
    3. ~/.claude/settings.json (Claude Code legacy fallback)
    """
    import os

    for env_name in (_SUMMARIZE_CFG["api_key_env"], "ANTHROPIC_AUTH_TOKEN"):
        key = os.getenv(env_name)
        if key:
            return key
    if LOCAL_KEY_PATH.exists():
        with open(LOCAL_KEY_PATH, encoding="utf-8") as f:
            api_key = json.load(f).get("api_key", "")
        if api_key:
            return api_key
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            api_key = json.load(f).get("env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
        if api_key:
            return api_key
    raise ApiKeyError(
        f"no API key found: set {_SUMMARIZE_CFG['api_key_env']} or "
        f"ANTHROPIC_AUTH_TOKEN env var, or use {LOCAL_KEY_PATH}"
    )


def build_prompt(
    owner: str,
    title: str,
    subtitle_text: str,
    vision_summary: str | None = None,
    template: str = "stock",
) -> list[dict[str, str]]:
    """Build the chat messages for the summarization request."""
    tpl = TEMPLATES.get(template, TEMPLATES["stock"])
    vision_section = VISION_SECTION.format(vision_summary=vision_summary) if vision_summary else ""
    user_content = tpl.format(
        owner=owner, title=title, subtitle=subtitle_text, vision_section=vision_section
    )
    return [
        {"role": "system", "content": "输出使用简体中文，markdown 格式。"},
        {"role": "user", "content": user_content},
    ]


def _post_completions(api_key: str, model: str, messages: list[dict[str, str]], max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Post one chat completion request and return the reply text."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    response = requests.post(API_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def summarize_subtitle(
    api_key: str,
    owner: str,
    title: str,
    subtitle_text: str,
    vision_summary: str | None = None,
    template: str = "stock",
) -> str:
    """Summarize subtitle text with model fallback on API error.

    Empty model output is retried once; transient API hiccups have been
    observed to return empty content for otherwise-valid prompts.
    """
    if not subtitle_text.strip():
        raise ValueError("subtitle text is empty")
    messages = build_prompt(owner, title, subtitle_text, vision_summary, template)
    for attempt in range(2):
        try:
            content = _post_completions(api_key, MODEL, messages)
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                content = _post_completions(api_key, FALLBACK_MODEL, messages)
            else:
                raise
        if content and content.strip():
            return content
        # v4-flash 思考型模型偶发 content 为空: 换非思考模型兜底重试
        content = _post_completions(api_key, FALLBACK_MODEL, messages)
        if content and content.strip():
            return content
        time.sleep(5)
    raise SummaryEmptyError("all model attempts returned empty content (thinking model + fallback)")


def main() -> None:
    """Summarize one subtitle file; argv: <subtitle.txt> <owner> [out.md] [style]."""
    if len(sys.argv) < 3:
        print("usage: python bili_summarize.py <subtitle.txt> <owner> [out.md] [style]")
        print(f"styles: {', '.join(TEMPLATES)}")
        sys.exit(1)
    sub_path = Path(sys.argv[1])
    if not sub_path.exists():
        print(f"subtitle file not found: {sub_path}")
        sys.exit(1)
    owner = sys.argv[2]
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else sub_path.with_suffix(".md")
    style = sys.argv[4] if len(sys.argv) > 4 else "stock"
    if style not in TEMPLATES:
        print(f"unknown style '{style}', available: {', '.join(TEMPLATES)}")
        sys.exit(1)
    subtitle_text = sub_path.read_text(encoding="utf-8")
    summary = summarize_subtitle(load_api_key(), owner, sub_path.stem, subtitle_text, template=style)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(summary, encoding="utf-8")
    print(f"summary ({style}) -> {out_path} ({len(summary)} chars)")


if __name__ == "__main__":
    main()
