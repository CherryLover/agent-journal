# Agent Journal MVP

A local-first prototype that reads Codex and Claude Code session logs from this machine, groups work by directory, and optionally asks an AI model to generate both a daily outline and a weekly-ready summary.

## Data sources

- `~/.codex/state_5.sqlite`
- `~/.codex/sessions/**/*.jsonl`
- `~/.claude/history.jsonl`
- `~/.claude/projects/**/*.jsonl`

## What it does

- exports real user / assistant chat records
- groups repeated sessions by `cwd`
- keeps tool usage and approximate token counts
- optionally asks `codex` or `claude` to generate:
  - a compressed daily outline from real chat logs
  - a weekly-ready summary (directory-grouped, condensed)
- writes only 2 final result files per day
- cleans intermediate files after the final files are produced

## Run without AI summarization

```bash
python3 /Users/jiangjiwei/Code/AI/agent-journal-mvp/journal_mvp.py \
  --date 2026-03-11
```

## Run with AI summarization

```bash
python3 /Users/jiangjiwei/Code/AI/agent-journal-mvp/journal_mvp.py \
  --date 2026-03-11 \
  --summarizer claude
```

You can also switch to Codex:

```bash
python3 /Users/jiangjiwei/Code/AI/agent-journal-mvp/journal_mvp.py \
  --date 2026-03-11 \
  --summarizer codex
```

## Output files

By default the script writes these final files under `~/Documents/ObCc/01_Journal/Agent_Journal/`:

- `~/Documents/ObCc/01_Journal/Agent_Journal/<date>_outline.md` - final daily outline
- `~/Documents/ObCc/01_Journal/Agent_Journal/<date>_weekly-read.md` - final weekly review material

Intermediate files such as transcript exports or `sessions.json` may be created temporarily during generation, but they are removed after the final files are written.

The script assumes `Asia/Shanghai` by default and reads logs from the current user's home directory.
