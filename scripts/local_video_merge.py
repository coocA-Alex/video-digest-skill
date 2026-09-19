"""Merge segmented local video recordings into one complete overview note.

Scans tmp/lecture/* (output of local_video_pipeline.py), orders segments by
the HH.MM.SS timestamp embedded in the video name, concatenates their
transcripts and per-segment vision summaries, and summarizes the whole
lecture into a single coherent note.

Long-input handling (lessons from 2026-08-13 real runs):
- thinking models (deepseek-v4-flash) exhaust max_tokens on reasoning and
  return truncated/empty content without erroring -> this module always
  passes a large max_tokens (see MERGE_MAX_TOKENS)
- inputs above the safe budget are split into chunks; each chunk is
  summarized, then the chunk summaries are merged into the final overview

Usage:
    python local_video_merge.py [--owner 讲座] [--title 讲座总览]
        [--match 子串] [--all] [--force]

tmp/lecture/ 会累积历次讲座, 默认只合并最新一次录制会话; --match <子串> 显式
指定某一会话, --all 恢复"全量合并"的旧行为。
"""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
NOTES_DIR = PROJECT_ROOT / "notes"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bili_summarize  # noqa: E402

# v4-flash 思考型: 长输入必须给足 max_tokens (50000 实测 API 接受; 8192 会被思考吃光)
MERGE_MODEL = "deepseek-v4-flash"
MERGE_MAX_TOKENS = 50000
SAFE_INPUT_CHARS = 40000  # 单次输入上限: 中文约 1.3 字符/token, 给上下文与输出留余量
CHUNK_CHARS = 30000

CHUNK_PROMPT = """以下是完整讲座的其中一段。请用紧凑的结构化 markdown 总结本段：
## 本段要点 (3-6 条, 每条含关键事实/数字/概念)
## 本段观点 (逐条, "讲述者观点:" 前缀 + 原话引用 ≤30 字)
## 本段画面信息 (画面中的标题/数字, 带时间戳; 无则写"无")
只基于给定文本, 不补充外部知识, 数字原样保留。"""


class MergeError(Exception):
    """Raised when the merge cannot proceed."""


_RECORDING_TS = re.compile(r"(\d{4}\.\d{2}\.\d{2})\s*-\s*(\d{2}\.\d{2}\.\d{2})")


def _order_key(name: str) -> str:
    """Sort key = the recording timestamp embedded in the video name.

    The whole 'YYYY.MM.DD - HH.MM.SS' is required: the old two-digit pattern
    matched only '26.09.16' out of '2026.09.16', so every segment recorded on
    the same day collided and the ordering silently fell back to glob order.
    """
    m = _RECORDING_TS.search(name)
    return f"{m.group(1)} {m.group(2)}" if m else name


def _recording_date(name: str) -> str:
    """Recording date ('YYYY.MM.DD') from the video name; '' when absent."""
    m = _RECORDING_TS.search(name)
    return m.group(1) if m else ""


def _latest_session(dirs: list[Path]) -> list[Path]:
    """Keep only the dirs belonging to the newest recording date."""
    latest = max((_recording_date(d.name) for d in dirs), default="")
    if not latest:
        return dirs  # 名字里没有时间戳, 无从判断新旧, 保持原样
    return [d for d in dirs if _recording_date(d.name) == latest]


def _has_transcript(d: Path) -> bool:
    return bool(list((d / "audio").glob("seg_*.txt")))


def collect_segments(
    match: str | None = None, merge_all: bool = False
) -> list[tuple[str, str, str]]:
    """Return [(seg_name, transcript, vision)] ordered by recording time.

    Defaults to the newest recording session only: tmp/lecture/ accumulates
    every lecture ever processed, and merging across sessions would silently
    concatenate unrelated talks. `match` selects by directory-name substring.
    """
    dirs = [d for d in TMP_DIR.glob("lecture/*") if _has_transcript(d)]
    if match:
        dirs = [d for d in dirs if match in d.name]
    elif not merge_all:
        dirs = _latest_session(dirs)

    segs: list[tuple[str, str, str]] = []
    for d in sorted(dirs, key=lambda p: _order_key(p.name)):
        txts = sorted((d / "audio").glob("seg_*.txt"))
        transcript = "\n".join(t.read_text(encoding="utf-8").strip() for t in txts)
        vpath = d / "frames" / "vision.md"
        vision = vpath.read_text(encoding="utf-8").strip() if vpath.exists() else ""
        segs.append((d.name, transcript, vision))
    return segs


