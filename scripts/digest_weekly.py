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
SEASON_PAGE_SIZE = 30
SEASON_MAX_PAGES = 10
# 合集准入三层: 补录策略(时间) → 标题筛(内容) → LLM 二次判断(语义)
SEASON_BACKFILL_MODES = ("all", "none")
SEASON_ADMIT_MAX_TOKENS = 8000
SEASON_ADMIT_PROMPT = """合集《{season}》近期发布的集是:
{refs}

现在新出现一集:
- 标题: {title}
- 简介: {desc}
- 时长: {minutes} 分钟

判断这一集是否属于该合集的主题范畴(博主可能把无关内容也塞进同一合集)。
只输出一行, 二选一:
收 | <一句话理由>
不收 | <一句话理由>"""

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
import bili_summarize  # noqa: E402  (合集准入二次判断复用其请求层)
from bili_summarize import (  # noqa: E402
    SummaryEmptyError,
    detect_suspicious,
    detect_template,
    load_api_key,
    summarize_subtitle,
)
from bili_media import enum_pages, fetch_audio, needs_asr_fallback  # noqa: E402
from video_frames import FrameExtractError, extract_frames, get_stream_info  # noqa: E402
from video_vision import TECH_PROMPT, VISION_PROMPT, VisionError, summarize_frames  # noqa: E402
from local_video_pipeline import extract_segments, transcribe_segments  # noqa: E402

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


