# Agent Journal

[English](README.md)

从 Codex、Claude Code 和 Kiro CLI 的本地会话记录中读取数据，按工作目录分组，并可选择让 AI 生成每日工作提纲和周复盘素材。

## 支持的工具

| 工具 | 日志位置 |
|------|---------|
| **Codex** | `~/.codex/state_5.sqlite` + `~/.codex/sessions/**/*.jsonl` |
| **Claude Code** | `~/.claude/history.jsonl` + `~/.claude/projects/**/*.jsonl` |
| **Kiro CLI** | `~/.kiro/sessions/cli/*.json` + `~/.kiro/sessions/cli/*.jsonl` |

Codex 和 Claude Code 记录 token 用量，Kiro 使用积分制，两者都会在报告中展示。

## 功能

- 从三个工具中读取真实的用户 / 助手对话记录
- 按工作目录（`cwd`）合并同一项目的多次会话
- 统计工具调用次数、大致 token 用量和 Kiro 积分
- 可选调用 `claude` 或 `codex` 生成：
  - 每日工作提纲（基于真实对话压缩）
  - 周复盘素材（按目录归并，适合写周报）
- 每天只输出 2 个 Markdown 文件

## 使用方法

**只收集，不生成 AI 摘要：**

```bash
python3 journal_mvp.py --date 2026-03-11
```

**带 AI 摘要：**

```bash
python3 journal_mvp.py --date 2026-03-11 --summarizer claude
# 或者用 Codex
python3 journal_mvp.py --date 2026-03-11 --summarizer codex
```

**完整参数：**

```
--date          目标日期，格式 YYYY-MM-DD（必填）
--summarizer    none | claude | codex（默认 none）
--summary-model 摘要模型名称（可选）
--result-dir    输出目录
--timezone      IANA 时区（默认 Asia/Shanghai）
--home          读取日志的 home 目录（默认 ~）
```

## 输出

默认写入 `~/Documents/ObCc/01_Journal/Agent_Journal/`，可通过 `--result-dir` 改为任意目录：

```bash
python3 journal_mvp.py --date 2026-03-11 --summarizer claude \
  --result-dir ~/my-journal
```

每天输出两个文件：

- `<date>_outline.md` — 每日工作提纲
- `<date>_weekly-read.md` — 周复盘素材
