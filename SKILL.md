---
name: video-digest
version: "0.4.0"
description: 视频与图文内容解析与笔记。兼容 B站视频（单视频/UP主追更/合集系列追踪）、小红书推文/视频、微信公众号图文、本地视频文件：字幕直取或语音转写、画面核验、图片文字提取与图注化、纯视觉内容解读、结构化总结、笔记归档。Use when: 需要解析视频/图文内容、提取字幕或图片文字、核验画面、追更某个 UP 主或某个合集系列、做结构化笔记。触发词：解析这个视频、总结这个视频、解析本地视频、视频摘要、字幕提取、画面核验、提炼视频要点、看视频讲了什么、做视频笔记、解析小红书、解析公众号推文、看小红书推文、追更、跟新视频、追踪课程系列、合集追更。
author: coocA-Alex
tags: [video, digest, bilibili, xiaohongshu, notes, subtitle, vision, asr, tracking, season]
license: MIT
metadata:
  version: 0.4.0
---

# 视频解析与情报追踪

## Purpose

把视频/图文内容转成结构化、可核验的笔记：字幕直取或语音转写、画面核验、图片文字提取，事实与观点分离，供个人学习与研究复用。

## Prerequisites

- Python 3.11+ 与 `requests`；`ffmpeg`（画面核验/音频提取场景）
- MiMo API key（语音转写，兼画面核验 fallback；`MIMO_API_KEY` 环境变量或项目 .env）
- 视觉模型 key（默认 DeepSeek，`DEEPSEEK_API_KEY`；也可用 `config/ds_key.local.json`）
- LLM 总结 key（`DEEPSEEK_API_KEY` 或项目 .env / `config/ds_key.local.json`，DeepSeek flash 建议 max_tokens=50000）
- B站字幕直取需 SESSDATA（`~/.bili_sessdata`）；小红书解析需 web_session（`~/.xhs_web_session`）

## 环境依赖

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `MIMO_API_KEY` | MiMo API 密钥（语音转写；视觉 fallback） | 是（ASR 用） |
| `DEEPSEEK_API_KEY` | 视觉（默认）+ 文本总结；也可放 `config/ds_key.local.json` | 是 |
| `SESSDATA` | B站登录 cookie（AI 字幕直取，存 `~/.bili_sessdata`） | 部分视频需要 |
| web_session | 小红书登录 cookie（推文/视频解析，存 `~/.xhs_web_session`） | 小红书需要 |
| DS key（`DEEPSEEK_API_KEY` 或项目 .env / `config/ds_key.local.json`） | LLM 总结 | 是 |

| 依赖 | 说明 | 必需 |
|------|------|------|
| `ffmpeg` | 视频抽帧/音频提取 | 画面核验场景 |
| `requests` | API 调用 | 是 |
| `numpy` / `Pillow` | 调色板聚类与图像读取 | 仅纯视觉解读 (`visual_palette.py`) |

## 能力

