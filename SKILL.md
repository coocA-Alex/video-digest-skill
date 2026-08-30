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
| 结构化总结 | `scripts/bili_summarize.py <subtitle> <owner> [out.md] [style] [desc]` | **内容类型模板 6 类 (MECE: 互斥穷尽)** — stock(股市收评)/ news(资讯多主题, 含财经要闻)/ teaching(教学方法论)/ tech(评测/单主题解析)/ lecture(讲座含问答)/ general(兜底); **自动分流**: 显式配置(creators)优先 → LLM 分类 detect_template → general 兜底; news 类**按叙事链分节**（事件→起因→影响→观点）, 杜绝口播碎片罗列; 另可显式指定输出格式 style (keypoints/timeline/notes/opinions, 与内容类型正交); 可选传视频简介校正字幕音译; **超长字幕（>40k 字符）自动语义分块**（[30k,35k] 区间内找 [mm:ss] 时间戳行切点 → 块总结 hash 缓存 → 二次合并，避免硬切断语义） |
| 多P/长视频兼容 | `scripts/bili_media.py` | `enum_pages(bvid)` 多P 枚举（112P 实测）/ `check_coverage(字幕, 时长)` 字幕覆盖比 / `needs_asr_fallback` 长视频（>20min）覆盖 <70% 判定 / `fetch_audio(bvid, cid)` dash 音频下载（Cookie+URL 刷新重试，ASR 兜底用） |
| 本地视频解析 | `scripts/local_video_pipeline.py` | 本地视频文件 → 转写 → 笔记（支持任意来源录制） |
| 批量追踪 | `scripts/digest_weekly.py` | B站关注列表增量 → 归档（creators 用 `config/creators.example.json` 模板） |
| 视频抽帧 | `scripts/video_frames.py` | B站/本地视频流式抽帧 → 时间戳 manifest（ffmpeg） |
| 帧画面核验管道 | `scripts/video_vision.py` | manifest 帧批量 → MIMO 读帧 → 时间戳视觉摘要（并发 4 workers） |
| 小红书推文/视频解析 | `scripts/xhs_note.py <url> [--extract]` | 小红书链接 → 类型判断（视频/图文）→ 下载 → 可选 ASR/帧/图片文字提取（web_session 存 `~/.xhs_web_session`） |

## 工作流（解析一个视频）

1. **字幕**：优先 B站 AI 字幕直取；无字幕/非 B站 → 音频转写（MIMO ASR 或配置的其他模型）；**长视频（>20min）字幕覆盖 <70%**（B站 AI 字幕常只覆盖口播部分）→ 自动 ASR 兜底（`bili_media.fetch_audio` dash 下载 + 分段转写）
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
- **视觉模型可选清单**（画面核验，默认 MIMO；换模型见下）：
  - `mimo-v2.5`（默认，Anthropic 兼容 messages API，`MIMO_API_KEY`）
  - `glm-5.3-flash`（智谱原生多模态，OpenAI 兼容 chat/completions，`https://open.bigmodel.cn/api/paas/v4/chat/completions`，key 用 `ZHIPU_API_KEY`；图片/视频输入、1M 上下文；thinking 始终开启需留 max_tokens 余量）
  - GPT 视觉（待 Codex 侧测试后补充；OpenAI 兼容格式与 GLM 相同）
  - **替换步骤**：① multimodal.json 的 vision 段改 base_url/model/api_key_env → ② 若协议与 Anthropic messages 不同（如 GLM/GPT 的 OpenAI 格式），修改 `mimo_vision.py` 请求构造（content 数组 `image`+`source/base64` → `image_url`+`url` 的 data URL；header `api-key` → `Authorization: Bearer`；响应取 `choices[0].message.content`）→ ③ .env 配对应 key。协议相同的模型仅改配置即可
  - **不另建常驻桥接脚本**；可选模型清单与接入要点见 `docs/vision-model-options.md`