def _chunk_summary(text: str, api_key: str, template: str, cache_dir: Path) -> str:
    """Summarize one chunk, cached by content hash (crash-resume support)."""
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    cache = cache_dir / f"{h}.md"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    messages = bili_summarize.build_prompt("讲座", "讲座分块", text, None, template)
    messages[-1]["content"] = messages[-1]["content"].replace(
        "以下是 {owner} 的视频《{title}》字幕全文", "以下是讲座的一段文本"
    )
    result = bili_summarize._post_completions(api_key, MERGE_MODEL, messages, max_tokens=MERGE_MAX_TOKENS)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(result, encoding="utf-8")
    return result


def merge(segs: list[tuple[str, str, str]], api_key: str, template: str = "lecture") -> str:
    """Summarize all segments into one overview, chunking when needed."""
    inputs = [
        f"===== 片段 {name} =====\n{transcript}\n\n{vision}"
        for name, transcript, vision in segs
        if transcript.strip()
    ]
    total = sum(len(t) for t in inputs)
    cache_dir = TMP_DIR / "lecture" / "_merge_cache"
    if total <= SAFE_INPUT_CHARS:
        # vision 走 vision_summary 参数(触发"画面提取与核验"节), 不混入转写正文
        transcript = "\n\n".join(
            f"===== 片段 {name} =====\n{t}" for name, t, _ in segs if t.strip()
        )
        vision = "\n\n".join(
            f"===== 片段 {name} =====\n{v}" for name, _, v in segs if v.strip()
        )
        messages = bili_summarize.build_prompt("讲座", "完整讲座", transcript, vision or None, template)
        return bili_summarize._post_completions(api_key, MERGE_MODEL, messages, max_tokens=MERGE_MAX_TOKENS)

    # 分块: 每块 ≤ CHUNK_CHARS, 块总结后合并 (块级缓存支持断点续跑)
    chunks, cur = [], ""
    for text in inputs:
        if len(cur) + len(text) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = text
        else:
            cur = f"{cur}\n\n{text}" if cur else text
    if cur:
        chunks.append(cur)
    chunk_summaries = [_chunk_summary(c, api_key, template, cache_dir) for c in chunks]
    merged = "\n\n".join(f"===== 分块 {i + 1} =====\n{s}" for i, s in enumerate(chunk_summaries))
    messages = bili_summarize.build_prompt("讲座", "完整讲座(分块合并)", merged, None, template)
    return bili_summarize._post_completions(api_key, MERGE_MODEL, messages, max_tokens=MERGE_MAX_TOKENS)


def main() -> None:
    owner, title, force = "讲座", "讲座总览", False
    match: str | None = None
    merge_all = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--owner":
            owner = args[i + 1]; i += 1
        elif args[i] == "--title":
            title = args[i + 1]; i += 1
        elif args[i] == "--match":
            match = args[i + 1]; i += 1
        elif args[i] == "--all":
            merge_all = True
        elif args[i] == "--force":
            force = True
        else:
            print(f"unknown arg: {args[i]}"); sys.exit(1)
        i += 1

    segs = collect_segments(match=match, merge_all=merge_all)
    if not segs:
        print("no segments found in tmp/lecture/; run local_video_pipeline.py first")
        sys.exit(1)
    print(f"选中 {len(segs)} 个片段 (录制日期 {_recording_date(segs[0][0]) or '未知'}):")
    for name, transcript, _ in segs:
        print(f"  {_order_key(name)}  {len(transcript)} 字")
    print(f"总转写 {sum(len(s[1]) for s in segs)} 字")

    out_path = NOTES_DIR / owner / f"{datetime.now().strftime('%Y-%m-%d')}_{title}.md"
    if out_path.exists() and not force:
        print(f"已存在, 跳过: {out_path}")
        sys.exit(0)

    api_key = bili_summarize.load_api_key()
    overview = merge(segs, api_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(overview, encoding="utf-8")
    print(f"总览归档 -> {out_path} ({len(overview)} 字)")


if __name__ == "__main__":
    main()