| 能力 | 入口 | 说明 |
|------|------|------|
| 单视频字幕直取 | `scripts/bili_subtitle.py <bvid> <cid>` | B站 AI 字幕（需 SESSDATA，读 `~/.bili_sessdata` 文件，非环境变量） |
| 语音转写 | `scripts/mimo_asr.py`（经 analysis 模块） | wav/mp3 → 文本（默认 MIMO，可换模型） |
| 画面核验 | `scripts/vision.py`（编码层 `llm_codec.py`） | 图片/帧 → 视觉理解（默认 DeepSeek，MIMO 为 fallback；换模型只改配置） |
| 结构化总结 | `scripts/bili_summarize.py <subtitle> <owner> [out.md] [style] [desc]` | **内容类型模板 7 类 (MECE: 互斥穷尽)** — stock(股市收评)/ news(资讯多主题, 含财经要闻)/ teaching(教学方法论)/ tech(评测/单主题解析)/ lecture(讲座含问答)/ wx(公众号图文, 含配图图注节)/ general(兜底); **自动分流**: 显式配置(creators)优先 → LLM 分类 detect_template → general 兜底; news 类**按叙事链分节**（事件→起因→影响→观点）, 杜绝口播碎片罗列; 另可显式指定输出格式 style (keypoints/timeline/notes/opinions, 与内容类型正交); 可选传视频简介校正字幕音译; **超长字幕（>40k 字符）自动语义分块**（[30k,35k] 区间内找 [mm:ss] 时间戳行切点 → 块总结 hash 缓存 → 二次合并，避免硬切断语义）; **口径/派生/取信标注**（财经类 stock/news 模板）: 成交额/涨跌家数/市值等**口径敏感数字**标 `（口径：沪深/含北交所/全市场/口径未明）`; 由原始数字计算得出的**派生数字**（分位/均值/同比/环比/区间位置）标 `[派生·口径: <窗口或算法>]`; 画面与口播冲突处给 `建议取信：口播/画面/待核`（同一冲突在多帧复现时只标一次） |
| 多P/长视频兼容 | `scripts/bili_media.py` | `enum_pages(bvid)` 多P 枚举（112P 实测）/ `check_coverage(字幕, 时长)` 字幕覆盖比 / `needs_asr_fallback` 长视频（>20min）覆盖 <70% 判定 / `fetch_audio(bvid, cid)` dash 音频下载（Cookie+URL 刷新重试，ASR 兜底用） |
| 本地视频解析 | `scripts/local_video_pipeline.py` | 本地视频文件 → 转写 → 笔记（支持任意来源录制）；**默认 2 分钟一段**并带静默丢字防护（见 Limitations） |
| 分段讲座整合 | `scripts/local_video_merge.py` | 把同一场讲座被切成多段的录屏（`tmp/lecture/*`）按录制时间序拼接 → 单篇完整总览；**默认只合并最新一次录制会话**，`--match` 显式选、`--all` 恢复全量；长输入自动分块 |
| 纯视觉内容解读 | `scripts/visual_palette.py` | 面向「内容就是画面」的视频/图集（渐变色卡、调色板演示、配色对比）：ffmpeg 抽帧 → 帧差定格检测 → 关键帧 k-means 调色板 → HSL 设计规律 → markdown/JSON；`--labels` 另用视觉模型读画面色号并逐条校验。此类视频音频常为纯音乐，ASR 会幻觉出歌词，必须读像素 |
| 批量追踪 | `scripts/digest_daily.py` | B站关注列表增量 → 归档（creators 用 `config/creators.example.json` 模板） |
| **合集级追踪** | `scripts/digest_daily.py` + creators 的 `season_id` | 只追**指定合集**的新集，忽略该 UP 主其余投稿（`fetch_season_videos` 走 polymer `seasons_archives_list`，实测免登录免 WBI 签名）。适用「课程系列专追」：当 UP 主日更多条混杂内容（如教程 + 资讯 + 专栏）时，按 UP 主全量追更会把无关内容灌进笔记库。候选集 = 合集全集 − state 已处理，每轮按 `--max` 限流，并自动跳过 backfill 分页（合集列表本身即全集）。**三层准入（正交可组合）**：① `season_backfill` 时间维 —— `all`(默认，教程/系统课需全补) / `none`(只收启用后新集) / `since:YYYY-MM-DD`(**资讯类必配**，否则旧闻倒灌)；② `season_filter` 内容维 —— 标题子串或 `regex:...`；③ `season_llm_filter` 语义维 —— LLM 二次判断该集是否属于合集主题（治"博主把无关内容塞进同一合集"），判定按 bvid 缓存；④ `season_include_others: true` 合集**之外**也收该 UP 主的投稿（科普/教学类构成知识体系；走 space API 取最近 30 条并剔除合集内已有的，**该通道失败只降级不抛出**，保证合集通道不被拖垮）。合集端点改 `sort_reverse=true` 最新在前 + 按时间下限早停（日常 1 页代替 10 页，`all` 模式不早停），并对 `-352`/HTTP 412 做 30s/60s/90s 退避重试；候选集按发布时间**降序**取 `--max`（积压时先出最新，倒着往回追） |
| 视频抽帧 | `scripts/video_frames.py` | B站/本地视频流式抽帧 → 时间戳 manifest（ffmpeg）; 抽帧超时自动**刷新 URL 重试**（dash URL 带 deadline, 传 sessdata 时最多重试 2 次） |
| 帧画面核验管道 | `scripts/video_vision.py` | manifest 帧批量 → 视觉读帧（默认 DeepSeek，MIMO 为 fallback）→ 时间戳视觉摘要（并发 4 workers） |
| 小红书推文/视频解析 | `scripts/xhs_note.py <url> [--extract]` | 小红书链接 → 类型判断（视频/图文）→ 下载 → 可选 ASR/帧/图片文字提取（web_session 存 `~/.xhs_web_session`） |
| 公众号图文解析 | `scripts/wx_article.py <url>` | mp.weixin.qq.com → 正文提取（图片转 [图N] 占位）→ 图片下载 + 视觉图注化（默认 DeepSeek；配图成为可检索内容）→ wx 模板总结；**正文缺失时自动走 `window.desc` 兜底**（微信对部分文章只吐分享/SEO 预渲染变体，无正文 DOM，此时无配图）；幂等缓存 tmp/wx_{id}/ |
| 文档转换 | `markitdown`（微软开源, 全局 python311, LW 项目成熟用法） | PDF/docx/html → markdown，用于 arxiv 论文等文档型内容解析（`markitdown <file>` CLI 或 `from markitdown import MarkItDown`） |