def fetch_season_videos(sessdata: str, mid: int, season_id: int) -> tuple[list[dict[str, object]], str]:
    """Fetch every episode of one 合集 (season), in upload order.

    Returns (episodes, season_title) — the title comes from the same response's
    meta block and is used as the theme reference for LLM admission checks.
    The polymer endpoint needs no WBI signature; SESSDATA is sent anyway so
    the request carries the same login state as the rest of the pipeline.
    """
    session = requests.Session()
    session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    headers = dict(HEADERS, Referer=f"https://space.bilibili.com/{mid}/lists/{season_id}?type=season")
    videos: list[dict[str, object]] = []
    season_title = ""
    for page in range(1, SEASON_MAX_PAGES + 1):
        response = session.get(
            "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
            params={
                "mid": mid,
                "season_id": season_id,
                "sort_reverse": "false",
                "page_num": page,
                "page_size": SEASON_PAGE_SIZE,
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data["code"] != 0:
            raise DigestError(f"season API {data['code']}: {data['message']}")
        archives = (data.get("data") or {}).get("archives") or []
        if page == 1:
            season_title = str(((data.get("data") or {}).get("meta") or {}).get("title") or "")
        if not archives:
            break
        videos.extend(
            {"bvid": item["bvid"], "title": item["title"], "pubdate": int(item["pubdate"])}
            for item in archives
        )
        if len(archives) < SEASON_PAGE_SIZE:
            break
        time.sleep(BACKFILL_PAGE_SLEEP_SECONDS)
    if len(videos) >= SEASON_MAX_PAGES * SEASON_PAGE_SIZE:
        # 撞到上限说明合集可能还有更多集, 别静默截断
        print(
            f"[season] 已达分页上限 {SEASON_MAX_PAGES}×{SEASON_PAGE_SIZE}, 合集可能被截断",
            file=sys.stderr,
        )
    return videos, season_title


def season_pubdate_floor(spec: object, season_id: int, state: dict[str, object]) -> int:
    """合集补录策略 → 发布时间下限 (unix ts)。0 = 不设限。

    spec: "all"(默认, 全集补齐, 适合教程/系统课) / "none"(只收启用之后的新集) /
          "since:YYYY-MM-DD"(只收该日及之后)。资讯类合集必须显式声明, 否则会倒灌旧闻。
    """
    if spec is None or spec == "all":
        return 0
    if spec == "none":
        # 首次运行把"启用时刻"落 state 作为基线, 之后只收更新的
        baseline = state.setdefault("season_baseline", {})
        key = str(season_id)
        if key not in baseline:
            baseline[key] = int(time.time())
        return int(baseline[key])
    if isinstance(spec, str) and spec.startswith("since:"):
        try:
            return int(datetime.strptime(spec[6:].strip(), "%Y-%m-%d").timestamp())
        except ValueError as exc:
            raise DigestError(f"season_backfill 日期格式错误: {spec!r} (应为 since:YYYY-MM-DD)") from exc
    raise DigestError(f"season_backfill 非法: {spec!r} (可选 all / none / since:YYYY-MM-DD)")


def season_title_ok(spec: object, title: str) -> bool:
    """标题准入: 空=全收; "regex:..."=正则; 其他=子串包含。"""
    if not spec:
        return True
    if isinstance(spec, str) and spec.startswith("regex:"):
        return re.search(spec[6:], title) is not None
    return str(spec) in title


def season_llm_admit(
    api_key: str,
    season_title: str,
    ref_titles: list[str],
    video: dict[str, object],
    cache: dict[str, object],
) -> tuple[bool, str]:
    """二次判断: 该集是否属于合集主题。结果按 bvid 缓存, 不重复判定。"""
    bvid = str(video["bvid"])
    if bvid in cache:
        hit = cache[bvid]
        assert isinstance(hit, dict)
        return bool(hit.get("admit")), str(hit.get("reason") or "")
    messages = [
        {
            "role": "user",
            "content": SEASON_ADMIT_PROMPT.format(
                season=season_title,
                refs="\n".join(f"- {t}" for t in ref_titles) or "(无参考)",
                title=video["title"],
                desc=str(video.get("desc") or "未提供")[:200],
                minutes=int(video.get("duration") or 0) // 60,
            ),
        }
    ]
    reply = bili_summarize._post_completions(
        api_key, bili_summarize.MODEL, messages, max_tokens=SEASON_ADMIT_MAX_TOKENS
    ).strip()
    admit = reply.startswith("收")
    reason = reply.split("|", 1)[1].strip()[:80] if "|" in reply else reply[:80]
    cache[bvid] = {"admit": admit, "reason": reason}
    return admit, reason


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
        "desc": info.get("desc", ""),
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


def find_archive(bvid: str, detail: dict[str, object]) -> Path | None:
    """Return the note path if it already exists on disk, else None."""
    date_str = datetime.fromtimestamp(int(detail["pubdate"])).strftime("%Y-%m-%d")
    owner_dir = NOTES_DIR / safe_filename(str(detail["owner"]))
    path = owner_dir / f"{date_str}_{bvid}.md"
    return path if path.exists() else None


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
        # 传 sessdata: 抽帧失败时刷新 URL 重试 (dash URL 带 deadline)
        extract_frames(stream_url, bvid, max_frames=DEFAULT_VISION_FRAMES, sessdata=sessdata)
        return summarize_frames(bvid, prompt=prompt)
    except (FrameExtractError, VisionError, requests.RequestException) as exc:
        print(f"    [vision] 降级纯字幕: {exc}", file=sys.stderr)
        return None


def process_video(
    sessdata: str,
    api_key: str,
    bvid: str,
    use_vision: bool = True,
    template: str | None = None,
) -> tuple[Path | None, dict[str, object]]:
    """Run the full subtitle→vision→summarize→archive chain for one video.

    template None → auto-detect by content type (LLM classify from
    title/desc/subtitle head) after the subtitle is fetched.
    Returns (None, detail) when the note already exists on disk.
    """
    detail = fetch_video_detail(bvid)
    if find_archive(bvid, detail):
        return None, detail
    pages = enum_pages(bvid)
    main_p = max(pages, key=lambda p: p["duration"])  # 主 P = 时长最长
    subtitle_text = fetch_subtitle_text(sessdata, bvid, str(main_p["cid"]))

    # 多P: 其余 P 有字幕则拉取, 差异 (含内容/长度) 追加到笔记
    p_notes: list[str] = []
    for p in pages:
        if p["cid"] == main_p["cid"]:
            continue
        try:
            t = fetch_subtitle_text(sessdata, bvid, str(p["cid"]))
            if t and len(t) > 100:
                p_notes.append(f"- P{p['cid']} (时长 {p['duration']}s, 字幕 {len(t)} 字符)")
        except (NoSubtitleError, SubtitleError):
            pass

    # 模板分流: 显式配置优先, 否则按内容类型自动分类 (2026-08-29 质量反馈)
    if template is None:
        template = detect_template(
            api_key, str(detail["title"]), str(detail.get("desc") or ""), subtitle_text[:800]
        )
        print(f"    [template] 自动检测: {template}", file=sys.stderr)

    # 长视频字幕覆盖不足 → ASR 兜底 (dash 音频 + 分段转写, 复用 lecture 管线)
    subtitle_source = "bili-ai"
    if needs_asr_fallback(subtitle_text, int(detail["duration"])):
        print("    [asr] 字幕覆盖不足, ASR 兜底", file=sys.stderr)
        try:
            mp3 = fetch_audio(bvid, int(main_p["cid"]))
            segs = extract_segments(mp3, PROJECT_ROOT / "tmp" / f"{bvid}_asr", 5, False)
            transcript, failures = transcribe_segments(segs, "zh", 5, False)
            if transcript and not failures:
                subtitle_text = transcript
                subtitle_source = "mimo-asr"
        except Exception as exc:  # 兜底失败降级纯字幕, 不阻塞 run
            print(f"    [asr] 兜底失败: {exc}", file=sys.stderr)

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
        str(detail.get("desc") or ""),
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
                str(detail.get("desc") or ""),
            )
    # 短视频内容密度提示 (笔记质量审计 P3): <2min 标薄内容
    if int(detail["duration"]) < 120:
        summary = summary.rstrip() + (
            f"\n\n> ⚠️ 短视频 ({detail['duration']}s): 内容密度低, "
            "如无独立信息价值可考虑不入库\n"
        )
    extra = []
    if subtitle_source == "mimo-asr":
        extra.append("> 字幕来源: MIMO ASR 兜底 (B站 AI 字幕覆盖不足, 无时间戳)")
    if p_notes:
        extra.append("## P 对照说明\n" + "\n".join(p_notes))
    if extra:
        summary = summary.rstrip() + "\n\n---\n\n" + "\n\n".join(extra)
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
    """True for report lines that indicate a failed video, creator, or run.

    412 风控的失败隔离是自愈型 (下轮自动重试), 不弹窗打扰;
    其余失败 (崩溃/空输出/视频错误) 弹窗通知。
    """
    if "失败隔离" in line and "412" in line:
        return False
    return any(k in line for k in ("失败", "崩溃", "Error", "error"))


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
        # 笔记已存在: 补记实际路径, 否则 _should_process 每次运行都重复检查
        _mark_processed(processed, bvid, detail, find_archive(bvid, detail))
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
                sessdata, api_key, processed, backfill_state, state,
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
    state: dict[str, object],
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
                sessdata, api_key, processed, backfill_state, state,
                report_lines, creator, max_videos, backfill_per_creator,
                cutoff_ts, use_vision,
            )
        except (DigestError, requests.RequestException, SummaryEmptyError) as exc:
            report_lines.append(f"[{name}] 失败隔离, 跳过本轮: {exc}")
            print(f"[{name}] 失败隔离, 跳过本轮: {exc}", file=sys.stderr)


