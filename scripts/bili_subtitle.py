"""Fetch bilibili AI subtitles using SESSDATA cookie + WBI signing.

SESSDATA is read from ~/.bili_sessdata (outside the repo, never committed).
Subtitle text is written to the project's tmp/ directory.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.parse
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
SESSDATA_FILE = Path.home() / ".bili_sessdata"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com",
}


class SubtitleError(Exception):
    """Base class for subtitle fetching failures."""


class NoSubtitleError(SubtitleError):
    """Raised when the video has no usable subtitle track."""


def load_sessdata() -> str:
    """Read the SESSDATA value from the user-level cookie file."""
    if not SESSDATA_FILE.exists():
        raise SubtitleError(f"SESSDATA file missing: {SESSDATA_FILE}")
    sessdata = SESSDATA_FILE.read_text(encoding="utf-8").strip()
    if not sessdata:
        raise SubtitleError("SESSDATA file is empty")
    return sessdata


def get_wbi_keys(session: requests.Session) -> tuple[str, str]:
    """Fetch WBI image/sub keys from the nav API."""
    response = session.get("https://api.bilibili.com/x/web-interface/nav", headers=HEADERS)
    response.raise_for_status()
    wbi = response.json()["data"]["wbi_img"]
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def mixin_key(img_key: str, sub_key: str) -> str:
    """Derive the 32-char WBI mixin key from img and sub keys."""
    original = img_key + sub_key
    return "".join(original[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def signed_params(params: dict[str, str], mixin: str) -> str:
    """Append wts/w_rid and return the signed query string."""
    params["wts"] = str(int(time.time()))
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return urllib.parse.urlencode(params)


def _fmt_timestamp(seconds: float) -> str:
    """Format subtitle offset seconds as [mm:ss] for time-stamped citations."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def fetch_subtitle_text(sessdata: str, bvid: str, cid: str) -> str:
    """Return the full subtitle text for one video, lines prefixed with [mm:ss]."""
    session = requests.Session()
    session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    img_key, sub_key = get_wbi_keys(session)
    mixin = mixin_key(img_key, sub_key)
    query = signed_params({"bvid": bvid, "cid": cid}, mixin)
    response = session.get(
        f"https://api.bilibili.com/x/player/wbi/v2?{query}", headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    data = response.json()
    if data["code"] != 0:
        raise SubtitleError(f"player API {data['code']}: {data['message']}")
    subtitles = data["data"]["subtitle"]["subtitles"]
    if not subtitles:
        raise NoSubtitleError(f"no subtitle tracks for {bvid}")
    chosen = next((item for item in subtitles if item["lan"].startswith("ai")), subtitles[0])
    subtitle_url = chosen["subtitle_url"]
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    body = session.get(subtitle_url, headers=HEADERS, timeout=30).json()["body"]
    return "\n".join(
        f"[{_fmt_timestamp(float(item.get('from', 0.0)))}] {item['content']}"
        for item in body
    )


def main() -> None:
    """Fetch subtitle for bvid/cid from argv and write tmp/{bvid}.txt."""
    if len(sys.argv) != 3:
        print("usage: python bili_subtitle.py <bvid> <cid>")
        sys.exit(1)
    bvid, cid = sys.argv[1], sys.argv[2]
    try:
        text = fetch_subtitle_text(load_sessdata(), bvid, cid)
    except (SubtitleError, requests.RequestException) as exc:
        print(f"error: {exc}")
        sys.exit(1)
    TMP_DIR.mkdir(exist_ok=True)
    out_path = TMP_DIR / f"{bvid}.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"chars: {len(text)} -> {out_path}")


if __name__ == "__main__":
    main()