## 工作流（解析一个视频）

1. **字幕**：优先 B站 AI 字幕直取；无字幕/非 B站 → 音频转写（MIMO ASR 或配置的其他模型）；**长视频（>20min）字幕覆盖 <70%**（B站 AI 字幕常只覆盖口播部分）→ 自动 ASR 兜底（`bili_media.fetch_audio` dash 下载 + 分段转写）；**整条视频完全没有 AI 字幕轨**（`no subtitle tracks`）时，批量管线记 `no_subtitle` 状态并 3 天后自动重试（B站 AI 字幕有滞后），单篇应急可直接走 ASR 兜底：`fetch_audio(bvid, cid)` → `local_video_pipeline.extract_segments`（2min/段）→ `transcribe_segments` → `summarize_subtitle` → 归档
2. **画面核验（可选）**：需要看图表/PPT/实验画面时，抽帧 → 视觉模型理解
3. **小红书推文（可选）**：`scripts/xhs_note.py <url> --extract` — 自动判断视频/图文；视频走 ASR+帧，图文走图片文字提取（正文常在图片里）
4. **总结**：LLM 结构化总结（可传视频简介 desc 校正字幕音译；general 模板 = 逻辑链+精选+思考与行动层）
5. **存疑点主动画面核验**：总结后若"数据可信度备注"有可被画面解决的存疑点（专有名词/工具名/表格数字）→ 按时间戳抽帧核验，回填笔记
6. **归档**：输出 markdown 到 data/ 或指定位置

## 配置（凭证隔离 — 重要）

- **API key 一律从环境变量读取**（agent 无关，Claude Code / Codex / 其他 agent 均可）：
  - `MIMO_API_KEY`（多模态）、`DEEPSEEK_API_KEY`（总结；本地也可用 `config/ds_key.local.json`）
