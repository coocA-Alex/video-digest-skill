# video-digest-skill — Video Parsing & Notes

> **Stop re-watching videos to remember them.** video-digest turns Bilibili videos and local recordings into structured, verified notes — pulling subtitles or transcribing speech, checking key visuals with vision models, and separating hard facts from opinions. Bring your own models: MiMo by default, any OpenAI-compatible provider via one config line.

[English](README.md) | [简体中文](README.zh-CN.md)

## Highlights

- **Dual input**: Bilibili AI subtitles (second-precision) or any local video file
- **Multimodal pipeline**: speech transcription + frame-level vision verification
- **Fact/opinion split**: structured summaries that keep claims verifiable
- **Bring your own model**: one config line swaps in any OpenAI-compatible provider
- **Zero credentials in repo**: keys live in environment variables only
- **Agent-agnostic**: works with Claude Code, Codex, Cursor, OpenClaw

> Personal-use tool, open-sourced. Built on Xiaomi MiMo multimodal models (ASR + vision) and DeepSeek for summarization — all swappable via config.

## Capabilities

| Capability | Script | Notes |
|------------|--------|-------|
| Bilibili subtitle fetch | `scripts/bili_subtitle.py` | AI subtitles, second-precision (requires login) |
| Speech transcription | `scripts/mimo_asr.py` | wav/mp3 → text (MiMo ASR by default, swappable) |
| Vision verification | `scripts/mimo_vision.py` | image/frame → visual understanding (MiMo Vision by default, swappable) |
| Structured summary | `scripts/bili_summarize.py` | fact/opinion dual-track template |
| Local video parsing | `scripts/lecture_pipeline.py` | local video file → transcript → notes (any recorded source) |

## Install

```bash
git clone https://github.com/coocA-Alex/video-digest-skill.git
# Option 1: user-level
cp -r video-digest-skill ~/.claude/skills/video-digest
# Option 2: project-level
cp -r video-digest-skill <your-project>/.claude/skills/video-digest
```

## Usage

Natural-language triggers: **"parse this video [URL/BV]" / "parse this local video [path]" / "summarize this video" / "make video notes" / "extract subtitles" / "verify frames"**

## Configuration (credential isolation — important)

1. Create `.env` in the project root (gitignored, **never commit**):
   ```
   MIMO_API_KEY=your_mimo_platform_key
   DEEPSEEK_API_KEY=your_deepseek_key
   SESSDATA=your_bilibili_cookie  # optional, some videos need it
   ```
2. Swappable models: edit `config/multimodal.json` (asr/vision/summarize sections: provider/model/base_url/api_key_env). OpenAI-compatible swap = config change; different protocols need a new adapter script.
3. **Agent compatibility**: SKILL.md is standard format (Claude Code / Codex / Cursor / OpenClaw); scripts are plain Python CLI with no agent dependency; key resolution order = environment → project-local config → Claude Code global config (legacy fallback).

## Open-source notice

- **MIT license** (see LICENSE): free to use, copy, modify, distribute (incl. commercial), with attribution retained
- Provided "as is", no warranty, use at your own risk
- **Privacy**: no real credentials in this repo (`.env` gitignored); never commit personal keys
- **Maintenance**: personal-use tool, best-effort maintenance; PRs for fixes welcome, complex features → fork

## Disclaimer

- For **personal learning/research only**: follow Bilibili ToS, no commercial/bulk scraping
- Subtitle/frame content belongs to original creators and platforms; summaries for personal reference only
- Cookie-heavy access may trigger account risk control — use at your own risk
- Platform APIs may change and break — normal for this kind of tool

## Directory

```
video-digest-skill/
├── SKILL.md              ← skill definition (frontmatter + workflow)
├── scripts/              ← plain Python CLI (agent-independent)
├── config/
│   ├── multimodal.json   ← model config (no keys)
│   └── creators.example.json
├── requirements.txt
└── LICENSE
```

## Acknowledgements

- [Xiaomi MiMo](https://github.com/XiaomiMiMo) — MiMo multimodal models (ASR + vision) and platform
- [DeepSeek](https://www.deepseek.com/) — summarization model API
- Compliance/open-source doc structure inspired by [bilibili-video-summary](https://github.com/bfftp0502/bilibili-video-summary)

## Contributors

- [coocA-Alex](https://github.com/coocA-Alex) — author
- [DeepSeek](https://www.deepseek.com/) — AI-assisted development (model-driven)
