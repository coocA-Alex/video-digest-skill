"""笔记头部块的唯一真相源: "# <标题>" + 元数据行 + 链接 + 分隔线。

主管线 `digest_daily.archive_note` 一直自带头部; 但**谷时队列**与**本地讲座管线**
原先直写 `bili_summarize` 的原始摘要 ⇒ 落盘无头部块 (2026-09-24 外部消费方通报 123 篇)。
本模块把那块头部抽出来共用, 三条写入路径不再各写一份。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_HAS_H1 = re.compile(r"^#\s+\S")


def build(
    owner: str,
    bvid: str,
    title: str,
    pubdate_ts: int,
    duration_sec: int | None = None,
    url: str = "",
) -> str:
    """拼出头部块。duration_sec=None ⇒ 省略"时长"字段 (讲座/文章类没有时长)。

    bvid 为空但有 url 时用 url 作链接行 —— 非视频来源 (如每日文字早报) 走这条;
    两者都为空则整行省略 (与旧行为一致)。
    """
    date_str = datetime.fromtimestamp(int(pubdate_ts)).strftime("%Y-%m-%d")
    meta = f"- UP主: {owner} | 日期: {date_str}"
    if duration_sec is not None:
        meta += f" | 时长: {int(duration_sec) // 60} 分钟"
    if bvid:
        link = f"\n- 链接: https://www.bilibili.com/video/{bvid}"
    elif url:
        link = f"\n- 链接: {url}"
    else:
        link = ""
    return f"# {title}\n\n{meta}{link}\n\n---\n\n"


def first_h1(path: Path) -> str | None:
    """读前 5 行的首个 H1 文本。

    重跑覆盖原文件前必须先摘下来 —— 覆盖后标题就无处可取了 (2026-09-01 那批
    38 篇重跑正是这么丢掉标题的)。
    """
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                m = re.match(r"^#\s+(\S.*)$", line.rstrip("\n"))
                if m:
                    return m.group(1).strip()
    except OSError:
        return None
    return None


def ensure(
    path: Path,
    *,
    owner: str,
    title: str | None,
    bvid: str = "",
    pubdate_ts: int | None = None,
    duration_sec: int | None = None,
) -> bool:
    """无头部则补上; 已有 H1 则**不动**。返回是否实际补写。

    title 缺失或 pubdate 未知时不猜、不写, 返回 False —— 宁可留缺也不写错数据。
    """
    if not path.exists() or not title or pubdate_ts is None:
        return False
    text = path.read_text(encoding="utf-8")
    if _HAS_H1.match(text.lstrip()):
        return False
    head = build(owner, bvid, title, pubdate_ts, duration_sec)
    path.write_text(head + text, encoding="utf-8")
    return True