- **登录凭证一律存仓库外文件**：B站 SESSDATA（`~/.bili_sessdata`）、小红书 web_session（`~/.xhs_web_session`）；scripts 只读这些路径，不打印不落盘
- **本 skill 及 scripts 中不包含任何真实 key/凭证**
- **换模型 = 改配置，不改代码**：编辑 `config/multimodal.json` 的 `asr`/`vision` 段。每段字段：
  `provider`(标记) / `protocol`(协议) / `model` / `base_url` / `api_key_env`（可选 `api_key_file`）/
  `auth`(认证头) / `params`(供应商私有开关，原样透传) / `limits`(硬限制) / `fallback`(失败降级链)
  - `protocol`：`anthropic_messages` | `openai_chat` | `transcriptions`（编码层 `scripts/llm_codec.py`）
  - `auth`：`bearer`（Authorization: Bearer）| `x-api-key` | `api-key`
  - 硬限制超限**显式报错**（不静默丢弃、不留给 API 报 400）；**配置字段名拼错也直接报错**，不会静默走默认值
- **视觉模型可选清单**（画面核验；2026-09-20 起默认 DeepSeek，MIMO 降为 fallback）：
  - `deepseek-flash`（**默认**，Anthropic 兼容端点 `https://api.deepseek.com/anthropic/v1/messages`，
    认证头 `x-api-key`，`DEEPSEEK_API_KEY`。同帧盲测：关键数字与枚举完整性均优于 MIMO）
  - `mimo-v2.5`（**fallback**，Anthropic 兼容 messages，`api-key` 头，`MIMO_API_KEY`）
  - `glm-5.3-flash`（智谱原生多模态，走 `openai_chat` 协议，`ZHIPU_API_KEY`；`thinking` 不可关，需留 max_tokens 余量）
  - GPT 视觉（走 `openai_chat` 协议，格式与 GLM 相同）
  - **接入步骤**：只要协议属于上表 3 种 → **只改配置**（protocol/model/base_url/api_key_env/auth/params/limits），不用碰代码；确实是新协议才需要新增编码器
  - **不另建常驻桥接脚本**；协议抽象设计见 `docs/2026-09-20-model-compat-design.md`
- 示例配置见 `config/multimodal.json`（不含 key）
- **Agent 兼容**：**已验证集成：Claude Code、Codex Desktop**（2026-08-29：skill 发现、包结构、脚本语法在 Codex Desktop 验证通过；端到端 provider 执行待验证）。核心能力为 Python CLI，可供能够读取 Markdown 技能说明并执行本地命令的其他 agent（Cursor/OpenClaw 等）适配；其他 agent 集成尚未实际验证。
- **key 解析顺序** = 环境变量（`api_key_env`）→ 项目本地配置（`config/ds_key.local.json`）。
  **Claude Code legacy 兜底默认关闭**：`ANTHROPIC_AUTH_TOKEN` 是 Anthropic 侧凭证，被当作 Bearer 发到上面配置的 `base_url`（可能是第三方端点）等于把 key 送错地方 → 只有显式信任该端点时才启用：在 `config/multimodal.json` 的 `summarize` 段设 `"allow_anthropic_token_fallback": true`

## 合规

- 个人学习/研究使用；遵守平台协议；不批量抓取、不商用他人内容
- 小红书仅按需解析单条推文（用户指定链接），不逆向签名、不采集列表/评论
- 输出仅供个人参考，不得二次分发

## Limitations

- B站 AI 字幕多数需登录态（cookies）；接口可能变动
- MIMO ASR 限 wav/mp3、base64 ≤10MB（长音频需分段）。**⚠️ 静默丢字（2026-09-16 实测）**：5 分钟段有 2/8 被整段丢成 `"嗯。"` 且**不报错**，重跑结果一致 → 默认段长改为 **2 分钟**，并在转写后按「字数/分钟」检测异常段（<60 字/分；健康讲课时 230–290），命中则自动拆 60s 重转再拼接。长音频务必走 `local_video_pipeline` 的分段+防护路径，不要自己裸切长段
- 多 P 视频：默认主 P（时长最长）总结 + 其余 P 有字幕则拉取并补"P 对照说明"节（digest 批量流程）；手动流程可用 `bili_media.enum_pages` 定向处理任意 P
- 小红书仅按需解析单条推文（用户指定链接）；未登录无法取视频流/互动数据

