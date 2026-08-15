"""Weekly digest orchestrator for followed bilibili creators.

Flow per creator: fetch latest videos via the space API (WBI-signed),
skip already-processed ones, fetch subtitle, summarize via DeepSeek
flash, archive markdown to notes/{owner}/{date}_{bvid}.md.

Processed videos are tracked in tmp/state.json (gitignored).
"""
from __future__ import annotations

import json
import msvcrt
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "creators.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "creators.example.json"
NOTES_DIR = PROJECT_ROOT / "notes"
STATE_PATH = PROJECT_ROOT / "tmp" / "state.json"
LOCK_PATH = PROJECT_ROOT / "tmp" / "digest.lock"
DEFAULT_MAX_VIDEOS = 10
DEFAULT_BACKFILL_CUTOFF = "2026-08-01"
DEFAULT_BACKFILL_PER_CREATOR = 5
BACKFILL_PAGE_SIZE = 30
BACKFILL_MAX_PAGES_PER_RUN = 3
BACKFILL_PAGE_SLEEP_SECONDS = 5

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bili_subtitle import (  # noqa: E402
    HEADERS,
    NoSubtitleError,
    SubtitleError,
    fetch_subtitle_text,
    get_wbi_keys,
    load_sessdata,
    mixin_key,
    signed_params,
)
from bili_summarize import detect_suspicious, load_api_key, summarize_subtitle  # noqa: E402
from video_frames import FrameExtractError, extract_frames, get_stream_info  # noqa: E402
from video_vision import TECH_PROMPT, VISION_PROMPT, VisionError, summarize_frames  # noqa: E402

DEFAULT_VISION_FRAMES = 20
# B站 AI 字幕滞后生成: 无字幕视频 N 秒后重试, 避免永久漏掉
RETRY_NO_SUBTITLE_SECONDS = 3 * 24 * 3600


class CreatorConfigError(Exception):
    """Raised when the creator list cannot be loaded."""


class DigestError(Exception):
    """Raised when a video cannot be processed to completion."""


def load_creators() -> list[dict[str, object]]:
    """Load the followed-creator list, falling back to the example file."""
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not config_path.exists():
        raise CreatorConfigError(f"no creator config found: {CONFIG_PATH}")
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    creators = data.get("creators", [])
    if not creators:
        raise CreatorConfigError("creator list is empty")
    return creators


_LOCK_FD: int | None = None


