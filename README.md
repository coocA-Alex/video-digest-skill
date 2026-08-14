# video-digest-skill — 视频解析与情报追踪

Claude Code skill：**兼容 B站视频与本地视频文件** — **字幕直取或语音转写 → 画面核验 → 结构化总结 → 笔记归档**。本地视频（任意来源录制，如讲座/课程/会议）同样支持完整解析与笔记。

> 个人自用工具开源分享。基于 Xiaomi MiMo 多模态模型（ASR + 视觉），多模态模型可配置替换。

## 能力

| 能力 | 脚本 | 说明 |
|------|------|------|
| B站字幕直取 | `scripts/bili_subtitle.py` | AI 字幕，精确到秒（需登录态） |
| 语音转写 | `scripts/mimo_asr.py` | wav/mp3 → 文本（默认 MiMo ASR，可换模型） |
| 画面核验 | `scripts/mimo_vision.py` | 图片/帧 → 视觉理解（默认 MiMo Vision，可换模型） |
| 结构化总结 | `scripts/bili_summarize.py` | 事实/观点双轨模板 |
| 本地视频解析 | `scripts/lecture_pipeline.py` | 本地视频文件 → 转写 → 笔记（任意来源录制） |

## 安装

```bash
# 拷贝 skill 目录到你的 skills 目录
git clone https://github.com/coocA-Alex/video-digest-skill.git
# 方式一: 项目级
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
   SESSDATA=你的B站登录cookie（可选，部分视频字幕需要）
   ```
2. 多模态模型可替换：编辑 `config/multimodal.json`（asr/vision 的 provider/model/base_url/api_key_env），
   换 OpenAI 兼容模型 = 改配置；协议不同的模型需新增适配器脚本。

## 开源说明

- **MIT 许可证**（见 LICENSE）：可自由使用、复制、修改、分发（含商用），需保留版权声明
- 软件按"原样"提供，无担保，使用后果自负
- **隐私**：仓库不含任何真实凭据（`.env`/cookies 已 gitignore）；请勿提交个人 key
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
│   ├── multimodal.json   ← 多模态模型配置（不含 key）
│   └── creators.example.json
├── requirements.txt
└── LICENSE
```

## 鸣谢

- [Xiaomi MiMo](https://github.com/XiaomiMiMo) — MiMo 多模态模型与开放平台
- 借鉴 [bilibili-video-summary](https://github.com/bfftp0502/bilibili-video-summary) 的合规与开源文档结构