## Troubleshooting

- **总结输出为空/过短**：思考型模型（deepseek-flash）长输入时 max_tokens 不足会静默截断 → 设 max_tokens=50000 或分块；**字幕 >40k 字符自动语义分块**（`bili_summarize` 内置，块级缓存 tmp/long_summary_cache/ 支持断点续跑）
- **长视频字幕只覆盖开头**：B站 AI 字幕对长视频可能只生成口播部分（如 96min 仅 9min）→ `needs_asr_fallback` 自动判定，走 ASR 兜底；兜底失败降级纯字幕并记录 no_subtitle 重试
- **ASR 音频下载失败（dash 403/截断）**：dash 流需 UA+Referer+Cookie 完整头（`bili_media.fetch_audio` 已内置）；URL 带 deadline 中途失效 → 重试时刷新 URL；分段缓存陈旧时先清 `tmp/{bvid}_asr/`
- **抽帧超时（ffmpeg 卡住等数据）**：B站 dash 流 URL 带 deadline，过期后 ffmpeg 挂起 → `video_frames.extract_frames` 已内置超时 180s + 刷新 URL 重试 2 次（需向调用链传入 sessdata）；仍失败则降级纯字幕，不阻塞整批
- **B站 412 / 空响应**：平台风控 → 停止重试 30 分钟以上，或交给定时任务兜底；单博主失败已隔离不影响其他。**风控期间备选**：从视频页面 HTML（`curl --compressed`）提取 cid + 标题/简介，字幕走 player API（通常未风控），视频流走 playurl API（fnval=4048 取 dash）
- **小红书无 __INITIAL_STATE__**：登录态失效或链接过期 → 重新提供 web_session / 链接
- **MIMO 500**：服务端暂时故障 → 等待 90s 重试，或 `--force` 重跑
- **换了模型后报「配置含未知字段」/「配置缺 protocol 字段」**：`config/multimodal.json` 的字段名写错或漏了。字段名拼错**不会**静默走默认值 —— 这正是为了防"改了没生效"这类最难查的问题。对照 `_说明` 里的字段清单改
- **报「所有视觉通道均失败」**：主通道与 `fallback` 都失败了，错误信息里会逐条列出每个通道的失败原因（HTTP 状态 + 响应片段，不含凭证）。常见原因：key 未配（`api_key_env` 变量没设且 `api_key_file` 不可读）、协议与端点不匹配（如把 Anthropic 端点写进 `openai_chat`）
- **合集追更没生效 / 追进来一堆无关视频**：`season_id` 必须是**正整数** —— 填 `0` 或负数会直接抛错并走失败隔离（不会静默退化成"追整个 UP 主"）。合集 ID 取法：视频详情 API 的 `ugc_season.id`（`https://api.bilibili.com/x/web-interface/view?bvid=<BV>`）或合集页 URL 里的 `season_id`。报告行形如 `合集 42 集, 待处理 3 集(本轮上限 10), 新 3 条`；出现「待处理」说明本轮被 `--max` 限流，剩余下轮继续
- **讲座分段合并选错了素材**：`local_video_merge.py` 默认只合并**最新一次录制会话**（`tmp/lecture/` 会累积历届），要指定用 `--match <录制名子串>`，要全量用 `--all`
- **合集追更把旧闻/无关内容也灌进来**：合集的边界 ≠ 内容的边界。资讯类合集必须配 `season_backfill: "since:YYYY-MM-DD"`（不配会从最老集补起）；博主混装的话再加 `season_filter`（标题子串/正则）或 `season_llm_filter: true`（LLM 判是否属于合集主题，被拒的集会在报告里列出理由）。报告行带 `(补录=… / 标题筛 / LLM判)` 标记，没标记说明三层都没启用

