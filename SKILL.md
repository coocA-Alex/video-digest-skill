---
name: video-digest
version: "0.1.0"
description: 视频与图文内容解析与笔记。兼容 B站视频、小红书推文/视频、本地视频文件：字幕直取或语音转写、画面核验、图片文字提取、结构化总结、笔记归档。Use when: 需要解析视频/图文内容、提取字幕或图片文字、核验画面、做结构化笔记。触发词：解析这个视频、总结这个视频、解析本地视频、视频摘要、字幕提取、画面核验、提炼视频要点、看视频讲了什么、做视频笔记、解析小红书、看小红书推文。
author: coocA-Alex
tags: [video, digest, bilibili, xiaohongshu, notes, subtitle, vision, asr]
license: MIT
metadata:
  version: 0.1.0
---

# 视频解析与情报追踪

## Purpose

把视频/图文内容转成结构化、可核验的笔记：字幕直取或语音转写、画面核验、图片文字提取，事实与观点分离，供个人学习与研究复用。

## Prerequisites

- Python 3.11+ 与 `requests`；`ffmpeg`（画面核验/音频提取场景）
- MiMo API key（多模态 ASR/视觉，`MIMO_API_KEY` 环境变量或项目 .env）
- LLM 总结 key（`ANTHROPIC_AUTH_TOKEN` 或项目 .env，DeepSeek flash 建议 max_tokens=50000）
- B站字幕直取需 SESSDATA（`~/.bili_sessdata`）；小红书解析需 web_session（`~/.xhs_web_session`）

## 环境依赖

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `MIMO_API_KEY` | MiMo API 密钥（多模态，与官方 MiMo-Skills 一致） | 是 |
| `SESSDATA` | B站登录 cookie（AI 字幕直取，存 `~/.bili_sessdata`） | 部分视频需要 |
| web_session | 小红书登录 cookie（推文/视频解析，存 `~/.xhs_web_session`） | 小红书需要 |
| DS key（`ANTHROPIC_AUTH_TOKEN` 或项目 .env） | LLM 总结 | 是 |

| 依赖 | 说明 | 必需 |
|------|------|------|
| `ffmpeg` | 视频抽帧/音频提取 | 画面核验场景 |
| `requests` | API 调用 | 是 |

## 能力

| 能力 | 入口 | 说明 |
|------|------|------|
| 单视频字幕直取 | `scripts/bili_subtitle.py <bvid> <cid>` | B站 AI 字幕（需 .env SESSDATA） |
| 语音转写 | `scripts/mimo_asr.py`（经 analysis 模块） | wav/mp3 → 文本（默认 MIMO，可换模型） |
| 画面核验 | `scripts/mimo_vision.py` | 图片/帧 → 视觉理解（默认 MIMO，可换模型） |
| 结构化总结 | `scripts/bili_summarize.py <subtitle> <owner> [out.md] [style] [desc]` | 5 领域模板（stock/finance/tech/general/lecture）; general 模板 = 逻辑链+精选事实/观点+思考与行动层; 可选传视频简介校正字幕音译 |
| 本地视频解析 | `scripts/local_video_pipeline.py` | 本地视频文件 → 转写 → 笔记（支持任意来源录制） |
| 批量追踪 | `scripts/digest_weekly.py` | B站关注列表增量 → 归档（creators 用 `config/creators.example.json` 模板） |
| 视频抽帧 | `scripts/video_frames.py` | B站/本地视频流式抽帧 → 时间戳 manifest（ffmpeg） |
| 帧画面核验管道 | `scripts/video_vision.py` | manifest 帧批量 → MIMO 读帧 → 时间戳视觉摘要（并发 4 workers） |
| 小红书推文/视频解析 | `scripts/xhs_note.py <url> [--extract]` | 小红书链接 → 类型判断（视频/图文）→ 下载 → 可选 ASR/帧/图片文字提取（web_session 存 `~/.xhs_web_session`） |

## 工作流（解析一个视频）

1. **字幕**：优先 B站 AI 字幕直取；无字幕/非 B站 → 音频转写（MIMO ASR 或配置的其他模型）
2. **画面核验（可选）**：需要看图表/PPT/实验画面时，抽帧 → 视觉模型理解
3. **小红书推文（可选）**：`scripts/xhs_note.py <url> --extract` — 自动判断视频/图文；视频走 ASR+帧，图文走图片文字提取（正文常在图片里）
4. **总结**：LLM 结构化总结（可传视频简介 desc 校正字幕音译；general 模板 = 逻辑链+精选+思考与行动层）
5. **存疑点主动画面核验**：总结后若"数据可信度备注"有可被画面解决的存疑点（专有名词/工具名/表格数字）→ 按时间戳抽帧核验，回填笔记
6. **归档**：输出 markdown 到 data/ 或指定位置

