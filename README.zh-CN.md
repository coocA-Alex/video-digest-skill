# video-digest-skill — 视频解析与笔记

> **不用重看视频就能记住它。** video-digest 把 B站视频和本地录制转成结构化、可核验的笔记 — 字幕直取或语音转写、视觉模型核验关键画面、严格区分事实与观点。模型自带：默认 MiMo，改一行配置即可换成任意 OpenAI 兼容模型。

[English](README.md) | 简体中文

## 亮点

- **双源输入**：B站 AI 字幕（精确到秒）或任意本地视频文件
- **多模态管道**：语音转写 + 帧级画面核验
- **事实/观点分离**：结构化总结，让每条论断可独立核验
- **模型自带**：一行配置切换任意 OpenAI 兼容模型
- **零凭证入库**：key 只走环境变量
- **多 agent 兼容**：Claude Code / Codex / Cursor / OpenClaw 通用

> 个人自用工具开源分享。基于 Xiaomi MiMo 多模态模型（ASR + 视觉）与 DeepSeek 总结模型，均可配置替换。

## 能力

| 能力 | 脚本 | 说明 |
|------|------|------|
| B站字幕直取 | `scripts/bili_subtitle.py` | AI 字幕，精确到秒（需登录态） |
| 语音转写 | `scripts/mimo_asr.py` | wav/mp3 → 文本（默认 MiMo ASR，可换模型） |
| 画面核验 | `scripts/mimo_vision.py` | 图片/帧 → 视觉理解（默认 MiMo Vision，可换模型） |
| 结构化总结 | `scripts/bili_summarize.py` | 事实/观点双轨模板 |
| 本地视频解析 | `scripts/local_video_pipeline.py` | 本地视频文件 → 转写 → 笔记（任意来源录制） |
| 批量追踪 | `scripts/digest_weekly.py` | B站关注列表增量 → 归档（creators 用 `config/creators.example.json` 模板） |
| 视频抽帧 | `scripts/video_frames.py` | 流式抽帧 → 时间戳 manifest（ffmpeg） |
| 帧画面核验管道 | `scripts/video_vision.py` | 帧批量 → MiMo 读帧 → 时间戳视觉摘要（并发 4 workers） |

## 安装

```bash
git clone https://github.com/coocA-Alex/video-digest-skill.git
# 方式一: 用户级
cp -r video-digest-skill ~/.claude/skills/video-digest
# 方式二: 项目级
cp -r video-digest-skill <your-project>/.claude/skills/video-digest
```

## 使用

自然语言触发：**"解析这个视频 [URL/BV号]" / "解析这个本地视频 [文件路径]" / "总结这个视频" / "做视频笔记" / "字幕提取" / "画面核验"**

## 配置（凭证隔离 — 重要）

1. 项目根创建 `.env`（已 gitignore，**绝不提交**）：
   ```
   MIMO_API_KEY=你的MiMo开放平台key
   DEEPSEEK_API_KEY=你的DeepSeek key
   SESSDATA=你的B站登录cookie（可选，部分视频字幕需要）
   ```
2. 多模态模型可替换：编辑 `config/multimodal.json`（asr/vision/summarize 段的 provider/model/base_url/api_key_env），换 OpenAI 兼容模型 = 改配置；协议不同的模型需新增适配器脚本。
3. **Agent 兼容**：SKILL.md 为标准格式（Claude Code / Codex / Cursor / OpenClaw 通用）；scripts 为纯 Python CLI 不依赖 agent；key 解析顺序 = 环境变量 → 项目本地配置 → Claude Code 全局配置（向后兼容）。

## 开源说明

- **MIT 许可证**（见 LICENSE）：可自由使用、复制、修改、分发（含商用），需保留版权声明
- 软件按"原样"提供，无担保，使用后果自负
- **隐私**：仓库不含任何真实凭据（`.env` 已 gitignore）；请勿提交个人 key
- **维护预期**：个人自用工具，佛系维护，不保证及时修复；欢迎修复 PR，复杂功能建议 fork

## 免责声明

- 仅供**个人学习与研究**使用：遵守 B站《用户协议》与相关法规，勿商用、勿批量抓取
- 字幕/画面内容版权归原作者与平台；总结输出仅供个人参考
- cookies 高频访问可能触发账号风控，风险自负
- 平台接口可能变更导致功能失效，属正常现象

## 目录结构

```
video-digest-skill/
├── SKILL.md              ← skill 说明书（frontmatter + 工作流）
├── scripts/              ← 通用 Python CLI（不依赖 agent，可独立调用）
├── config/
│   ├── multimodal.json   ← 模型配置（不含 key）
│   └── creators.example.json
├── requirements.txt
└── LICENSE
```

## 鸣谢

- [Xiaomi MiMo](https://github.com/XiaomiMiMo) — MiMo 多模态模型（ASR + 视觉）与开放平台
- [DeepSeek](https://www.deepseek.com/) — 总结模型 API
- 合规与开源文档结构借鉴 [bilibili-video-summary](https://github.com/bfftp0502/bilibili-video-summary)

## Contributors

- [coocA-Alex](https://github.com/coocA-Alex) — 项目作者
- [DeepSeek](https://www.deepseek.com/) — AI 辅助开发（模型驱动开发）

> 开发工具链：Claude Code 作为编码 agent 客户端，DeepSeek 模型作为 LLM 后端。