## Available Scripts

| Script | Purpose | Arguments |
|--------|---------|-----------|
| `bili_subtitle.py` | B站 AI 字幕直取 | `<bvid> <cid>` |
| `bili_media.py` | 多P 枚举 / 字幕覆盖检测 / dash 音频下载 | 库函数: `enum_pages` `check_coverage` `needs_asr_fallback` `fetch_audio` |
| `bili_summarize.py` | 字幕 → 结构化总结（超长自动分块） | `<subtitle.txt> <owner> [out.md] [style]` |
| `xhs_note.py` | 小红书推文/视频解析（自动类型判断） | `<explore_url> [--name noteX] [--extract]` |
| `local_video_pipeline.py` | 本地视频 → 转写 → 笔记 | `<video> [--no-vision] [--owner] [--template] [--language]` |
| `local_video_merge.py` | 分段讲座录屏 → 单篇总览 | `[--owner] [--title] [--match 子串] [--all] [--force]` |
| `visual_palette.py` | 纯视觉内容（色卡/调色板）解读 | `<video\|image> [--name] [--fps N] [--labels] [--out md]` |
| `digest_daily.py` | B站关注列表/合集 批量追踪 | `[--max N] [--backfill N] [--cutoff DATE] [--no-vision]` |
| `llm_codec.py` | 协议编码层：3 种协议的请求构造 / 响应解析 / 限制校验 / 退避重试 | 库函数: `call` `encode` `decode` `check_limits` |
| `asr.py` | 转写入口：按 `multimodal.json` 路由 provider + fallback 降级 | 库函数: `analyze_audio(path, language)` |
| `vision.py` | 视觉入口：按 `multimodal.json` 路由 provider + fallback 降级 | 库函数: `analyze_image` `analyze_images` |
| `mimo_asr.py` | 音频转写（旧入口，保留兼容） | `<audio> [lang]` |
| `mimo_vision.py` | 图片/帧视觉理解（旧入口，保留兼容） | `<image...> [--prompt]` |
| `video_frames.py` | 流式抽帧 + 时间戳 manifest | `<bvid/url> [--count N]` |
| `video_vision.py` | 帧批量读帧 → 视觉摘要 | `<bvid> [--force]` |
| `wx_article.py` | 公众号图文: 正文 + 图片下载 + 视觉图注 | `<url> [--no-vision] [--force] [--skip-existing]` |

## Examples

```bash
# B站单视频字幕 + 总结
python scripts/bili_subtitle.py BV1xxx <cid>
python scripts/bili_summarize.py tmp/BV1xxx.txt 博主名 notes/out.md stock

# 小红书推文/视频（自动判断类型 + 提取）
python scripts/xhs_note.py "https://www.xiaohongshu.com/explore/<id>?xsec_token=..." --extract

# 公众号推文（正文 + 图片图注化）
python scripts/wx_article.py "https://mp.weixin.qq.com/s/xxx"
python scripts/bili_summarize.py tmp/wx_xxx/article.txt 公众号名 notes/out.md wx

# 本地视频（转写 + 画面 + 笔记）
python scripts/local_video_pipeline.py lecture.mp4 --owner 讲座

# 分段讲座录屏 → 单篇总览（默认只取最新一次录制会话）
python scripts/local_video_merge.py --owner 讲座 --match 2026.09.16

# 纯视觉内容（色卡 / 调色板演示视频，无有效口播）
python scripts/visual_palette.py palette.mp4 --name mig --labels

# 合集系列专追：在 config/creators.json 里给该博主配 season_id 即可
# {"name": "某教程号", "mid": <UP主mid>, "season_id": <合集id>,
#  "template": "tech", "vision": false, "backfill": false}
python scripts/digest_daily.py --max 10

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