def acquire_single_instance_lock() -> None:
    """Exit if another digest instance is running.

    msvcrt exclusive lock on tmp/digest.lock: a second concurrent run
    exits immediately instead of hammering the bilibili API (412 risk).
    The lock is released automatically when the process exits or dies.
    """
    global _LOCK_FD
    LOCK_PATH.parent.mkdir(exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        print("[lock] 另一实例正在运行, 本轮退出", file=sys.stderr)
        sys.exit(0)
    _LOCK_FD = fd


def load_state() -> dict[str, object]:
    """Load the processed-video state from tmp/state.json."""
    if not STATE_PATH.exists():
        return {"processed": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, object]) -> None:
    """Persist the processed-video state to tmp/state.json."""
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _set_fingerprint_cookies(session: requests.Session) -> None:
    """Set buvid3/buvid4 cookies to pass bilibili risk control (412)."""
    response = session.get(
        "https://api.bilibili.com/x/frontend/finger/spi", headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    data = response.json()["data"]
    session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
    session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")


def fetch_latest_videos(sessdata: str, mid: int, max_videos: int) -> list[dict[str, object]]:
    """Fetch the latest published videos of one creator via the space API.

    Bilibili risk control returns 412 intermittently; retry with fresh
    fingerprint cookies up to 3 attempts.
    """
    session = requests.Session()
    session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    headers = dict(HEADERS, Referer=f"https://space.bilibili.com/{mid}")
    response: requests.Response | None = None
    for attempt in range(3):
        try:
            _set_fingerprint_cookies(session)
            img_key, sub_key = get_wbi_keys(session)
            mixin = mixin_key(img_key, sub_key)
            query = signed_params(
                {"mid": str(mid), "ps": str(max_videos), "pn": "1", "order": "pubdate"}, mixin
            )
            response = session.get(
                f"https://api.bilibili.com/x/space/wbi/arc/search?{query}",
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            break
        except requests.HTTPError as exc:
            if exc.response.status_code != 412 or attempt == 2:
                raise
            time.sleep(2)
    data = response.json()
    if data["code"] != 0:
        raise DigestError(f"space API {data['code']}: {data['message']}")
    vlist = data.get("data", {}).get("list", {}).get("vlist", [])
    return [
        {
            "bvid": item["bvid"],
            "title": item["title"],
            "pubdate": int(item["created"]),
        }
        for item in vlist
    ]


def fetch_paginated_videos(
    sessdata: str, mid: int, start_pn: int, max_pages: int, cutoff_ts: int = 0
) -> tuple[list[dict[str, object]], int, bool]:
    """Fetch videos from start_pn, up to max_pages. Returns (videos, next_pn, reached_end).

    reached_end is True when a page comes back empty, or when a page
    contains videos older than cutoff_ts (no point paging further back).
    Pages are rate-limited with 2s sleeps; 412 retries with fresh cookies.
    """
    session = requests.Session()
    session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    headers = dict(HEADERS, Referer=f"https://space.bilibili.com/{mid}")
    videos: list[dict[str, object]] = []
    next_pn = start_pn
    for page in range(start_pn, start_pn + max_pages):
        response: requests.Response | None = None
        for attempt in range(3):
            try:
                _set_fingerprint_cookies(session)
                img_key, sub_key = get_wbi_keys(session)
                mixin = mixin_key(img_key, sub_key)
                query = signed_params(
                    {"mid": str(mid), "ps": str(BACKFILL_PAGE_SIZE), "pn": str(page), "order": "pubdate"},
                    mixin,
                )
                response = session.get(
                    f"https://api.bilibili.com/x/space/wbi/arc/search?{query}",
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                break
            except requests.HTTPError as exc:
                if exc.response.status_code != 412:
                    raise
                if attempt == 2:
                    # 风控持续: 本轮暂停, 断点留在当前页, 下轮继续
                    time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
                    return videos, page, False
                time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
        assert response is not None
        data = response.json()
        if data["code"] != 0:
            raise DigestError(f"space API {data['code']}: {data['message']}")
        vlist = data.get("data", {}).get("list", {}).get("vlist", [])
        if not vlist:
            return videos, page + 1, True
        videos.extend(
            {"bvid": item["bvid"], "title": item["title"], "pubdate": int(item["created"])}
            for item in vlist
        )
        next_pn = page + 1
        if cutoff_ts and any(int(item["created"]) < cutoff_ts for item in vlist):
            return videos, page + 1, True
        time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
    return videos, next_pn, False


def fetch_video_detail(bvid: str) -> dict[str, object]:
    """Fetch title/cid/pubdate/duration/owner for one video via the view API."""
    response = requests.get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data["code"] != 0:
        raise DigestError(f"view API {data['code']}: {data['message']}")
    info = data["data"]
    return {
        "cid": str(info["cid"]),
        "title": info["title"],
        "pubdate": int(info["pubdate"]),
        "duration": int(info["duration"]),
        "owner": info["owner"]["name"],
    }


def safe_filename(name: str) -> str:
    """Strip characters that are illegal in Windows file names."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def archive_note(owner: str, bvid: str, detail: dict[str, object], summary: str) -> Path:
    """Write the summary markdown to notes/{owner}/{date}_{bvid}.md."""
    date_str = datetime.fromtimestamp(int(detail["pubdate"])).strftime("%Y-%m-%d")
    owner_dir = NOTES_DIR / safe_filename(owner)
    owner_dir.mkdir(parents=True, exist_ok=True)
    out_path = owner_dir / f"{date_str}_{bvid}.md"
    minutes = int(detail["duration"]) // 60
    header = (
        f"# {detail['title']}\n\n"
        f"- UP主: {owner} | 日期: {date_str} | 时长: {minutes} 分钟\n"
        f"- 链接: https://www.bilibili.com/video/{bvid}\n\n---\n\n"
    )
    out_path.write_text(header + summary, encoding="utf-8")
    return out_path


def archive_exists(bvid: str, detail: dict[str, object]) -> bool:
    """True if the note file already exists on disk (idempotency check)."""
    date_str = datetime.fromtimestamp(int(detail["pubdate"])).strftime("%Y-%m-%d")
    owner_dir = NOTES_DIR / safe_filename(str(detail["owner"]))
    return (owner_dir / f"{date_str}_{bvid}.md").exists()


def _vision_prompt(template: str) -> str:
    """Scene-aware vision extraction: numbers for stock, on-screen text for tech."""
    return TECH_PROMPT if template == "tech" else VISION_PROMPT


def _collect_vision_summary(
    sessdata: str, bvid: str, detail: dict[str, object], prompt: str
) -> str | None:
    """Extract frames and run MIMO vision; return the vision summary text.

    Any failure degrades to a pure-subtitle run (the narration summary is
    never blocked by vision). Errors are printed to stderr.
    """
    try:
        stream_url, _, _ = get_stream_info(sessdata, bvid)
        extract_frames(stream_url, bvid, max_frames=DEFAULT_VISION_FRAMES)
        return summarize_frames(bvid, prompt=prompt)
    except (FrameExtractError, VisionError, requests.RequestException) as exc:
        print(f"    [vision] 降级纯字幕: {exc}", file=sys.stderr)
        return None


def process_video(
    sessdata: str,
    api_key: str,
    bvid: str,
    use_vision: bool = True,
    template: str = "stock",
) -> tuple[Path | None, dict[str, object]]:
    """Run the full subtitle→vision→summarize→archive chain for one video.

    Returns (None, detail) when the note already exists on disk.
    """
    detail = fetch_video_detail(bvid)
    if archive_exists(bvid, detail):
        return None, detail
    subtitle_text = fetch_subtitle_text(sessdata, bvid, str(detail["cid"]))
    vision_summary = (
        _collect_vision_summary(sessdata, bvid, detail, _vision_prompt(template))
        if use_vision else None
    )
    summary = summarize_subtitle(
        api_key,
        str(detail["owner"]),
        str(detail["title"]),
        subtitle_text,
        vision_summary,
        template,
    )
    # 纯字幕总结若标记多处疑似听错 (音译/疑为), 自动开画面核验重总结, 不硬猜
    if not use_vision and detect_suspicious(summary):
        print("    [vision] 字幕可疑, 自动画面核验重跑", file=sys.stderr)
        vision_summary = _collect_vision_summary(sessdata, bvid, detail, _vision_prompt(template))
        if vision_summary:
            summary = summarize_subtitle(
                api_key,
                str(detail["owner"]),
                str(detail["title"]),
                subtitle_text,
                vision_summary,
                template,
            )
    out_path = archive_note(str(detail["owner"]), bvid, detail, summary)
    return out_path, detail


def _mark_processed(
    processed: dict[str, object], bvid: str, detail: dict[str, object] | None, out_path: Path | None, no_subtitle: bool = False
) -> None:
    """Record a video in state so it is not re-attempted on later runs."""
    entry: dict[str, object] = {"pubdate": int(detail["pubdate"]) if detail else 0}
    if out_path is not None:
        entry["archived"] = str(out_path)
    elif no_subtitle:
        entry["no_subtitle"] = True
        entry["checked_at"] = int(time.time())
    processed[bvid] = entry


def _should_process(processed: dict[str, object], bvid: str) -> bool:
    """True when a video is unprocessed, was no-subtitle long enough ago to
    retry, or is a stale half-processed record (subtitle fetched but the
    archive step failed, leaving only pubdate in state)."""
    entry = processed.get(bvid)
    if entry is None:
        return True
    if entry.get("archived"):
        return False
    if entry.get("no_subtitle"):
        checked_at = int(entry.get("checked_at", 0))
        if time.time() - checked_at > RETRY_NO_SUBTITLE_SECONDS:
            return True
        return False
    return True


def _is_failure_line(line: str) -> bool:
    """True for report lines that indicate a failed video, creator, or run."""
    return any(k in line for k in ("失败", "崩溃", "Error", "error", "412"))


def _alert_failures(report_lines: list[str]) -> None:
    """Notify the user when a digest run had failures.

    Appends to tmp/alert.log and shows a desktop popup (Windows, scheduled-
    task friendly). Never raises — notification is best-effort.
    """
    failures = [line for line in report_lines if _is_failure_line(line)]
    if not failures:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    alert_path = PROJECT_ROOT / "tmp" / "alert.log"
    alert_path.parent.mkdir(exist_ok=True)
    with open(alert_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}]\n" + "\n".join(f"  {line}" for line in failures) + "\n")
    message = f"VideoDigest {ts} 失败 {len(failures)} 项:\n" + "\n".join(
        line[:100] for line in failures[:5]
    )
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "(New-Object -ComObject WScript.Shell).Popup("
        f"'{message.replace(chr(39), chr(39)*2)}', 30, 'VideoDigest', 64)",
    ]
    try:
        # popup auto-closes after 30s; timeout must outlive it
        subprocess.run(
            cmd, timeout=45, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        print(f"[alert] 弹窗失败: {exc}", file=sys.stderr)


def _handle_video(
    sessdata: str,
    api_key: str,
    bvid: str,
    processed: dict[str, object],
    report_lines: list[str],
    tag: str,
    use_vision: bool = True,
    template: str = "stock",
) -> None:
    """Process one video, record its outcome in state, and report it."""
    try:
        out_path, detail = process_video(sessdata, api_key, bvid, use_vision, template)
    except NoSubtitleError as exc:
        _mark_processed(processed, bvid, None, None, no_subtitle=True)
        report_lines.append(f"  {tag}跳过 {bvid} (无字幕, 已记 state): {exc}")
        return
    except (SubtitleError, requests.RequestException) as exc:
        report_lines.append(f"  {tag}失败 {bvid}: {exc}")
        return
    if out_path is None:
        _mark_processed(processed, bvid, detail, None)
        report_lines.append(f"  {tag}已存在 {bvid}, 补记 state")
        return
    _mark_processed(processed, bvid, detail, out_path)
    report_lines.append(f"  {tag}归档 {bvid} -> {out_path}")


def run_digest(
    max_videos: int = DEFAULT_MAX_VIDEOS,
    backfill_per_creator: int = 0,
    cutoff_str: str = DEFAULT_BACKFILL_CUTOFF,
    use_vision: bool = True,
) -> None:
    """Process new videos for all followed creators and print a report.

    With backfill_per_creator > 0, also scans older videos (from the last
    pagination offset in state) until cutoff_date, processing at most
    backfill_per_creator old videos per creator per run.
    """
    acquire_single_instance_lock()
    cutoff_ts = int(datetime.strptime(cutoff_str, "%Y-%m-%d").timestamp())
    report_lines: list[str] = []
    try:
        sessdata = load_sessdata()
        api_key = load_api_key()
        state = load_state()
        processed = state.setdefault("processed", {})
        backfill_state = state.setdefault("backfill", {})
        try:
            _run_creators(
                sessdata, api_key, processed, backfill_state,
                report_lines, max_videos, backfill_per_creator, cutoff_ts, use_vision,
            )
        finally:
            save_state(state)
        print("\n".join(report_lines))
        try:
            from review_queue import build_review_queue
            build_review_queue()
        except Exception as exc:
            print(f"[review_queue] 生成失败: {exc}", file=sys.stderr)
    except Exception as exc:
        report_lines.append(f"run 崩溃: {exc}")
        raise
    finally:
        _alert_failures(report_lines)


def _run_creators(
    sessdata: str,
    api_key: str,
    processed: dict[str, object],
    backfill_state: dict[str, object],
    report_lines: list[str],
    max_videos: int,
    backfill_per_creator: int,
    cutoff_ts: int,
    use_vision: bool,
) -> None:
    """Process daily-new and backfill videos for every creator.

    Each creator runs in isolation: DigestError / network failures for one
    creator (e.g. bilibili 412 risk control) are logged and skipped instead
    of aborting the whole run.
    """
    for creator in load_creators():
        name = str(creator["name"])
        try:
            _run_one_creator(
                sessdata, api_key, processed, backfill_state,
                report_lines, creator, max_videos, backfill_per_creator,
                cutoff_ts, use_vision,
            )
        except (DigestError, requests.RequestException) as exc:
            report_lines.append(f"[{name}] 失败隔离, 跳过本轮: {exc}")
            print(f"[{name}] 失败隔离, 跳过本轮: {exc}", file=sys.stderr)


def _run_one_creator(
    sessdata: str,
    api_key: str,
    processed: dict[str, object],
    backfill_state: dict[str, object],
    report_lines: list[str],
    creator: dict[str, object],
    max_videos: int,
    backfill_per_creator: int,
    cutoff_ts: int,
    use_vision: bool,
) -> None:
    """Process daily-new and backfill videos for one creator."""
    name = str(creator["name"])
    mid = int(creator["mid"])
    template = str(creator.get("template", "stock"))
    creator_vision = bool(creator.get("vision", False)) and use_vision
    videos = fetch_latest_videos(sessdata, mid, max_videos)
    new_videos = [v for v in videos if _should_process(processed, str(v["bvid"]))]
    report_lines.append(
        f"[{name}] 最近 {len(videos)} 条, 新 {len(new_videos)} 条, 模板={template}, 视觉={'开' if creator_vision else '关'}"
    )
    for video in new_videos:
        _handle_video(
            sessdata, api_key, str(video["bvid"]), processed, report_lines, "",
            creator_vision, template,
        )
    if backfill_per_creator <= 0 or not bool(creator.get("backfill", True)):
        return
    bf = backfill_state.setdefault(str(mid), {"next_pn": 1, "done": False})
    if bf["done"]:
        report_lines.append(f"[{name}] 回填已完成, 跳过")
        return
    bf_videos, next_pn, reached_end = fetch_paginated_videos(
        sessdata, mid, int(bf["next_pn"]), BACKFILL_MAX_PAGES_PER_RUN, cutoff_ts
    )
    bf["next_pn"] = next_pn
    candidates = [
        v for v in bf_videos
        if int(v["pubdate"]) >= cutoff_ts and _should_process(processed, str(v["bvid"]))
    ]
    done_this_run = 0
    for video in candidates:
        if done_this_run >= backfill_per_creator:
            break
        _handle_video(
            sessdata, api_key, str(video["bvid"]), processed, report_lines,
            "[回填]", creator_vision, template,
        )
        done_this_run += 1
    if reached_end:
        bf["done"] = True
    report_lines.append(
        f"[{name}] 回填进度: pn={bf['next_pn']} 完成={bf['done']} 本轮处理={done_this_run}"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Video digest for followed creators")
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX_VIDEOS,
        help="latest videos per creator in daily mode (default: %(default)s)",
    )
    parser.add_argument(
        "--backfill", nargs="?", const=DEFAULT_BACKFILL_PER_CREATOR, type=int, default=0,
        help=f"backfill N old videos per creator per run (bare flag uses {DEFAULT_BACKFILL_PER_CREATOR})",
    )
    parser.add_argument(
        "--cutoff", default=DEFAULT_BACKFILL_CUTOFF,
        help="backfill cutoff date, older videos are skipped (default: %(default)s)",
    )
    parser.add_argument(
        "--no-vision", action="store_true",
        help="skip frame extraction and MIMO vision (subtitle-only summary)",
    )
    args = parser.parse_args()
    run_digest(args.max, args.backfill, args.cutoff, use_vision=not args.no_vision)
