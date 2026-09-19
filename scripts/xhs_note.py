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

# --- ASR 幻觉校验 (2026-09-14) ---
# 纯 BGM 音轨上 ASR 模型会编造完整歌词 (实测输出与笔记内容完全无关的英文歌词), 且不报错。
# 音频侧判据实测不可用: 口播下面普遍垫 BGM, "静音比例"在 7 个样本上全为 0 (含 3 个真口播)。
# 改用输出侧判据, 实测分离干净 (9 个真口播样本 vs 1 个幻觉样本):
#   中文字符占比: 幻觉 0%      vs 真口播 71-97%
#   重复 3-gram:  幻觉 11.5%   vs 真口播 9/9 全为 0%
ASR_CJK_MIN = 0.3      # ASR 中文占比低于此值, 且笔记以中文为主 -> 语言不符
NOTE_CJK_MIN = 0.5
ASR_REPEAT_MAX = 0.03  # 重复 3-gram 超此比例 -> 疑点
CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z]")
WORD_RE = re.compile(r"[a-z一-鿿]+")


def _cjk_ratio(text: str) -> float:
    cjk, lat = len(CJK_RE.findall(text)), len(LATIN_RE.findall(text))
    return cjk / (cjk + lat) if cjk + lat else 0.0


def _repeat_ratio(text: str) -> float:
    w = WORD_RE.findall(text.lower())
    if len(w) < 4:
        return 0.0
    tri = [" ".join(w[i:i + 3]) for i in range(len(w) - 2)]
    return 1 - len(set(tri)) / len(tri)


def asr_suspect(text: str, note_text: str) -> list[str]:
    """ASR 文本可信度疑点 (空列表 = 可信)。命中即判幻觉, 不写 asr.txt。"""
    sus = []
    c_asr, c_note = _cjk_ratio(text), _cjk_ratio(note_text)
    if c_note > NOTE_CJK_MIN and c_asr < ASR_CJK_MIN:
        sus.append(f"语言不符 (ASR 中文占比 {c_asr:.0%}, 笔记 {c_note:.0%})")
    rep = _repeat_ratio(text)
    if rep > ASR_REPEAT_MAX:
        sus.append(f"高频重复 3-gram {rep:.1%}")
    return sus


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


def extract_content(name: str, out_dir: Path, info: dict | None = None,
                    force_asr: bool = False) -> None:
    """视频 -> 音频 ASR + 帧; 图片 -> MIMO 视觉提取文字。

    ASR 判为幻觉时不写 asr.txt (下游不消费), 原文另存 asr_suspect.txt 供审计,
    并自动转视觉型内容解读 (visual_palette)。
    """
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
            from asr import analyze_audio
            text = analyze_audio(str(audio), "auto")
            note_text = f"{info.get('title', '')} {info.get('desc', '')}" if info else ""
            sus = [] if force_asr else asr_suspect(text, note_text)
            if sus:
                (out_dir / "asr_suspect.txt").write_text(
                    "判为 ASR 幻觉, 未采用。疑点: " + "; ".join(sus) + "\n\n" + text,
                    encoding="utf-8")
                print(f"asr: 判为幻觉已丢弃 ({'; '.join(sus)})")
                print(f"asr: 原文留档 -> {out_dir / 'asr_suspect.txt'}")
                palette = Path(__file__).resolve().parent / "visual_palette.py"
                r = subprocess.run([sys.executable, str(palette), str(video),
                                    "--name", name, "--labels"],
                                   capture_output=True, text=True)
                out = [l for l in (r.stdout or r.stderr).strip().splitlines() if l.strip()]
                # 首行是 "关键帧 N 个 | ..." 摘要, 末行才是路径
                print(f"视觉解读: {out[0] if out else '失败'}")
                if len(out) > 1:
                    print(f"视觉解读: {out[-2] if len(out) > 2 else out[-1]}")
            else:
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
        from vision import analyze_images
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
    ap.add_argument("--force-asr", action="store_true",
                    help="跳过 ASR 幻觉校验, 强制采用 ASR 文本 (逃生口)")
    args = ap.parse_args()

    info = fetch_note(args.url)
    meta_path = TMP_ROOT / f"xhs_{args.name}_meta.json"
    meta_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"type={info['type']} title={info['title'][:50]} "
          f"images={info['n_images']} video={'Y' if info['video_url'] else 'N'}")
    print(f"meta -> {meta_path}")

    if args.extract:
        out_dir = download_media(args.name, info)
        extract_content(args.name, out_dir, info, args.force_asr)


if __name__ == "__main__":
    main()
