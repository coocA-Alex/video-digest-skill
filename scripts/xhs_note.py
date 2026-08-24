# === 小红书推文/视频解析管线 ===
# 用法: python scripts/xhs_note.py <explore_url> [--name noteX] [--extract]
# 凭证: web_session 从 ~/.xhs_web_session 读取 (仓库外), 不打印不落盘
# 产物: tmp/xhs_{name}/ (meta.json + video/images + asr/img_text 可选)
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

WS_PATH = Path.home() / ".xhs_web_session"
TMP_ROOT = Path(__file__).resolve().parent.parent / "tmp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.xiaohongshu.com/",
}

STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*\})\s*</script>", re.S)


def _load_session() -> requests.Session:
    if not WS_PATH.exists():
        raise SystemExit(f"web_session 文件缺失: {WS_PATH} (请让用户重新提供)")
    ws = WS_PATH.read_text(encoding="utf-8").strip()
    s = requests.Session()
    s.cookies.set("web_session", ws, domain=".xiaohongshu.com")
    return s


def fetch_note(url: str) -> dict:
    """请求 explore 页面并解析 note 元数据 + 媒体 URL。"""
    s = _load_session()
    r = s.get(url, headers=HEADERS, timeout=30)
    m = STATE_RE.search(r.text)
    if not m:
        raise SystemExit(f"未找到 __INITIAL_STATE__ (status {r.status_code}, 可能风控或失效)")
    state = json.loads(m.group(1).replace("undefined", "null"))
    note_map = state.get("note", {}).get("noteDetailMap", {})
    if not note_map:
        raise SystemExit("noteDetailMap 为空 (登录态失效?)")
    note = list(note_map.values())[0].get("note", {})
    info = {
        "noteId": note.get("noteId"),
        "type": note.get("type"),
        "title": (note.get("title") or "").strip() or (note.get("desc") or "").split("\n")[0][:80],
        "desc": note.get("desc", ""),
        "tags": [t.get("name") for t in note.get("tagList", [])],
        "interact": {
            k: note.get("interactInfo", {}).get(k)
            for k in ("likedCount", "collectedCount", "commentCount")
        },
        "n_images": len(note.get("imageList", [])),
        "image_urls": [
            img.get("urlDefault") or img.get("url") or ""
            for img in note.get("imageList", [])
            if img.get("urlDefault") or img.get("url")
        ],
    }
    media = (note.get("video") or {}).get("media") or {}
    h264 = (media.get("stream") or {}).get("h264") or [None]
    if h264 and h264[0]:
        info["video_url"] = h264[0].get("masterUrl", "").replace("\\u002F", "/")
    else:
        info["video_url"] = None
    return info


def download_media(name: str, info: dict) -> Path:
    """下载视频/图片到 tmp/xhs_{name}/。"""
    out_dir = TMP_ROOT / f"xhs_{name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update(HEADERS)
    if info.get("video_url"):
        r = s.get(info["video_url"], timeout=90)
        if r.status_code == 200:
            (out_dir / "video.mp4").write_bytes(r.content)
            print(f"video: {len(r.content)} bytes -> {out_dir / 'video.mp4'}")
    for i, u in enumerate(info.get("image_urls", [])):
        try:
            r = s.get(u, timeout=40)
            (out_dir / f"img{i}.jpg").write_bytes(r.content)
            print(f"img{i}: {len(r.content)} bytes")
        except requests.RequestException as e:
            print(f"img{i}: ERR {e}")
    return out_dir


def extract_content(name: str, out_dir: Path) -> None:
    """视频 -> 音频 ASR + 帧; 图片 -> MIMO 视觉提取文字。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    video = out_dir / "video.mp4"
    audio = out_dir / "audio.mp3"
    if video.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
             "-ar", "16000", "-b:a", "64k", str(audio)],
            check=False,
        )
        if audio.exists():
            from mimo_asr import analyze_audio
            text = analyze_audio(str(audio), "auto")
            (out_dir / "asr.txt").write_text(text, encoding="utf-8")
            print(f"asr: {len(text)} chars -> {out_dir / 'asr.txt'}")
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", "fps=1",
             "-q:v", "3", str(frames_dir / "f_%02d.jpg")],
            check=False,
        )
        print(f"frames: {len(list(frames_dir.glob('f_*.jpg')))}")
    images = sorted(out_dir.glob("img*.jpg"))
    if images:
        from mimo_vision import analyze_images
        prompt = "逐张提取图片中的完整文本内容(原文逐字), 若含图表/公式请描述结构。输出格式: 图N: [提取文本]"
        text = analyze_images([str(p) for p in images], prompt)
        if isinstance(text, str):
            (out_dir / "img_text.txt").write_text(text, encoding="utf-8")
            print(f"img_text: {len(text)} chars -> {out_dir / 'img_text.txt'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="小红书推文/视频解析")
    ap.add_argument("url", help="explore 页面链接 (含 xsec_token)")
    ap.add_argument("--name", default="note", help="产物目录名, 默认 note")
    ap.add_argument("--extract", action="store_true", help="下载后执行 ASR/画面提取")
    args = ap.parse_args()

    info = fetch_note(args.url)
    meta_path = TMP_ROOT / f"xhs_{args.name}_meta.json"
    meta_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"type={info['type']} title={info['title'][:50]} "
          f"images={info['n_images']} video={'Y' if info['video_url'] else 'N'}")
    print(f"meta -> {meta_path}")

    if args.extract:
        out_dir = download_media(args.name, info)
        extract_content(args.name, out_dir)


if __name__ == "__main__":
    main()