- 示例配置见 `config/multimodal.json`（不含 key）
- **Agent 兼容**：**已验证集成：Claude Code、Codex Desktop**（2026-08-29：skill 发现、包结构、脚本语法在 Codex Desktop 验证通过；端到端 provider 执行待验证）。核心能力为 Python CLI，可供能够读取 Markdown 技能说明并执行本地命令的其他 agent（Cursor/OpenClaw 等）适配；其他 agent 集成尚未实际验证。
- **key 解析顺序** = 环境变量 → 项目本地配置 → Claude Code 全局配置（最后一项为 **Claude Code legacy fallback**，不作为 Codex 或其他 agent 的前置条件）

## 合规

- 个人学习/研究使用；遵守平台协议；不批量抓取、不商用他人内容
- 小红书仅按需解析单条推文（用户指定链接），不逆向签名、不采集列表/评论
- 输出仅供个人参考，不得二次分发

## Limitations

- B站 AI 字幕多数需登录态（cookies）；接口可能变动
- MIMO ASR 限 wav/mp3、base64 ≤10MB（长音频需分段）
- 多 P 视频：默认主 P（时长最长）总结 + 其余 P 有字幕则拉取并补"P 对照说明"节（digest 批量流程）；手动流程可用 `bili_media.enum_pages` 定向处理任意 P
- 小红书仅按需解析单条推文（用户指定链接）；未登录无法取视频流/互动数据

## Troubleshooting

- **总结输出为空/过短**：思考型模型（deepseek-v4-flash）长输入时 max_tokens 不足会静默截断 → 设 max_tokens=50000 或分块；**字幕 >40k 字符自动语义分块**（`bili_summarize` 内置，块级缓存 tmp/long_summary_cache/ 支持断点续跑）
- **长视频字幕只覆盖开头**：B站 AI 字幕对长视频可能只生成口播部分（如 96min 仅 9min）→ `needs_asr_fallback` 自动判定，走 ASR 兜底；兜底失败降级纯字幕并记录 no_subtitle 重试
- **ASR 音频下载失败（dash 403/截断）**：dash 流需 UA+Referer+Cookie 完整头（`bili_media.fetch_audio` 已内置）；URL 带 deadline 中途失效 → 重试时刷新 URL；分段缓存陈旧时先清 `tmp/{bvid}_asr/`
- **B站 412 / 空响应**：平台风控 → 停止重试 30 分钟以上，或交给定时任务兜底；单博主失败已隔离不影响其他。**风控期间备选**：从视频页面 HTML（`curl --compressed`）提取 cid + 标题/简介，字幕走 player API（通常未风控），视频流走 playurl API（fnval=4048 取 dash）
- **小红书无 __INITIAL_STATE__**：登录态失效或链接过期 → 重新提供 web_session / 链接
- **MIMO 500**：服务端暂时故障 → 等待 90s 重试，或 `--force` 重跑

## Available Scripts

| Script | Purpose | Arguments |
|--------|---------|-----------|
| `bili_subtitle.py` | B站 AI 字幕直取 | `<bvid> <cid>` |
| `bili_media.py` | 多P 枚举 / 字幕覆盖检测 / dash 音频下载 | 库函数: `enum_pages` `check_coverage` `needs_asr_fallback` `fetch_audio` |
| `bili_summarize.py` | 字幕 → 结构化总结（超长自动分块） | `<subtitle.txt> <owner> [out.md] [style]` |
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

# 多P 视频定向处理任意 P（如 112P 课程的第 5 讲）
python -c "import sys; sys.path.insert(0,'scripts'); from bili_media import enum_pages; print([(p['cid'],p['part']) for p in enum_pages('BV1xxx')])"
```

Agent 调用方式:

```bash
# 通用 CLI 示例（Claude Code / Codex / 任何可执行本地命令的 agent 均适用）
python scripts/bili_subtitle.py BV1xxx <cid>
python scripts/xhs_note.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..." --extract
python scripts/bili_summarize.py tmp/BV1xxx.txt 博主名 notes/out.md news
```

```text
# Claude Code 示例（run_script 为本工具专属能力, 其他 agent 请用上面的终端命令）
run_script("scripts/bili_subtitle.py", ["BV1xxx", "cid"])            # 拉字幕
run_script("scripts/xhs_note.py", [url, "--extract"])                # 小红书解析
run_script("scripts/bili_summarize.py", [sub, owner, out, "news"])   # 总结（模板按内容类型自动分类）
```
