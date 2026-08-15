"""Extract key frames from a bilibili video stream without downloading the full file.

Streams via ffmpeg directly from the playurl endpoint, saves a frame every
INTERVAL seconds into tmp/{bvid}_frames/, and writes manifest.json mapping
each frame to its timestamp. Frames are downscaled to <=WIDTH px (MIMO
vision API rejects images >10MB).

Usage:
    python video_frames.py <bvid> [--interval 15] [--max-frames 30] [--width 1280] [--force]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bili_subtitle import HEADERS, load_sessdata  # noqa: E402

FFMPEG = "ffmpeg"
DEFAULT_INTERVAL = 15
DEFAULT_MAX_FRAMES = 30
DEFAULT_WIDTH = 1280


class FrameExtractError(Exception):
    """Raised when frames cannot be extracted."""


def get_stream_info(sessdata: str, bvid: str) -> tuple[str, int, int]:
    """Return (stream_url, cid, duration_seconds) for one video."""
    session = requests.Session()
    session.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    view = session.get(
        "https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}, headers=HEADERS, timeout=15
    ).json()
    if view["code"] != 0:
        raise FrameExtractError(f"view API {view['code']}: {view['message']}")
    info = view["data"]
    cid, duration = int(info["cid"]), int(info["duration"])
    play = session.get(
        "https://api.bilibili.com/x/player/playurl",
        params={"bvid": bvid, "cid": cid, "qn": "64", "fnval": "1", "platform": "pc"},
        headers=HEADERS,
        timeout=15,
    ).json()
    durl = play.get("data", {}).get("durl")
    if not durl:
        raise FrameExtractError(f"no playable stream for {bvid}")
    return durl[0]["url"], cid, duration


def extract_frames(
    stream_url: str,
    bvid: str,
    interval: int = DEFAULT_INTERVAL,
    max_frames: int = DEFAULT_MAX_FRAMES,
    width: int = DEFAULT_WIDTH,
    force: bool = False,
) -> Path:
    """Stream the video and save frames to tmp/{bvid}_frames/, return manifest path."""
    out_dir = TMP_DIR / f"{bvid}_frames"
    if out_dir.exists() and any(out_dir.glob("frame_*.jpg")) and not force:
        print(f"frames already exist: {out_dir} (use --force to re-extract)")
        return out_dir / "manifest.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    headers_arg = (
        "Referer: https://www.bilibili.com\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
    )
    cmd = [
        FFMPEG, "-y",
        "-headers", headers_arg,
        "-i", stream_url,
        "-vf", f"fps=1/{interval},scale={width}:-2",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        str(out_dir / "frame_%03d.jpg"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise FrameExtractError(f"ffmpeg failed: {result.stderr[-500:]}")
    frames = sorted(out_dir.glob("frame_*.jpg"))
    manifest = {
        "bvid": bvid,
        "interval": interval,
        "frames": [
            {"file": f.name, "time_sec": i * interval, "time_str": _fmt(i * interval)}
            for i, f in enumerate(frames)
        ],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(frames)} frames -> {out_dir}")
    return manifest_path


def _fmt(sec: int) -> str:
    return f"{sec // 60:02d}:{sec % 60:02d}"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python video_frames.py <bvid> [--interval N] [--max-frames N] [--width N] [--force]")
        sys.exit(1)
    bvid = sys.argv[1]
    interval, max_frames, width, force = DEFAULT_INTERVAL, DEFAULT_MAX_FRAMES, DEFAULT_WIDTH, False
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--interval":
            interval = int(args[i + 1]); i += 1
        elif args[i] == "--max-frames":
            max_frames = int(args[i + 1]); i += 1
        elif args[i] == "--width":
            width = int(args[i + 1]); i += 1
        elif args[i] == "--force":
            force = True
        else:
            print(f"unknown arg: {args[i]}"); sys.exit(1)
        i += 1
    try:
        stream_url, cid, duration = get_stream_info(load_sessdata(), bvid)
        print(f"cid={cid} duration={duration}s interval={interval}s frames<= {max_frames}")
        extract_frames(stream_url, bvid, interval, max_frames, width, force)
    except (FrameExtractError, requests.RequestException) as exc:
        print(f"error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
