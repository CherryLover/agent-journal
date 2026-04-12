# Agent Journal

A local-first tool that reads session logs from Codex, Claude Code, and Kiro CLI, groups work by directory, and optionally asks an AI model to generate a daily outline and a weekly-ready summary.

## Supported tools

| Tool | Log location |
|------|-------------|
| **Codex** | `~/.codex/state_5.sqlite` + `~/.codex/sessions/**/*.jsonl` |
| **Claude Code** | `~/.claude/history.jsonl` + `~/.claude/projects/**/*.jsonl` |
| **Kiro CLI** | `~/.kiro/sessions/cli/*.json` + `~/.kiro/sessions/cli/*.jsonl` |

Codex and Claude Code store token counts; Kiro uses a credit system — both are shown in the report.

## What it does

- reads real user / assistant chat records from all three tools
- groups sessions by working directory (`cwd`)
- tracks tool usage, approximate token counts, and Kiro credits
- optionally asks `claude` or `codex` to generate:
  - a compressed daily outline (from real chat logs)
  - a weekly-ready summary (directory-grouped, condensed)
- writes 2 final Markdown files per day

## Usage

**Without AI summarization** (just collect and export):

```bash
python3 journal_mvp.py --date 2026-03-11
```

**With AI summarization:**

```bash
python3 journal_mvp.py --date 2026-03-11 --summarizer claude
# or
python3 journal_mvp.py --date 2026-03-11 --summarizer codex
```

**All options:**

```
--date          Target date in YYYY-MM-DD format (required)
--summarizer    none | claude | codex  (default: none)
--summary-model Optional model name for the summarizer
--result-dir    Output directory for final Markdown files
--timezone      IANA timezone string (default: Asia/Shanghai)
--home          Home directory to read logs from (default: ~)
```

## Output

By default the script writes to `~/Documents/ObCc/01_Journal/Agent_Journal/`. To use a different directory, pass `--result-dir`:

```bash
python3 journal_mvp.py --date 2026-03-11 --summarizer claude \
  --result-dir ~/my-journal
```

Two files are written per day:

- `<date>_outline.md` — daily outline
- `<date>_weekly-read.md` — weekly review material