## 配置（凭证隔离 — 重要）

- **API key 一律从环境变量读取**（agent 无关，Claude Code / Codex / 其他 agent 均可）：
  - `MIMO_API_KEY`（多模态）、`DEEPSEEK_API_KEY`（总结，兼容 `ANTHROPIC_AUTH_TOKEN`）
- **登录凭证一律存仓库外文件**：B站 SESSDATA（`~/.bili_sessdata`）、小红书 web_session（`~/.xhs_web_session`）；scripts 只读这些路径，不打印不落盘
- **本 skill 及 scripts 中不包含任何真实 key/凭证**
- **换模型**：编辑 `config/multimodal.json`（asr/vision/summarize 段的 provider/model/base_url/api_key_env），
  例如总结换 OpenAI 兼容模型 = 改 base_url + model + api_key_env；协议不同的模型需新增适配器脚本
- 示例配置见 `config/multimodal.json`（不含 key）
- **Agent 兼容**：SKILL.md 为标准格式（Claude Code / Codex / Cursor / OpenClaw 通用）；scripts 为纯 Python CLI 不依赖 agent；key 解析顺序 = 环境变量 → 项目本地配置 → Claude Code 全局配置（向后兼容）

## 合规

- 个人学习/研究使用；遵守平台协议；不批量抓取、不商用他人内容
- 小红书仅按需解析单条推文（用户指定链接），不逆向签名、不采集列表/评论
- 输出仅供个人参考，不得二次分发

## Limitations

- B站 AI 字幕多数需登录态（cookies）；接口可能变动
- MIMO ASR 限 wav/mp3、base64 ≤10MB（长音频需分段）
- 多 P 视频默认总结第一 P
- 小红书仅按需解析单条推文（用户指定链接）；未登录无法取视频流/互动数据

## Troubleshooting

- **总结输出为空/过短**：思考型模型（deepseek-v4-flash）长输入时 max_tokens 不足会静默截断 → 设 max_tokens=50000 或分块
- **B站 412 / 空响应**：平台风控 → 停止重试 30 分钟以上，或交给定时任务兜底；单博主失败已隔离不影响其他。**风控期间备选**：从视频页面 HTML（`curl --compressed`）提取 cid + 标题/简介，字幕走 player API（通常未风控），视频流走 playurl API（fnval=4048 取 dash）
- **小红书无 __INITIAL_STATE__**：登录态失效或链接过期 → 重新提供 web_session / 链接
- **MIMO 500**：服务端暂时故障 → 等待 90s 重试，或 `--force` 重跑

## Available Scripts

| Script | Purpose | Arguments |
|--------|---------|-----------|
| `bili_subtitle.py` | B站 AI 字幕直取 | `<bvid> <cid>` |
| `bili_summarize.py` | 字幕 → 结构化总结 | `<subtitle.txt> <owner> [out.md] [style]` |
| `xhs_note.py` | 小红书推文/视频解析（自动类型判断） | `<explore_url> [--name noteX] [--extract]` |
| `local_video_pipeline.py` | 本地视频 → 转写 → 笔记 | `<video> [--no-vision] [--owner] [--template]` |
| `digest_weekly.py` | B站关注列表批量追踪 | `[--backfill N] [--cutoff DATE]` |
| `mimo_asr.py` | 音频转写 | `<audio> [lang]` |
| `mimo_vision.py` | 图片/帧视觉理解 | `<image...> [--prompt]` |
| `video_frames.py` | 流式抽帧 + 时间戳 manifest | `<bvid/url> [--count N]` |
| `video_vision.py` | 帧批量读帧 → 视觉摘要 | `<bvid> [--force]` |

## Examples

```bash
# B站单视频字幕 + 总结
python scripts/bili_subtitle.py BV1xxx <cid>
python scripts/bili_summarize.py tmp/BV1xxx.txt 博主名 notes/out.md stock

# 小红书推文/视频（自动判断类型 + 提取）
python scripts/xhs_note.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..." --extract

# 本地视频（转写 + 画面 + 笔记）
python scripts/local_video_pipeline.py lecture.mp4 --owner 讲座
```

Agent 调用方式（run_script）:

```text
run_script("scripts/bili_subtitle.py", ["BV1xxx", "cid"])            # 拉字幕
run_script("scripts/xhs_note.py", [url, "--extract"])                # 小红书解析
run_script("scripts/bili_summarize.py", [sub, owner, out, "stock"])  # 总结
```
