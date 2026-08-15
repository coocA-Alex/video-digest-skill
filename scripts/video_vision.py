"""Generate a timestamped vision summary for one video's frames via MIMO.

Reads tmp/{bvid}_frames/manifest.json (produced by video_frames.py),
sends each frame to the MIMO vision API, and writes the combined
extraction to tmp/{bvid}_frames/vision.md. Used as a second input for
the summarization prompt so on-screen numbers can cross-check the
spoken narration.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mimo_vision import analyze_image  # noqa: E402

VISION_PROMPT = (
    "这是B站视频截图。只提取画面中的数字和数据(指数点位、涨跌幅、成交额、"
    "家数、K线/表格数值、价格、板块涨跌、日期时间等),按'时间+内容'精简列出,"
    "每行一条。忽略菜单栏、导航、软件名称、登录注册等界面装饰文字。"
    "只列画面中真实存在的内容,不要推测。"
)

# tech 场景: 内嵌字幕/界面截图的英文专有名词是核验重点
# (B站 AI 字幕对双语专有名词识别差, 画面原文用于纠正口播音译)
TECH_PROMPT = (
    "这是B站视频截图。提取画面中所有可辨识的文字,重点是:1)内嵌字幕原文"
    "(屏幕下方中英文字幕);2)界面截图、代码、产品演示中的英文专有名词"
    "(AI产品名、公司名、工具名、版本号);3)日期时间。英文原文原样保留。"
    "按行精简列出,每行一条,只列画面中真实存在的内容,不要推测。"
    "忽略菜单栏、导航、角标等界面装饰。"
)

# 界面噪音行: 命中任一关键词即丢弃, 控制 prompt token
NOISE_KEYWORDS = (
    "菜单栏", "系统(", "行情(", "资讯(", "数据(", "分析(", "功能(", "交易(", "帮助(",
    "登录", "注册", "快速交易", "自定义面板", "多窗口", "东方财富 经典版",
    "自选股", "沪深京", "板块监控", "科创板", "金融期货", "期权市场", "全球市场",
    "沪深港通", "港股市场", "美股市场", "DDE", "龙虎榜", "融资融券", "大宗交易",
    "限售解禁", "宏观数据", "L2", "内参",
)


def _filter_noise(text: str) -> str:
    """Drop interface-chrome lines to shrink the vision summary."""
    kept = []
    for line in text.splitlines():
        if any(kw in line for kw in NOISE_KEYWORDS):
            continue
        if line.strip():
            kept.append(line)
    return "\n".join(kept)


class VisionError(Exception):
    """Raised when no vision summary can be produced."""


# MIMO 帧间独立且不限频: 并发读帧把视觉阶段从 ~2.5min 压到 ~40s
VISION_FRAME_WORKERS = 4


def _analyze_one(item: dict, frames_dir: Path, prompt: str) -> str:
    frame_path = frames_dir / item["file"]
    try:
        text = _filter_noise(analyze_image(str(frame_path), prompt, max_tokens=1200))
        if text.strip():
            return f"[{item['time_str']}] {text.strip()}"
        return ""
    except Exception as exc:
        return f"[{item['time_str']}] (读帧失败: {exc})"


def summarize_frames(
    bvid: str,
    force: bool = False,
    frames_dir: Path | None = None,
    prompt: str = VISION_PROMPT,
) -> str:
    """Extract text/numbers from all frames of one video via MIMO.

    Cached in vision.md; use force=True to re-run. Single-frame failures
    are recorded inline instead of aborting. prompt selects the extraction
    focus: VISION_PROMPT (numbers) or TECH_PROMPT (burned-in subtitles and
    English proper nouns).
    """
    if frames_dir is None:
        frames_dir = TMP_DIR / f"{bvid}_frames"
    manifest_path = frames_dir / "manifest.json"
    if not manifest_path.exists():
        raise VisionError(f"no frames manifest for {bvid}; run video_frames.py first")
    out_path = frames_dir / "vision.md"
    if out_path.exists() and not force:
        return out_path.read_text(encoding="utf-8")
    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=VISION_FRAME_WORKERS) as pool:
        futures = {
            pool.submit(_analyze_one, item, frames_dir, prompt): i
            for i, item in enumerate(manifest["frames"])
        }
        results: list[str] = [""] * len(manifest["frames"])
        for fut, i in futures.items():
            results[i] = fut.result()
    lines = [r for r in results if r]
    summary = "\n\n".join(lines)
    out_path.write_text(summary, encoding="utf-8")
    return summary


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python video_vision.py <bvid> [--force]")
        sys.exit(1)
    bvid = sys.argv[1]
    force = "--force" in sys.argv[2:]
    try:
        summary = summarize_frames(bvid, force)
        print(f"vision summary: {len(summary)} chars")
        print(summary[:800])
    except VisionError as exc:
        print(f"error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