def _run_one_creator(
    sessdata: str,
    api_key: str,
    processed: dict[str, object],
    backfill_state: dict[str, object],
    state: dict[str, object],
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
    template = creator.get("template")  # None → 按内容类型自动检测 (2026-08-29)
    creator_vision = bool(creator.get("vision", False)) and use_vision
    season_id = creator.get("season_id")
    if season_id is not None:
        # season_id 填 0/负数会被当成"没配", 静默退化成跟整个 UP 主 feed → 直接报错更安全
        if int(season_id) <= 0:
            raise DigestError(f"season_id 非法: {season_id} (需为正整数合集 id)")
        # 合集模式: 只跟这一个合集的新集, 忽略该 UP 主的其余投稿
        videos, season_title = fetch_season_videos(sessdata, mid, int(season_id))
        pending = [v for v in videos if _should_process(processed, str(v["bvid"]))]
        # 三层准入: 补录策略(时间) → 标题筛(内容) → LLM 二次判断(语义)
        backfill_spec = creator.get("season_backfill")
        floor = season_pubdate_floor(backfill_spec, int(season_id), state)
        if floor:
            pending = [v for v in pending if int(v["pubdate"]) >= floor]
        title_spec = creator.get("season_filter")
        if title_spec:
            pending = [v for v in pending if season_title_ok(title_spec, str(v["title"]))]
        if creator.get("season_llm_filter"):
            verdicts = state.setdefault("season_verdicts", {})
            assert isinstance(verdicts, dict)
            refs = [str(v["title"]) for v in videos[-8:]]
            kept: list[dict[str, object]] = []
            for video in pending:
                ok, reason = season_llm_admit(api_key, season_title, refs, video, verdicts)
                if ok:
                    kept.append(video)
                else:
                    report_lines.append(
                        f"  [LLM 过滤] {video['bvid']} {str(video['title'])[:32]} — {reason}"
                    )
            pending = kept
        # 首轮补齐历史集时按 max_videos 限流, 不一次跑几十集
        new_videos = pending[:max_videos]
        source = f"合集 {len(videos)} 集"
        if backfill_spec or title_spec or creator.get("season_llm_filter"):
            marks = [f"补录={backfill_spec or 'all'}"]
            if title_spec:
                marks.append("标题筛")
            if creator.get("season_llm_filter"):
                marks.append("LLM判")
            source += f" ({' / '.join(marks)})"
        if len(pending) > len(new_videos):
            source += f", 待处理 {len(pending)} 集(本轮上限 {max_videos})"
    else:
        videos = fetch_latest_videos(sessdata, mid, max_videos)
        source = f"最近 {len(videos)} 条"
        new_videos = [v for v in videos if _should_process(processed, str(v["bvid"]))]
    report_lines.append(
        f"[{name}] {source}, 新 {len(new_videos)} 条, "
        f"模板={template or 'auto'}, 视觉={'开' if creator_vision else '关'}"
    )
    for video in new_videos:
        _handle_video(
            sessdata, api_key, str(video["bvid"]), processed, report_lines, "",
            creator_vision, template,
        )
    if backfill_per_creator <= 0 or season_id or not bool(creator.get("backfill", True)):
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
