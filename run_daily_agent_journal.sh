#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$HOME/Code/AI/agent-journal-mvp"
PYTHON_BIN="/opt/homebrew/bin/python3"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/agent-journal.log"

export SHELL="/bin/zsh"
export TZ="Asia/Shanghai"
export LANG="zh_CN.UTF-8"
export LC_ALL="zh_CN.UTF-8"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

set +e
set +u
[[ -f "$HOME/.zprofile" ]] && source "$HOME/.zprofile" >/dev/null 2>&1 || true
[[ -f "$HOME/.zshrc" ]] && source "$HOME/.zshrc" >/dev/null 2>&1 || true
set -e
set -u

mkdir -p "$LOG_DIR"

CURRENT_DATE="$(date +%F)"
CURRENT_TIME="$(date '+%F %T %Z')"

{
  echo "[$CURRENT_TIME] start daily agent journal for $CURRENT_DATE"
  "$PYTHON_BIN" "$PROJECT_DIR/journal_mvp.py" --date "$CURRENT_DATE" --summarizer claude
  echo "[$(date '+%F %T %Z')] finished daily agent journal for $CURRENT_DATE"
  echo
} >> "$LOG_FILE" 2>&1
