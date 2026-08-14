---
name: video-digest
description: 视频内容解析与笔记。兼容 B站视频与本地视频文件：字幕直取或语音转写、画面核验、结构化总结、笔记归档。触发词：解析这个视频、总结这个视频、解析本地视频、视频摘要、字幕提取、画面核验、提炼视频要点、看视频讲了什么、做视频笔记。
license: MIT
metadata:
  version: 0.1.0
---

# 视频解析与情报追踪

## 环境依赖

| 环境变量 | 说明 | 必需 |
|---------|------|------|
| `MIMO_API_KEY` | MiMo API 密钥（多模态，与官方 MiMo-Skills 一致） | 是 |
| `SESSDATA` | B站登录 cookie（AI 字幕直取） | 部分视频需要 |
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
| 结构化总结 | `scripts/bili_summarize.py <subtitle> <owner> [out.md] [style]` | 5 领域模板（stock/finance/tech/general/lecture）+ 4 风格（keypoints/timeline/notes/opinions） |
| 本地视频解析 | `scripts/lecture_pipeline.py` | 本地视频文件 → 转写 → 笔记（支持任意来源录制） |
| 批量追踪 | `scripts/digest_weekly.py` | B站关注列表增量 → 归档 |

## 工作流（解析一个视频）

1. **字幕**：优先 B站 AI 字幕直取；无字幕/非 B站 → 音频转写（MIMO ASR 或配置的其他模型）
2. **画面核验（可选）**：需要看图表/PPT/实验画面时，抽帧 → 视觉模型理解
3. **总结**：LLM 结构化总结（事实/观点双轨，可衔接预测提取）
4. **归档**：输出 markdown 到 data/ 或指定位置

## 配置（凭证隔离 — 重要）

- **API key 一律从环境变量读取**（agent 无关，Claude Code / Codex / 其他 agent 均可）：
  - `MIMO_API_KEY`（多模态）、`DEEPSEEK_API_KEY`（总结，兼容 `ANTHROPIC_AUTH_TOKEN`）、B站 SESSDATA（存 `~/.bili_sessdata`，仓库外）
- **本 skill 及 scripts 中不包含任何真实 key/凭证**
- **换模型**：编辑 `config/multimodal.json`（asr/vision/summarize 段的 provider/model/base_url/api_key_env），
  例如总结换 OpenAI 兼容模型 = 改 base_url + model + api_key_env；协议不同的模型需新增适配器脚本
- 示例配置见 `config/multimodal.json`（不含 key）
- **Agent 兼容**：SKILL.md 为标准格式（Claude Code / Codex / Cursor / OpenClaw 通用）；scripts 为纯 Python CLI 不依赖 agent；key 解析顺序 = 环境变量 → 项目本地配置 → Claude Code 全局配置（向后兼容）

## 合规

- 个人学习/研究使用；遵守平台协议；不批量抓取、不商用他人内容
- 输出仅供个人参考，不得二次分发

## 已知限制

- B站 AI 字幕多数需登录态（cookies）；接口可能变动
- MIMO ASR 限 wav/mp3、base64 ≤10MB（长音频需分段）
- 多 P 视频默认总结第一 P
