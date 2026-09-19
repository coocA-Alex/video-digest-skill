"""Extract palettes and keyframes from visual-only videos (no narration).

Made for videos whose content IS the visual: gradient cards, palette demos,
color comparisons. ASR is useless on these (music-only audio makes the ASR
model hallucinate lyrics), so the content has to be read off the pixels.

Pipeline: ffmpeg 抽帧 -> 平台期检测 (帧差) -> 关键帧调色板 (k-means) ->
HSL 设计规律 -> markdown + JSON. `--labels` additionally reads on-screen hex
codes with the vision model and checks each one really exists in the frame
(the labels in such videos sit on a moving gradient and animation makes the
text unreadable mid-transition, so only keyframes are sampled).

See docs/visual-palette.md for the method, the traps and the validation run.

Usage:
  python scripts/visual_palette.py <video|image> [--name x] [--fps 4]
                                   [--labels] [--out path.md] [--json-out path]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = PROJECT_ROOT / "tmp"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".flv"}
ANALYSIS_WIDTH = 192
GRID = 4
N_COLORS = 6

# 色号与采样色算同一色的门限 (欧氏距离, 0-255 空间). 2026-09-14 实测:
# 视频压缩噪声使同一色号偏差 <=8/通道, 即距离 <=14 -> 15 以内视为同色
LABEL_CONFIRM_DIST = 15.0

# 平色卡判定: 中位覆盖率 >= 此值 -> 色块渲染色与标注色号一致 ("平色卡")。
# 低于此值有两种可能, 由色号数区分 (见下): 幻觉, 或"渲染漂移卡"。
SWATCH_MATCH_MIN = 0.5
# 单色号占比低于此值 -> 标存疑交人工。取 0.2% (而非 0.5%): 平色卡上色块多的卡
# (如实测 10 色块一页) 每个占比本就只有 0.4% 左右, 0.5% 会大面积误报;
# 且 hex↔RGB 算术自洽已是主判据, 覆盖率只作补充。
REVIEW_COVERAGE = 0.2

# 幻觉闸门: 实测模型在"没有调色板的图"上会幻觉出整份 CSS 命名颜色表
# (主色 #FF0000/#00FF00/#808080 ...; 同一张图三次跑出 118/22/270 个)。
# 判据用"色号数"而非覆盖率: 实测真实色卡 5-10 个 vs 幻觉 22-270 个, 零重叠;
# 而覆盖率会把"渲染漂移卡"误判为幻觉 (见 docs/visual-palette.md)。
MAX_LABELS = 20
RAW_CAP = 1500

LABEL_PROMPT = (
    "读取这张配色卡的配色信息。只输出下面格式, 不要任何其他内容:\n"
    "名称: <画面中的配色主题名, 如'暮光紫金'; 画面里没有则留空>\n"
    "色号: #RRGGBB RGB(r,g,b)\n"
    "色号: #RRGGBB RGB(r,g,b)\n"
    "(每个颜色一行, 按画面从左到右顺序; 一行里同时给出十六进制色号和十进制 RGB)\n"
    "要求: 逐字符精确转录, 不要推测、不要补全、不要输出画面中不存在的颜色。"
)
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")
NAME_RE = re.compile(r"名称[:：]\s*(\S.*)")
RGB_RE = re.compile(r"(\d{1,3})\s*[,，]\s*(\d{1,3})\s*[,，]\s*(\d{1,3})")


def parse_color_lines(text: str) -> list[tuple[str | None, tuple[int, int, int] | None]]:
    """逐行解析 "色号: #RRGGBB RGB(r,g,b)" -> [(hex, rgb)]。

    图面同时印了 hex 和 RGB, 成对读出才能互校: hex 的字母易误读 (实测
    #376B9E 被读成 #3769E9/#37699E), RGB 是纯数字, 可靠得多。
    """
    out = []
    for line in text.splitlines():
        if "色号" not in line and "#" not in line:
            continue
        m_hex = HEX_RE.search(line)
        m_rgb = RGB_RE.search(line)
        rgb = None
        if m_rgb:
            vals = tuple(int(v) for v in m_rgb.groups())
            if all(v <= 255 for v in vals):
                rgb = vals
        if m_hex or rgb:
            out.append((m_hex.group(0).upper() if m_hex else None, rgb))
    return out


def reconcile(hex_raw: str | None, rgb_raw: tuple | None) -> tuple[str, str, str]:
    """-> (采用色号, 来源, 备注)。两源不一致时以印刷 RGB 反推 — 数字比字母可靠。"""
    if hex_raw and rgb_raw:
        derived = _hex(rgb_raw).upper()
        if derived == hex_raw:
            return hex_raw, "hex+RGB 一致", ""
        return derived, "以RGB修正", f"hex 读作 {hex_raw}, 与印刷 RGB{rgb_raw} 不符"
    if hex_raw:
        return hex_raw, "仅hex", "画面未印 RGB"
    return _hex(rgb_raw).upper(), "仅RGB", "画面未印 hex"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, round(v)))) for v in rgb)


def _from_hex(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def probe(path: Path) -> dict:
    """时长/分辨率/有无音轨。"""
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "csv=p=0", str(path)]).stdout.strip()
    info = {"duration": float(out) if out else 0.0}
    s = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "stream=width,height", "-of", "csv=p=0", str(path)]).stdout.strip()
    if s:
        parts = s.split(",")
        info["width"], info["height"] = int(parts[0]), int(parts[1])
    info["has_audio"] = bool(_run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                   "-show_entries", "stream=index", "-of", "csv=p=0",
                                   str(path)]).stdout.strip())
    return info


def extract_frames(src: Path, out_dir: Path, fps: float) -> list[dict]:
    """视频按 fps 抽小尺寸帧用于分析 (原图另按需从视频重取)。"""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("f_*.jpg"):
        old.unlink()
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
          f"fps={fps},scale={ANALYSIS_WIDTH}:-1", "-q:v", "3",
          str(frames_dir / "f_%05d.jpg")])
    paths = sorted(frames_dir.glob("f_*.jpg"))
    return [{"t": round(i / fps, 3), "path": p, "src": src} for i, p in enumerate(paths)]


def gallery_frames(srcs: list[Path], out_dir: Path) -> list[dict]:
    """图集笔记: 每张图 = 一张色卡 (原图保留给 --labels 读色号用)。"""
    frames_dir = out_dir / "gallery"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("g_*.jpg"):
        old.unlink()
    frames = []
    for i, p in enumerate(srcs, 1):
        thumb = frames_dir / f"g_{i:03d}.jpg"
        im = Image.open(p).convert("RGB")
        w = ANALYSIS_WIDTH
        im.resize((w, max(1, round(im.height * w / im.width))), Image.BILINEAR).save(
            thumb, quality=92)
        frames.append({"t": float(i), "path": thumb, "src": p})
    return frames


def frame_grid(path: Path) -> np.ndarray:
    """GRID×GRID 分块均值 -> 展平描述子, 比全图均值对局部变化更敏感。"""
    im = Image.open(path).convert("RGB").resize((GRID, GRID), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32).reshape(-1)


def find_plateaus(frames: list[dict], grids: np.ndarray, min_hold: float,
                  tol_ratio: float = 0.12, tol_min: float = 1.0) -> list[dict]:
    """找出画面稳定的"定格"区间 (关键帧候选), 阈值相对全片变化幅度自适应。

    定格期是画面文字唯一可读的时刻: 过渡期的色号文字在滚动/乱序。
    """
    if len(grids) < 3:
        return [{"t_start": frames[0]["t"], "t_end": frames[-1]["t"],
                 "t_key": frames[len(frames) // 2]["t"], "idx": len(frames) // 2,
                 "hold": round(frames[-1]["t"] - frames[0]["t"], 2)}]
    deltas = np.linalg.norm(np.diff(grids, axis=0), axis=1)
    active = deltas[deltas > tol_min]
    scale = float(np.percentile(active, 75)) if active.size else 0.0
    tol = max(tol_min, scale * tol_ratio)
    stable = deltas <= tol
    runs, start = [], None
    for i, ok in enumerate(stable):
        if ok and start is None:
            start = i
        if start is not None and (not ok or i == len(stable) - 1):
            end = i if not ok else i + 1
            if frames[end]["t"] - frames[start]["t"] >= min_hold:
                mid = (start + end) // 2
                runs.append({"t_start": frames[start]["t"], "t_end": frames[end]["t"],
                             "t_key": frames[mid]["t"], "idx": mid,
                             "hold": round(frames[end]["t"] - frames[start]["t"], 2)})
            start = None
    return runs


def _kmeans(px: np.ndarray, k: int, iters: int = 24, seed: int = 0):
    rng = np.random.default_rng(seed)
    k = max(1, min(k, len(px)))
    centers = px[rng.choice(len(px), k, replace=False)].copy()
    labels = np.full(len(px), -1, dtype=np.int32)
    for _ in range(iters):
        dist = ((px[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
        new = dist.argmin(1).astype(np.int32)
        if np.array_equal(new, labels):
            break
        labels = new
        for j in range(k):
            m = labels == j
            if m.any():
                centers[j] = px[m].mean(0)
    return centers, np.bincount(labels, minlength=k)


def extract_palette(img_path: Path, k: int = N_COLORS, max_px: int = 6000) -> list[dict]:
    """k-means 主色板, 按占比降序。"""
    im = Image.open(img_path).convert("RGB")
    px = np.asarray(im, dtype=np.float32).reshape(-1, 3)
    if len(px) > max_px:
        px = px[:: max(1, len(px) // max_px)]
    centers, counts = _kmeans(px, k)
    total = counts.sum()
    order = np.argsort(-counts)
    return [{"hex": _hex(centers[i]), "rgb": [int(round(v)) for v in centers[i]],
             "share": round(float(counts[i]) / total, 3)}
            for i in order if counts[i] > 0]


def rgb_to_hsl(rgb) -> tuple[float, float, float]:
    """-> (hue 0-360, lightness 0-100, saturation 0-100), 标准 colorsys 公式。"""
    mx, mn = max(rgb) / 255.0, min(rgb) / 255.0
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, l * 100, 0.0
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    r, g, b = [v / 255.0 for v in rgb]
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60, l * 100, s * 100


def analyze_keyframe(palette: list[dict]) -> dict:
    """取主色里最暗/最亮的一对作为"暗端/亮端", 给出配色关系指标。"""
    major = [c for c in palette if c["share"] >= 0.05] or palette
    if not major:
        return {}
    hs = [(c, *rgb_to_hsl(c["rgb"])) for c in major]
    dark = min(hs, key=lambda x: x[2])
    light = max(hs, key=lambda x: x[2])
    dh = (light[1] - dark[1] + 180) % 360 - 180
    return {
        "dark": dark[0]["hex"], "light": light[0]["hex"],
        "d_lightness": round(light[2] - dark[2], 1),
        "d_hue": round(dh, 1),
        "light_range": [round(dark[2], 1), round(light[2], 1)],
        "sat_range": [round(min(x[3] for x in hs), 1), round(max(x[3] for x in hs), 1)],
    }


def coverage(img_path: Path, rgb: np.ndarray) -> float:
    """该色号在画面中的像素占比 (%)。

    比"是否存在于画面"强得多: 近白/近黑色号几乎总能找到匹配像素, 而覆盖面积
    才是真信号。2026-09-14 实测分离度: 真实色号 1.3-8.8%, 幻觉色号 0-0.39%。
    """
    px = np.asarray(Image.open(img_path).convert("RGB"), dtype=np.float32).reshape(-1, 3)
    if len(px) > 200000:
        px = px[:: len(px) // 200000]
    hit = np.linalg.norm(px - rgb, axis=1) <= LABEL_CONFIRM_DIST
    return round(100.0 * float(hit.mean()), 3)


def read_labels(src: Path, t: float, out_dir: Path) -> tuple[list[str], str, Path, str]:
    """视频按 t 抽原分辨率帧; 图片直接用原图 (都保原分辨率送模型)。

    返回 (色号列表, 模型原文, 所用帧路径, 配色主题名) —— 帧路径供核验时复用,
    避免拿缩略图做像素比对 (缩放+重编码会引入色差)。
    """
    if src.suffix.lower() in VIDEO_EXTS:
        out_dir.mkdir(parents=True, exist_ok=True)
        frame = out_dir / f"key_{t:.2f}.png"
        _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src),
              "-frames:v", "1", str(frame)])
    else:
        frame = src
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vision import analyze_image
    # vision 桥接无重试, 而 MIMO 实测有瞬时故障 (2026-09-14: 连续 3 次调用耗时
    # 8.4/5.3/78.6s, 且整页偶尔直接超时) -> 本地补退避重试, 否则一次抖动废掉整条笔记
    text = ""
    for attempt, wait in enumerate((0, 30, 90)):
        if wait:
            time.sleep(wait)
        try:
            text = analyze_image(str(frame), LABEL_PROMPT)
            break
        except Exception as e:  # noqa: BLE001 - 网络/超时/5xx 一律重试
            if attempt == 2:
                raise
            print(f"  色号读取失败 ({type(e).__name__}: {str(e)[:80]}), {30 if attempt == 0 else 90}s 后重试")
    m = NAME_RE.search(text)
    name = m.group(1).strip().strip("<>《》") if m else ""
    # 模型没给名称行时, 正则可能抓到色号行 -> 丢弃这类伪名称
    if "色号" in name or "#" in name or not 2 <= len(name) <= 20:
        name = ""
    return parse_color_lines(text), text, frame, name


def run(srcs: list[Path], name: str, fps: float, min_hold: float, k: int,
        want_labels: bool, verify_runs: int = 1) -> dict:
    out_dir = TMP_ROOT / f"{name}_palette"
    out_dir.mkdir(parents=True, exist_ok=True)
    video = next((p for p in srcs if p.suffix.lower() in VIDEO_EXTS), None)
    if video:
        mode = "video"
        meta = probe(video)
        frames = extract_frames(video, out_dir, fps)
        if not frames:
            raise SystemExit(f"抽帧失败: {video}")
        grids = np.stack([frame_grid(f["path"]) for f in frames])
        plateaus = find_plateaus(frames, grids, min_hold)
    else:
        # 图集笔记: 每张图就是一张色卡, 无定格检测可言
        mode = "gallery"
        meta = {"duration": None, "n_images": len(srcs), "has_audio": False}
        frames = gallery_frames(srcs, out_dir)
        plateaus = [{"t_start": None, "t_end": None, "t_key": float(i + 1),
                     "idx": i, "hold": None} for i in range(len(frames))]
    keyframes = []
    for n, p in enumerate(plateaus, 1):
        f = frames[p["idx"]]
        palette = extract_palette(f["path"], k)
        kf = {"n": n, "t": p["t_key"], "hold": p["hold"],
              "t_start": p["t_start"], "t_end": p["t_end"], "palette": palette}
        kf.update(analyze_keyframe(palette))
        if want_labels:
            # 复跑取一致性: 视觉模型输出不确定, 同一输入两次结果不同即不可信
            runsets, raw, frame_path, card_name, info = [], "", None, "", {}
            for _ in range(max(1, verify_runs)):
                pairs, raw, frame_path, card_name = read_labels(f["src"], p["t_key"], out_dir)
                adopted = set()
                for h, rgb in pairs:
                    val, src_kind, note = reconcile(h, rgb)
                    adopted.add(val)
                    info.setdefault(val, {"hex_raw": h, "rgb_raw": list(rgb) if rgb else None,
                                          "source": src_kind, "note": note})
                runsets.append(adopted)
            kf["labels_raw"] = raw[:RAW_CAP]
            kf["card_name"] = card_name
            kf["label_runs"] = len(runsets)
            names = sorted({lab for rs in runsets for lab in rs})
            covs = {lab: coverage(frame_path, _from_hex(lab)) for lab in names}
            med = float(np.median(list(covs.values()))) if covs else 0.0
            if len(names) > MAX_LABELS:
                kf["labels_warning"] = (
                    f"读到 {len(names)} 个色号 (中位覆盖 {med}%), 超过 {MAX_LABELS} 上限, "
                    f"判为模型幻觉已丢弃 (该画面可能本就没有调色板)")
            else:
                # 渲染漂移卡: 整张卡色块与标注色号系统性不符 -> 覆盖率对每个色号都低,
                # 这是卡片特性不是识别错误, 只记一次不逐条报 (否则复核清单一堆噪声)
                flat = med >= SWATCH_MATCH_MIN
                kf["card_type"] = "平色卡" if flat else "渲染漂移卡"
                if not flat:
                    # 覆盖率对两种"低覆盖"情形不可分 (实测: 渲染漂移卡 0.014% vs
                    # 无调色板的幻觉图 0.000%), 且模型每次幻觉的标准色表都不同
                    # (CSS 命名色 / Office 主题色), 枚举不可行 -> 交人工判定
                    kf["card_review"] = ("色块与标注色号不符, 无法自动核验 — 可能确实是"
                                         "渲染漂移卡, 也可能该图本就没有调色板; 请人工确认")
                kf["labels"] = []
                for lab in names:
                    hits = sum(1 for rs in runsets if lab in rs)
                    reasons = []
                    if hits < len(runsets):
                        reasons.append(f"复跑仅 {hits}/{len(runsets)} 次命中")
                    if flat and covs[lab] < REVIEW_COVERAGE:
                        reasons.append(f"画面占比仅 {covs[lab]}%")
                    kf["labels"].append({
                        "label": lab, "coverage": covs[lab],
                        "runs_seen": f"{hits}/{len(runsets)}",
                        **info.get(lab, {}),
                        "status": "确认" if not reasons else "存疑",
                        "review": "; ".join(reasons)})
        keyframes.append(kf)
    source = str(video) if video else f"{srcs[0].parent}\\* ({len(srcs)} 张)"
    return {"source": source, "name": name, "mode": mode, "meta": meta,
            "fps": fps, "n_frames": len(frames), "keyframes": keyframes}


def summarize(rep: dict) -> dict:
    kfs = rep["keyframes"]
    dl = [k["d_lightness"] for k in kfs if "d_lightness" in k]
    return {
        "n_keyframes": len(kfs),
        "dark_to_light": sum(1 for v in dl if v >= 15),
        "light_to_dark": sum(1 for v in dl if v <= -15),
        "d_lightness_min": min(dl) if dl else None,
        "d_lightness_max": max(dl) if dl else None,
        "duration": rep["meta"].get("duration"),
    }


def timeline_colors(k: dict) -> tuple[str, str, str]:
    """端点色 + 来源。色号是设计者声明的真值, 采样值是画面实际构成:
    渐变卡的 k-means 中心会向斜坡中部收 (实测 #569390 vs 真值 #367D8C),
    所以有标签时以标签为准, 采样值仅作结构参考。"""
    labs = [lab["label"] for lab in k.get("labels", []) if lab["status"] != "不符"]
    if len(labs) >= 2:
        labs.sort(key=lambda h: rgb_to_hsl(_from_hex(h))[1])
        return labs[0], labs[-1], "画面色号"
    return k.get("dark", ""), k.get("light", ""), "采样"


def to_markdown(rep: dict) -> str:
    s = summarize(rep)
    kfs = rep["keyframes"]
    gallery = rep.get("mode") == "gallery"
    scope = (f"图集 {rep['n_frames']} 张" if gallery
             else f"时长 {(s['duration'] or 0):.1f}s | 分析帧 {rep['n_frames']} @ {rep['fps']}fps")
    lines = [f"# 配色解读 — {rep['name']}", "",
             f"- 源: `{rep['source']}`",
             f"- {scope} | 色卡 {s['n_keyframes']} 张",
             f"- 明度方向: 暗→亮 {s['dark_to_light']} 组, 亮→暗 {s['light_to_dark']} 组",
             f"- 明度跨度: {s['d_lightness_min']} ~ {s['d_lightness_max']}", "",
             "## 色彩时间轴" if not gallery else "## 色卡清单", "",
             "| # | " + ("图" if gallery else "时间 | 定格") + " | 最暗 | 最亮 | 明度Δ | 色相差 | 端点来源 |",
             "|---|" + ("----|" if gallery else "------|------|") + "------|------|-------|--------|---------|"]
    for k in kfs:
        if "dark" not in k:
            continue
        a, b, src = timeline_colors(k)
        dl = round(rgb_to_hsl(_from_hex(b))[1] - rgb_to_hsl(_from_hex(a))[1], 1) \
            if src == "画面色号" else k["d_lightness"]
        dh = (rgb_to_hsl(_from_hex(b))[0] - rgb_to_hsl(_from_hex(a))[0] + 180) % 360 - 180 \
            if src == "画面色号" else k["d_hue"]
        when = f"{k['n']}" if gallery else f"{k['t']:.2f}s | {k['hold']}s"
        lines.append(f"| {k['n']} | {when} | `{a}` | `{b}` "
                     f"| {dl:+.1f} | {dh:+.0f}° | {src} |")
    lines += ["", "## 各色卡调色板", ""]
    for k in kfs:
        head = (f"### {k['n']}. 图 {k['n']}" if gallery
                else f"### {k['n']}. t={k['t']:.2f}s (定格 {k['hold']}s)")
        if k.get("card_name"):
            head += f" — {k['card_name']}"
        lines.append(head)
        lines.append("")
        if k.get("labels_warning"):
            lines += [f"> ⚠️ {k['labels_warning']}", ""]
        if k.get("card_type") == "渲染漂移卡":
            lines += [f"> ⚠️ 待人工核验: {k.get('card_review', '')}", "",
                      "> 本卡画面色块与标注色号系统性不符, 覆盖率校验不适用; "
                      "若确认是真实色卡, **色号以标注为准**。", ""]
        if k.get("labels"):
            # 画面色号是设计者声明的调色板 -> 主表; 采样值只反映画面构成
            ok = [lab for lab in k["labels"] if lab["status"] != "不符"]
            lines.append(f"调色板 (画面色号, {len(ok)} 色):")
            lines.append("")
            lines.append("| 色号 | 画面占比 | 复跑 | 读数来源 | 核验 |")
            lines.append("|------|---------|------|---------|------|")
            for lab in k["labels"]:
                mark = "" if lab["status"] == "确认" else f" — {lab['review']}"
                src = lab.get("source", "")
                if lab.get("hex_raw") and lab["hex_raw"].upper() != lab["label"]:
                    src += f" (模型原文 {lab['hex_raw']})"
                lines.append(f"| `{lab['label']}` | {lab['coverage']}% | {lab['runs_seen']} "
                             f"| {src} | {lab['status']}{mark} |")
            lines.append("")
        lines.append("画面构成 (采样):")
        lines.append("")
        lines.append("| 色号 | 占比 |")
        lines.append("|------|------|")
        for c in k["palette"]:
            lines.append(f"| `{c['hex']}` | {c['share'] * 100:.0f}% |")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="视觉型视频(色卡/配色)解读")
    ap.add_argument("src", nargs="+", help="视频 / 单图 / 图集(多图, 每图一张色卡)")
    ap.add_argument("--name", default=None, help="产物目录名, 默认取首文件名")
    ap.add_argument("--fps", type=float, default=4.0, help="抽帧频率, 默认 4")
    ap.add_argument("--min-hold", type=float, default=0.6, help="定格最小时长(秒), 默认 0.6")
    ap.add_argument("--colors", type=int, default=N_COLORS, help="调色板色数, 默认 6")
    ap.add_argument("--labels", action="store_true", help="读画面色号并核验 (消耗 vision API)")
    ap.add_argument("--verify-runs", type=int, default=1,
                    help="色号复跑次数, 不一致即标存疑 (默认 1; 需核对时用 2)")
    ap.add_argument("--out", default=None, help="markdown 输出路径")
    ap.add_argument("--json-out", default=None, help="JSON 输出路径")
    args = ap.parse_args()

    srcs = [Path(p) for p in args.src]
    missing = [str(p) for p in srcs if not p.exists()]
    if missing:
        raise SystemExit(f"文件不存在: {', '.join(missing)}")
    name = args.name or srcs[0].stem
    rep = run(srcs, name, args.fps, args.min_hold, args.colors, args.labels,
              args.verify_runs)
    s = summarize(rep)

    md_path = Path(args.out) if args.out else TMP_ROOT / f"{name}_palette.md"
    md_path.write_text(to_markdown(rep), encoding="utf-8")
    json_path = Path(args.json_out) if args.json_out else TMP_ROOT / f"{name}_palette.json"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"关键帧 {s['n_keyframes']} 个 | 暗→亮 {s['dark_to_light']} 组 | "
          f"明度跨度 {s['d_lightness_min']} ~ {s['d_lightness_max']}")
    print(f"markdown -> {md_path}")
    print(f"json     -> {json_path}")


if __name__ == "__main__":
    main()
