"""B站媒体层: 多P 枚举 / 字幕覆盖检测 / 音频下载 (ASR 兜底)。

用法:
    from bili_media import enum_pages, check_coverage, fetch_audio
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
SESSDATA_FILE = Path.home() / ".bili_sessdata"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}

AUDIO_DOWNLOAD_TIMEOUT = 1500
COVERAGE_ASR_THRESHOLD = 0.7
LONG_VIDEO_SECONDS = 20 * 60

_TS_RE = re.compile(r"^\[(\d{2}):(\d{2})(?::\d{2})?\]")


class BiliMediaError(Exception):
    """Base class for media-layer failures."""


def _session() -> requests.Session:
    if not SESSDATA_FILE.exists():
        raise BiliMediaError(f"SESSDATA file missing: {SESSDATA_FILE}")
    session = requests.Session()
    session.cookies.set("SESSDATA", SESSDATA_FILE.read_text(encoding="utf-8").strip(), domain=".bilibili.com")
    return session


def enum_pages(bvid: str) -> list[dict]:
    """Return all pages of one video: [{bvid, cid, part, duration}]. Single-P
    videos yield one entry; multi-P videos yield one per part."""
    session = _session()
    view = session.get(
        "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, headers=HEADERS, timeout=15
    ).json()
    if view["code"] != 0:
        raise BiliMediaError(f"view API {view['code']}: {view['message']}")
    info = view["data"]
    pages = info.get("pages") or [{"cid": info["cid"], "part": "", "duration": info["duration"]}]
    return [
        {"bvid": bvid, "cid": p["cid"], "part": p.get("part", ""), "duration": p.get("duration", 0)}
        for p in pages
    ]


def check_coverage(subtitle_text: str, duration_sec: float) -> float:
    """Ratio of last subtitle timestamp to video duration. Returns 1.0 when
    the subtitle has no [mm:ss] timestamps (cannot judge, assume complete)."""
    if duration_sec <= 0:
        return 1.0
    last_ts = 0.0
    for line in subtitle_text.splitlines():
        m = _TS_RE.match(line.strip())
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2))
            last_ts = max(last_ts, t)
    if last_ts <= 0:
        return 1.0
    return min(last_ts / duration_sec, 1.0)


def needs_asr_fallback(subtitle_text: str, duration_sec: float) -> bool:
    """True when the subtitle clearly under-covers a long video (B站 AI 字幕
    on long videos often only covers the narrated part)."""
    return duration_sec > LONG_VIDEO_SECONDS and check_coverage(subtitle_text, duration_sec) < COVERAGE_ASR_THRESHOLD


def fetch_audio(bvid: str, cid: int, out_mp3: Path | None = None, force: bool = False) -> Path:
    """Download the audio track (dash stream, fnval=16) to an mp3.

    Uses the verified dash+headers scheme (UA/Referer/Cookie); refreshes the
    stream URL on retry since it carries a deadline. Returns the mp3 path.
    """
    out_mp3 = out_mp3 or (TMP_DIR / f"{bvid}.mp3")
    if out_mp3.exists() and not force:
        return out_mp3
    session = _session()
    for attempt in range(3):
        play = session.get(
            "https://api.bilibili.com/x/player/playurl",
            params={"bvid": bvid, "cid": cid, "qn": "64", "fnval": "16", "platform": "pc"},
            headers=HEADERS,
            timeout=15,
        ).json()
        if play["code"] != 0:
            raise BiliMediaError(f"playurl {play['code']}: {play['message']}")
        try:
            url = play["data"]["dash"]["audio"][0]["baseUrl"]
        except (KeyError, IndexError):
            raise BiliMediaError(f"no dash audio for {bvid}")
        headers_arg = (
            "Referer: https://www.bilibili.com\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Cookie: SESSDATA={session.cookies.get('SESSDATA', domain='.bilibili.com')}\r\n"
        )
        r = subprocess.run(
            ["ffmpeg", "-y", "-headers", headers_arg, "-i", url,
             "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(out_mp3)],
            capture_output=True, text=True, timeout=AUDIO_DOWNLOAD_TIMEOUT,
        )
        if r.returncode == 0 and out_mp3.exists() and out_mp3.stat().st_size > 0:
            return out_mp3
        # URL 可能带 deadline 中途失效: 重试前删半成品并刷新 URL
        out_mp3.unlink(missing_ok=True)
        time.sleep(2 * (attempt + 1))
    raise BiliMediaError(f"audio download failed for {bvid} after 3 attempts")
