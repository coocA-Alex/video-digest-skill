"""Local video pipeline: video -> MIMO ASR -> summary -> archived note.

Handles local video files (not bilibili): probes the audio track, splits
audio into mp3 segments under the MiMo ASR 10MB limit, transcribes via
mimo-v2.5-asr, optionally reads frames via MIMO vision, then summarizes
with the general template and archives to notes/{owner}/.

Re-runs are incremental: successful segment transcripts are cached, so a
failed run can simply be re-invoked to retry only the missing segments.

Usage:
    python local_video_pipeline.py <video> [--no-vision] [--segment-min 5]
        [--template general] [--owner 讲座] [--language auto] [--force]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
NOTES_DIR = PROJECT_ROOT / "notes"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bili_summarize  # noqa: E402
import mimo_asr  # noqa: E402
import video_vision  # noqa: E402

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
DEFAULT_SEGMENT_MIN = 5
DEFAULT_TEMPLATE = "general"
DEFAULT_OWNER = "讲座"
MAX_VISION_FRAMES = 20
VISION_WIDTH = 1280
# 示例实测 MIMO ASR 约实时处理, 段长 x2 + 120s 缓冲, 防 5min 段被 timeout 砍断
TIMEOUT_FACTOR = 2
TIMEOUT_PADDING = 120


class PipelineError(Exception):
    """Raised when the pipeline cannot proceed."""


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _has_audio_stream(video: Path) -> bool:
    result = _run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "json", str(video)],
        60,
    )
    if result.returncode != 0:
        raise PipelineError(f"ffprobe failed: {result.stderr[-300:]}")
    return bool(json.loads(result.stdout or "{}").get("streams", []))


def _duration(video: Path) -> float:
    result = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)], 60)
    return float(json.loads(result.stdout)["format"]["duration"])


def _hash_prefix(video: Path, n: int = 8) -> str:
    h = hashlib.md5()
    with open(video, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()[:n]


def extract_segments(video: Path, seg_dir: Path, seg_min: int, force: bool) -> list[Path]:
    """Split the audio track into mp3 segments, each well under 10MB base64."""
    segs = sorted(seg_dir.glob("seg_*.mp3")) if seg_dir.exists() else []
    if segs and not force:
        return segs
    seg_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-i", str(video), "-vn",
        "-f", "segment", "-segment_time", str(seg_min * 60),
        "-ar", "16000", "-ac", "1",
        "-acodec", "libmp3lame", "-b:a", "128k",
        str(seg_dir / "seg_%03d.mp3"),
    ]
    result = _run(cmd, 7200)
    if result.returncode != 0:
        raise PipelineError(f"ffmpeg segment failed: {result.stderr[-500:]}")
    segs = sorted(seg_dir.glob("seg_*.mp3"))
    if not segs:
        raise PipelineError("no audio segments produced (empty audio track?)")
    return segs


def transcribe_segments(
    segs: list[Path], language: str, seg_min: int, force: bool
) -> tuple[str, list[str]]:
    """Transcribe each segment to seg_XXX.txt (cached); returns (transcript, failures)."""
    failures: list[str] = []
    for i, seg in enumerate(segs):
        out_txt = seg.with_suffix(".txt")
        if out_txt.exists() and not force:
            continue
        timeout = seg_min * 60 * TIMEOUT_FACTOR + TIMEOUT_PADDING
        print(f"  转写 {seg.name} ({i + 1}/{len(segs)}) ...")
        try:
            text = mimo_asr.analyze_audio(str(seg), language=language, timeout=timeout)
            out_txt.write_text(text, encoding="utf-8")
        except Exception as exc:
            failures.append(f"{seg.name}: {exc}")
    parts = [t.read_text(encoding="utf-8").strip() for t in (s.with_suffix(".txt") for s in segs) if t.exists()]
    transcript = "\n\n".join(parts)
    return transcript, failures


def extract_frames_local(video: Path, key: str, frames_dir: Path, duration: float, force: bool) -> Path:
    """Uniformly sample frames (duration/20) with the MIMO-compatible manifest format."""
    manifest_path = frames_dir / "manifest.json"
    if manifest_path.exists() and not force:
        return manifest_path
    frames_dir.mkdir(parents=True, exist_ok=True)
    interval = max(1.0, duration / MAX_VISION_FRAMES)
    cmd = [
        FFMPEG, "-y", "-i", str(video),
        "-vf", f"fps=1/{interval},scale={VISION_WIDTH}:-2",
        "-frames:v", str(MAX_VISION_FRAMES), "-q:v", "4",
        str(frames_dir / "frame_%03d.jpg"),
    ]
    result = _run(cmd, 1800)
    if result.returncode != 0:
        raise PipelineError(f"ffmpeg frame extract failed: {result.stderr[-500:]}")
    frames = sorted(frames_dir.glob("frame_*.jpg"))
    manifest = {
        "bvid": key,
        "interval": interval,
        "frames": [
            {"file": f.name, "time_sec": int(i * interval), "time_str": f"{int(i * interval) // 60:02d}:{int(i * interval) % 60:02d}"}
            for i, f in enumerate(frames)
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {len(frames)} 帧 -> {frames_dir}")
    return manifest_path


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python local_video_pipeline.py <video> [--no-vision] [--segment-min 5] [--template general] [--owner 讲座] [--language auto] [--force]")
        sys.exit(1)
    video = Path(sys.argv[1])
    no_vision = False
    seg_min, template, owner, language = DEFAULT_SEGMENT_MIN, DEFAULT_TEMPLATE, DEFAULT_OWNER, "auto"
    force = False
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--no-vision":
            no_vision = True
        elif args[i] == "--segment-min":
            seg_min = int(args[i + 1]); i += 1
        elif args[i] == "--template":
            template = args[i + 1]; i += 1
        elif args[i] == "--owner":
            owner = args[i + 1]; i += 1
        elif args[i] == "--language":
            language = args[i + 1]; i += 1
        elif args[i] == "--force":
            force = True
        else:
            print(f"unknown arg: {args[i]}"); sys.exit(1)
        i += 1

    if not video.exists():
        print(f"video not found: {video}"); sys.exit(1)
    if template not in bili_summarize.TEMPLATES:
        print(f"unknown template '{template}', available: {list(bili_summarize.TEMPLATES)}"); sys.exit(1)

    try:
        print(f"[1/5] 预检音轨 {video.name} ...")
        if not _has_audio_stream(video):
            raise PipelineError("video has no audio track")
        duration = _duration(video)
        print(f"      时长 {int(duration)}s, 分段 {seg_min}min/段")

        key = _hash_prefix(video)
        work_dir = TMP_DIR / "lecture" / f"{key}_{video.stem}"
        seg_dir = work_dir / "audio"

        print(f"[2/5] 抽音轨分段 ...")
        segs = extract_segments(video, seg_dir, seg_min, force)
        print(f"      {len(segs)} 段 -> {seg_dir}")

        print(f"[3/5] MIMO ASR 逐段转写 (language={language}) ...")
        transcript, failures = transcribe_segments(segs, language, seg_min, force)
        if failures:
            print(f"      ⚠️ {len(failures)} 段失败: {failures[0]}")
            print("      ↳ 重跑本命令即可增量重试失败段(已有段缓存跳过)")
        if not transcript.strip():
            raise PipelineError("transcript is empty; fix the failed segments and re-run")

        vision_summary = None
        if not no_vision:
            print(f"[4/5] 画面抽帧 + MIMO 读帧 ...")
            frames_dir = work_dir / "frames"
            extract_frames_local(video, key, frames_dir, duration, force)
            try:
                vision_summary = video_vision.summarize_frames(key, force=force, frames_dir=frames_dir)
                print(f"      画面摘要 {len(vision_summary)} 字")
            except Exception as exc:
                print(f"      ⚠️ 画面分析失败(降级纯字幕): {exc}")

        print(f"[5/5] 总结归档 (template={template}) ...")
        out_dir = NOTES_DIR / owner
        out_path = out_dir / f"{datetime.now().strftime('%Y-%m-%d')}_{video.stem}.md"
        if out_path.exists() and not force:
            print(f"      已存在, 跳过: {out_path}")
        else:
            api_key = bili_summarize.load_api_key()
            summary = bili_summarize.summarize_subtitle(
                api_key, owner, video.stem, transcript, vision_summary, template
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(summary, encoding="utf-8")
            print(f"      归档 -> {out_path}")

        print("done.")
    except PipelineError as exc:
        print(f"error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
