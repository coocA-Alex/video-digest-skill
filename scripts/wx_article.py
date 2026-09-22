"""Fetch a WeChat public-account article: extract text, download images,
and generate per-image notes via the vision provider (MIMO).

Pipelines: URL -> tmp/wx_{id}/ (article.txt + imgs/ + img_notes.md),
then summarize with bili_summarize.py using the "wx" template.

Idempotent: existing images and img_notes.md are reused; --force re-runs.
Image notes are cached so repeated runs cost no extra vision tokens.
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vision import analyze_image  # noqa: E402  (provider 由 multimodal.json 路由)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://mp.weixin.qq.com/",
}
TIMEOUT = 25
VISION_WORKERS = 4

# 公众号配图: 图表/公式/截图是关键知识载体, 图注应还原其承载的信息
WX_IMAGE_PROMPT = (
    "这是微信公众号文章的配图。请提取图中承载的关键信息:1)图中文字原文"
    "(标题/数值/标签/图例);2)图表/曲线/表格/流程图的要点结论;3)公式则还原其含义。"
    "中文输出 3-5 行,只写图中真实存在的内容,不要推测。"
)


class WxError(Exception):
    """Base error for article fetching."""


def extract_body(html: str) -> str:
    """Return the js_content div HTML."""
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', html, re.S)
    if not m:
        m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>', html, re.S)
    if not m:
        raise WxError("no js_content found (page may need verification)")
    return m.group(1)


def recover_from_desc(page: str) -> tuple[str, str]:
    """兜底通道: 从 window.desc 恢复正文, 返回 (公众号名, 正文)。

    背景 (2026-09-22 实测): 微信对**部分文章**只吐「分享/SEO 预渲染」变体 ——
    页面里 `rich_media_content` / `id="js_content"` / `rich_media_title` 计数**全为 0**,
    2.6 MB 里 98% 是 JS, 但 `og:title` / `og:url` / `window.name` / **`window.desc` 仍在**,
    且 `window.desc` **含完整正文**。

    实测: 某科普号一篇（页面 2.6 MB）重试 3 次（含换 UA / 加 Referer / 延时）**均无正文 DOM**,
    而 desc 恢复出 654 字完整正文（含 5 个分节 + 结尾句）。

    ⚠️ **该变体没有正文 DOM ⇒ 也没有配图**, 故调用方须按「无图」处理。
    ⚠️ 这不是「删除 / 限流 / 验证」—— 页面里无任何错误标记, 属正常变体。
    """
    i = page.find("window.desc = ")
    if i < 0:
        return "", ""
    j = i + len("window.desc = ") + 1          # 跳过开引号
    end = j
    while end < len(page) and page[end] != '"':
        end += 2 if page[end] == "\\" else 1
    body = page[j:end]

    name = ""
    k = page.find("window.name = ")
    if k >= 0:
        a = page.find('"', k) + 1
        b = page.find('"', a)
        name = page[a:b]

    body = body.replace("\\x0a", "\n").replace("\\x26", "&").replace("\\x27", "'")
    body = unescape(body)
    body = re.sub(r"<a\s+class=.*", "", body, flags=re.S)   # 尾部话题链接标记
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return name, body


def extract_images(body_html: str) -> list[str]:
    """All <img> src URLs inside the article body, de-duplicated in order."""
    urls = []
    for src in re.findall(r'<img[^>]*src="([^"]+)"', body_html):
        if src not in urls:
            urls.append(src)
    return urls


def paragraphs_with_markers(body_html: str) -> str:
    """Convert body HTML to text, replacing <img> with [图N] markers in order."""
    img_seq = 0
    parts: list[str] = []
    for piece in re.split(r'(<img[^>]*>)', body_html):
        if piece.startswith("<img"):
            img_seq += 1
            parts.append(f"[图{img_seq}]")
            continue
        text = re.sub(r'<[^>]+>', "\n", piece)
        text = unescape(text)
        text = re.sub(r'[ \t　]+', " ", text)
        text = re.sub(r'\n{2,}', "\n", text)
        parts.append(text.strip())
    return re.sub(r'\n{2,}', "\n\n", "\n".join(parts)).strip()


def article_id_from_url(url: str) -> str:
    """从 URL 推稳定目录名。

    - 短链 `/s/<shortid>` → 直接取尾段（如 `GeYY9V1oqEcke7EIRwZwiQ`）
    - **规范链**（`/s?__biz=..&mid=..&idx=..&sn=..`，合集枚举返回的就是这种）→ 取 `m<mid>_<idx>`
      ；若直接取尾段会得到 `s?__biz=...` —— **含 `?`/`&`/`=`，是非法 Windows 文件名**（2026-09-22 发现）
    """
    m = re.search(r"[?&]mid=(\d+)", url)
    if m:
        idx = re.search(r"[?&]idx=(\d+)", url)
        return f"m{m.group(1)}_{idx.group(1) if idx else '1'}"
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^\w\-]", "_", tail)[:48] or "unknown"


def find_existing_by_title(title: str) -> Path | None:
    """跨 URL 形态去重 —— 同一篇文章的短链与规范链目录名不同, 目录名判定会漏。

    背景 (2026-09-22 实测): 合集枚举返回**规范链** (`/s?__biz=..&mid=..&idx=..&sn=..`),
    而手工解析常用**短链** (`/s/<id>`)。两者指向同一篇, 但 `article_id_from_url` 得到的
    目录名不同 ⇒ `--skip-existing` 失效 ⇒ 重复下载图片 + 重复跑视觉（真实成本）。
    改按 `article.txt` 首行标题匹配, 命中即复用。
    """
    for p in TMP_DIR.glob("wx_*/article.txt"):
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:160]
        except OSError:
            continue
        if head.startswith(f"标题: {title}\n"):
            return p.parent
    return None


def fetch_article(url: str) -> tuple[str, str, str, list[str], bool]:
    """Return (title, author, text_with_markers, image_urls, via_desc).

    via_desc=True ⇒ 正文走 window.desc 兜底通道; 该变体无正文 DOM, **必然无配图**。
    """
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    html = resp.text
    m = re.search(r'property="og:title" content="([^"]+)"', html)
    title = unescape(m.group(1)).strip() if m else "NO_TITLE"
    m2 = re.search(r'id="js_name"[^>]*>([^<]+)<', html)
    author = m2.group(1).strip() if m2 else ""

    try:
        body = extract_body(html)
    except WxError:
        name, text = recover_from_desc(html)
        if not text:
            raise WxError(
                "no js_content found, and window.desc fallback also empty "
                "(页面可能被风控或链接无效)"
            )
        return title, (author or name or "NO_AUTHOR"), text, [], True

    return title, (author or "NO_AUTHOR"), paragraphs_with_markers(body), extract_images(body), False


def download_images(workdir: Path, urls: list[str], force: bool = False) -> list[Path]:
    """Download each image to workdir/imgs/N.ext (skip existing)."""
    imgs_dir = workdir / "imgs"
    imgs_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, url in enumerate(urls, 1):
        ext = re.search(r'format=(\w+)', unquote(url))
        ext = (ext.group(1) if ext else "jpg").lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        dest = imgs_dir / f"{i:02d}.{ext}"
        if dest.exists() and not force:
            paths.append(dest)
            continue
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        paths.append(dest)
    return paths


def generate_image_notes(workdir: Path, paths: list[Path], force: bool = False) -> str:
    """MIMO 读图, 图注写入 workdir/img_notes.md; 已存在则直接复用."""
    out_path = workdir / "img_notes.md"
    if out_path.exists() and not force:
        return out_path.read_text(encoding="utf-8")
    if not paths:
        out_path.write_text("(无配图)", encoding="utf-8")
        return "(无配图)"

    def one(p: Path) -> str:
        try:
            text = analyze_image(str(p), WX_IMAGE_PROMPT, max_tokens=800).strip()
            return f"图{p.stem}: {text}" if text else f"图{p.stem}: (读图无输出)"
        except Exception as exc:
            return f"图{p.stem}: (读图失败: {exc})"

    with ThreadPoolExecutor(max_workers=VISION_WORKERS) as pool:
        notes = list(pool.map(one, paths))
    text = "\n\n".join(notes)
    out_path.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description="WeChat article -> text + images + image notes")
    ap.add_argument("url", help="mp.weixin.qq.com article URL")
    ap.add_argument("--force", action="store_true", help="re-download / re-read images")
    ap.add_argument("--no-vision", action="store_true", help="skip MIMO image notes")
    ap.add_argument("--skip-existing", action="store_true",
                    help="tmp/wx_<id>/article.txt 已存在则跳过 (供 wx_album --fetch 批量调用)")
    args = ap.parse_args()


    try:
        title, author, text, urls, via_desc = fetch_article(args.url)
    except (WxError, requests.RequestException) as exc:
        print(f"error: {exc}")
        sys.exit(1)

    # 幂等: ① 同 URL 形态 → 同目录名  ② 跨 URL 形态 → 同标题 (见 find_existing_by_title)
    if args.skip_existing:
        if (TMP_DIR / f"wx_{article_id_from_url(args.url)}" / "article.txt").exists():
            print(f"skip: 同 URL 已解析过 -> {args.url}")
            return
        dup = find_existing_by_title(title)
        if dup:
            print(f"skip: 同标题已解析过 ({dup.name}) -> {title}")
            return

    # tmp/wx_{id}: 稳定且可读 (见 article_id_from_url; 规范链会含非法字符, 必须走它)
    art_id = article_id_from_url(args.url)
    workdir = TMP_DIR / f"wx_{art_id}"
    workdir.mkdir(parents=True, exist_ok=True)

    (workdir / "article.txt").write_text(
        f"标题: {title}\n公众号: {author}\n\n{text}", encoding="utf-8"
    )
    print(f"title: {title} | author: {author} | chars: {len(text)} | images: {len(urls)}")
    if via_desc:
        print("warn: 正文经 window.desc 兜底通道恢复 (该页无正文 DOM ⇒ 无配图)")

    paths: list[Path] = []
    if urls:
        try:
            paths = download_images(workdir, urls, args.force)
        except requests.RequestException as exc:
            print(f"warn: image download failed ({exc}); continuing without images")
    if paths and not args.no_vision:
        notes = generate_image_notes(workdir, paths, args.force)
        print(f"image notes ({len(paths)}): {len(notes)} chars -> {workdir / 'img_notes.md'}")
        # 图注并入 article.txt: 总结时正文+图注一次进 prompt
        art_path = workdir / "article.txt"
        art_path.write_text(
            art_path.read_text(encoding="utf-8") + f"\n\n配图图注:\n{notes}",
            encoding="utf-8",
        )
    print(f"-> {workdir / 'article.txt'}")


if __name__ == "__main__":
    main()
