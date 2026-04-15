#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = 'Asia/Shanghai'
DEFAULT_HOME = Path.home()
DEFAULT_RESULT_DIR = DEFAULT_HOME / 'Documents' / 'ObCc' / '01_Journal' / 'Agent_Journal'
GENERIC_TITLES = {
    'Implement the following plan:',
    '[Request interrupted by user for tool use]',
}
SUMMARY_MESSAGE_LIMIT = 1200


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def add_claude_usage(self, usage: dict) -> None:
        input_tokens = int(usage.get('input_tokens', 0) or 0)
        output_tokens = int(usage.get('output_tokens', 0) or 0)
        cached_tokens = int(usage.get('cache_read_input_tokens', 0) or 0) + int(
            usage.get('cache_creation_input_tokens', 0) or 0
        )
        reasoning_tokens = int(usage.get('reasoning_output_tokens', 0) or 0)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_tokens += cached_tokens
        self.reasoning_tokens += reasoning_tokens
        self.total_tokens += input_tokens + output_tokens + cached_tokens

    def add_codex_total(self, total_tokens: int | None) -> None:
        if total_tokens is None:
            return
        self.total_tokens += max(0, int(total_tokens))

    def merge(self, other: 'TokenUsage') -> 'TokenUsage':
        merged = TokenUsage()
        merged.input_tokens = self.input_tokens + other.input_tokens
        merged.output_tokens = self.output_tokens + other.output_tokens
        merged.cached_tokens = self.cached_tokens + other.cached_tokens
        merged.reasoning_tokens = self.reasoning_tokens + other.reasoning_tokens
        merged.total_tokens = self.total_tokens + other.total_tokens
        return merged

    def to_markdown(self) -> str:
        parts = []
        if self.input_tokens:
            parts.append(f'input {self.input_tokens:,}')
        if self.output_tokens:
            parts.append(f'output {self.output_tokens:,}')
        if self.cached_tokens:
            parts.append(f'cached {self.cached_tokens:,}')
        if self.reasoning_tokens:
            parts.append(f'reasoning {self.reasoning_tokens:,}')
        if self.total_tokens:
            parts.append(f'total {self.total_tokens:,}')
        return ', '.join(parts) if parts else 'n/a'


def format_token_number(n: int | float) -> str:
    """Format number: <1M uses comma separator, >=1M uses M, >=1B uses B."""
    if n >= 1_000_000_000:
        return f'{n / 1_000_000_000:.2f}B'
    if n >= 1_000_000:
        return f'{n / 1_000_000:.2f}M'
    if isinstance(n, float):
        return f'{n:.2f}'
    return f'{n:,}'


def build_token_frontmatter(report: 'DailyReport') -> str:
    """Build YAML frontmatter with token statistics."""
    month_link = report.report_date.strftime('%Y-%m')
    lines = ['---', f'month: "[[{month_link}]]"', 'tokens:']

    # Claude Code tokens (has detailed breakdown)
    claude_usage = TokenUsage()
    for session in report.claude_sessions:
        claude_usage = claude_usage.merge(session.token_usage)
    lines.append('  claude_code:')
    lines.append(f'    input: {format_token_number(claude_usage.input_tokens)}')
    lines.append(f'    output: {format_token_number(claude_usage.output_tokens)}')
    if claude_usage.cached_tokens:
        lines.append(f'    cached: {format_token_number(claude_usage.cached_tokens)}')
    if claude_usage.reasoning_tokens:
        lines.append(f'    reasoning: {format_token_number(claude_usage.reasoning_tokens)}')
    lines.append(f'    total: {format_token_number(claude_usage.total_tokens)}')

    # Codex tokens (only total available)
    codex_total = sum(s.token_usage.total_tokens for s in report.codex_sessions)
    lines.append('  codex:')
    lines.append(f'    total: {format_token_number(codex_total)}')

    # Kiro credits
    kiro_credits = sum(s.kiro_credits for s in report.kiro_sessions)
    if kiro_credits:
        lines.append('  kiro:')
        lines.append(f'    credits: {format_token_number(kiro_credits)}')

    # Total
    total = report.total_tokens()
    lines.append(f'  total: {format_token_number(total)}')

    lines.append('---')
    return '\n'.join(lines) + '\n'


@dataclass
class TranscriptMessage:
    timestamp: datetime
    role: str
    text: str
    tool: str
    session_id: str
    cwd: str
    source: str = 'main'

    def short_role(self) -> str:
        return self.role if self.source == 'main' else f'{self.role}:{self.source}'


@dataclass
class SessionSummary:
    tool: str
    session_id: str
    title: str
    cwd: str
    started_at: datetime | None
    ended_at: datetime | None
    user_messages: list[str] = field(default_factory=list)
    assistant_messages: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    kiro_credits: float = 0.0
    notes: list[str] = field(default_factory=list)
    transcript: list[TranscriptMessage] = field(default_factory=list)

    def brief_title(self) -> str:
        title = normalize_title(self.title, self.user_messages[0] if self.user_messages else '')
        return title.strip().replace('\n', ' ') if title else self.session_id

    def top_tools(self, limit: int = 5) -> str:
        if not self.tool_calls:
            return 'none'
        return ', '.join(f'{name} x{count}' for name, count in self.tool_calls.most_common(limit))

    def time_window(self) -> str:
        return format_time_window(self.started_at, self.ended_at)


@dataclass
class DirectorySummary:
    cwd: str
    sessions: list[SessionSummary]

    def combined_transcript(self) -> list[TranscriptMessage]:
        messages: list[TranscriptMessage] = []
        for session in self.sessions:
            messages.extend(session.transcript)
        return sorted(messages, key=lambda item: item.timestamp)

    def combined_tools(self) -> Counter[str]:
        combined: Counter[str] = Counter()
        for session in self.sessions:
            combined.update(session.tool_calls)
        return combined

    def combined_tokens(self) -> TokenUsage:
        merged = TokenUsage()
        for session in self.sessions:
            merged = merged.merge(session.token_usage)
        return merged

    def topics(self, limit: int = 6) -> list[str]:
        return dedupe_preserve_order(session.brief_title() for session in self.sessions)[:limit]

    def time_window(self) -> str:
        timestamps = [item for session in self.sessions for item in (session.started_at, session.ended_at) if item]
        if not timestamps:
            return 'unknown'
        return format_time_window(min(timestamps), max(timestamps))

    def tools_used(self, limit: int = 6) -> str:
        combined = self.combined_tools()
        if not combined:
            return 'none'
        return ', '.join(f'{name} x{count}' for name, count in combined.most_common(limit))

    def transcript_clues(self, limit: int = 4) -> list[str]:
        clues = []
        for message in self.combined_transcript():
            if message.role != 'user':
                continue
            clues.append(message.text.replace('\n', ' '))
        return dedupe_preserve_order(clues)[:limit]


@dataclass
class DailyReport:
    report_date: date
    timezone: ZoneInfo
    codex_sessions: list[SessionSummary]
    claude_sessions: list[SessionSummary]
    kiro_sessions: list[SessionSummary] = field(default_factory=list)
    ai_daily_outline: str | None = None
    ai_weekly_ready: str | None = None

    @property
    def all_sessions(self) -> list[SessionSummary]:
        return sorted(
            self.codex_sessions + self.claude_sessions + self.kiro_sessions,
            key=lambda item: item.started_at or datetime.min.replace(tzinfo=self.timezone),
        )

    def directory_groups(self) -> list[DirectorySummary]:
        grouped: dict[str, list[SessionSummary]] = defaultdict(list)
        for session in self.all_sessions:
            grouped[session.cwd or 'unknown cwd'].append(session)
        summaries = [DirectorySummary(cwd=cwd, sessions=sessions) for cwd, sessions in grouped.items()]
        return sorted(
            summaries,
            key=lambda item: item.sessions[0].started_at or datetime.min.replace(tzinfo=self.timezone),
        )

    def total_tokens(self) -> int:
        return sum(session.token_usage.total_tokens for session in self.all_sessions)

    def directories(self) -> list[str]:
        return [item.cwd for item in self.directory_groups() if item.cwd]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Generate a local AI work journal from Codex and Claude session logs.')
    parser.add_argument('--date', required=True, help='Target local date in YYYY-MM-DD format.')
    parser.add_argument('--timezone', default=DEFAULT_TIMEZONE, help='IANA timezone, defaults to Asia/Shanghai.')
    parser.add_argument('--result-dir', help='Directory for final result markdown files. Defaults to ~/Documents/ObCc/01_Journal/Agent_Journal.')
    parser.add_argument('--home', default=str(DEFAULT_HOME), help='Home directory containing .codex and .claude.')
    parser.add_argument(
        '--summarizer',
        default='none',
        choices=['none', 'codex', 'claude'],
        help='Optional AI summarizer to turn grouped transcripts into a daily outline and weekly-ready summary.',
    )
    parser.add_argument('--summary-model', help='Optional model name for the selected summarizer.')
    return parser.parse_args()


def parse_iso8601(value: str) -> datetime:
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    return datetime.fromisoformat(value)


def normalize_title(title: str, fallback: str) -> str:
    cleaned = (title or '').strip()
    fallback = (fallback or '').strip()
    if not cleaned or cleaned in GENERIC_TITLES:
        return fallback
    return cleaned


def extract_user_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get('type')
            if item_type in {'text', 'input_text'} and item.get('text'):
                pieces.append(str(item['text']).strip())
        return '\n'.join(piece for piece in pieces if piece)
    return ''


def extract_assistant_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'text' and item.get('text'):
                pieces.append(str(item['text']).strip())
        return '\n'.join(piece for piece in pieces if piece)
    return ''


def format_time_window(started_at: datetime | None, ended_at: datetime | None) -> str:
    if not started_at and not ended_at:
        return 'unknown'
    if started_at and ended_at:
        return f"{started_at.strftime('%H:%M')} - {ended_at.strftime('%H:%M')}"
    single = started_at or ended_at
    return single.strftime('%H:%M') if single else 'unknown'


def local_day_bounds(target_day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    day_start = datetime.combine(target_day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


def within_local_day(ts: datetime, day_start: datetime, day_end: datetime, tz: ZoneInfo) -> bool:
    local_ts = ts.astimezone(tz)
    return day_start <= local_ts < day_end


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def collect_codex_sessions(target_day: date, tz: ZoneInfo, home: Path) -> list[SessionSummary]:
    state_db = home / '.codex' / 'state_5.sqlite'
    if not state_db.exists():
        return []

    day_start, day_end = local_day_bounds(target_day, tz)
    start_epoch = int(day_start.astimezone(timezone.utc).timestamp())
    end_epoch = int(day_end.astimezone(timezone.utc).timestamp())

    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''
            SELECT id, rollout_path, created_at, updated_at, cwd, title, tokens_used, first_user_message
            FROM threads
            WHERE updated_at >= ? AND created_at < ?
            ORDER BY created_at ASC
            ''',
            (start_epoch, end_epoch),
        ).fetchall()
    finally:
        conn.close()

    sessions: list[SessionSummary] = []
    for row in rows:
        rollout_path = Path(row['rollout_path'])
        if not rollout_path.exists():
            continue
        summary = parse_codex_rollout(row, rollout_path, target_day, tz)
        if summary:
            sessions.append(summary)
    return sessions


def parse_codex_rollout(row: sqlite3.Row, rollout_path: Path, target_day: date, tz: ZoneInfo) -> SessionSummary | None:
    day_start, day_end = local_day_bounds(target_day, tz)
    activity_times: list[datetime] = []
    user_messages: list[str] = []
    tool_calls: Counter[str] = Counter()
    token_snapshots: list[tuple[datetime, int]] = []
    notes: list[str] = []
    transcript: list[TranscriptMessage] = []
    assistant_messages = 0

    try:
        with rollout_path.open() as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                event = json.loads(raw_line)
                event_ts_text = event.get('timestamp')
                event_ts = parse_iso8601(event_ts_text) if event_ts_text else None
                if not event_ts:
                    continue
                if not within_local_day(event_ts, day_start, day_end, tz):
                    continue
                local_ts = event_ts.astimezone(tz)
                activity_times.append(local_ts)
                payload = event.get('payload', {}) if isinstance(event.get('payload'), dict) else {}

                if event.get('type') == 'event_msg':
                    payload_type = payload.get('type')
                    if payload_type == 'user_message':
                        text = str(payload.get('message', '')).strip()
                        if text:
                            user_messages.append(text)
                            transcript.append(
                                TranscriptMessage(
                                    timestamp=local_ts,
                                    role='user',
                                    text=text,
                                    tool='Codex',
                                    session_id=row['id'],
                                    cwd=row['cwd'],
                                )
                            )
                    elif payload_type == 'agent_message':
                        text = str(payload.get('message', '')).strip()
                        if text:
                            assistant_messages += 1
                            transcript.append(
                                TranscriptMessage(
                                    timestamp=local_ts,
                                    role='assistant',
                                    text=text,
                                    tool='Codex',
                                    session_id=row['id'],
                                    cwd=row['cwd'],
                                )
                            )
                    elif payload_type == 'task_complete':
                        last_agent_message = str(payload.get('last_agent_message', '')).strip()
                        if last_agent_message:
                            notes.append(last_agent_message.splitlines()[0])
                    elif payload_type == 'token_count':
                        info = payload.get('info') or {}
                        total_usage = info.get('total_token_usage') or {}
                        total_tokens = total_usage.get('total_tokens')
                        if total_tokens is not None:
                            token_snapshots.append((event_ts, int(total_tokens)))
                elif event.get('type') == 'response_item':
                    payload_type = payload.get('type')
                    if payload_type == 'function_call':
                        tool_calls[str(payload.get('name', 'function_call'))] += 1
                    elif payload_type == 'web_search_call':
                        tool_calls['web_search'] += 1
    except (json.JSONDecodeError, OSError) as exc:
        notes.append(f'Failed to parse rollout: {exc}')

    if not activity_times:
        return None

    token_delta = estimate_codex_token_delta(token_snapshots, day_start, day_end)
    summary = SessionSummary(
        tool='Codex',
        session_id=row['id'],
        title=(row['title'] or row['first_user_message'] or '').strip(),
        cwd=row['cwd'],
        started_at=min(activity_times),
        ended_at=max(activity_times),
        user_messages=dedupe_preserve_order(user_messages),
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        notes=dedupe_preserve_order(notes),
        transcript=transcript,
    )
    summary.token_usage.add_codex_total(token_delta if token_delta is not None else row['tokens_used'])
    if token_delta is None:
        summary.notes.append('Token fallback uses thread total tokens_used; per-day delta was unavailable.')
    return summary


def estimate_codex_token_delta(
    token_snapshots: list[tuple[datetime, int]], day_start: datetime, day_end: datetime
) -> int | None:
    if not token_snapshots:
        return None
    token_snapshots.sort(key=lambda item: item[0])
    start_total: int | None = None
    end_total: int | None = None
    for ts, total in token_snapshots:
        local_ts = ts.astimezone(day_start.tzinfo)
        if local_ts < day_start:
            start_total = total
        elif day_start <= local_ts < day_end:
            if start_total is None:
                start_total = 0
            end_total = total
    if end_total is None:
        return None
    return max(0, end_total - (start_total or 0))


def find_claude_session_file(claude_home: Path, project_path: str, session_id: str) -> Path | None:
    encoded = project_path.replace('/', '-')
    direct = claude_home / 'projects' / encoded / f'{session_id}.jsonl'
    if direct.exists():
        return direct
    matches = list((claude_home / 'projects').glob(f'**/{session_id}.jsonl'))
    return matches[0] if matches else None


def collect_claude_sessions(target_day: date, tz: ZoneInfo, home: Path) -> list[SessionSummary]:
    history_path = home / '.claude' / 'history.jsonl'
    claude_home = home / '.claude'
    if not history_path.exists():
        return []

    day_start, day_end = local_day_bounds(target_day, tz)
    session_prompts: dict[str, list[str]] = defaultdict(list)
    session_projects: dict[str, str] = {}

    with history_path.open() as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            record = json.loads(raw_line)
            timestamp_ms = record.get('timestamp')
            session_id = record.get('sessionId')
            if not timestamp_ms or not session_id:
                continue
            local_ts = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).astimezone(tz)
            if not (day_start <= local_ts < day_end):
                continue
            display = str(record.get('display', '')).strip()
            if display:
                session_prompts[session_id].append(display)
            project_path = str(record.get('project', '')).strip()
            if project_path:
                session_projects[session_id] = project_path

    sessions: list[SessionSummary] = []
    for session_id, prompts in session_prompts.items():
        project_path = session_projects.get(session_id)
        if not project_path:
            continue
        session_file = find_claude_session_file(claude_home, project_path, session_id)
        if not session_file:
            continue
        summary = parse_claude_session(session_file, target_day, tz)
        if not summary:
            continue
        summary.user_messages = dedupe_preserve_order(prompts + summary.user_messages)
        summary.title = normalize_title(summary.title, summary.user_messages[0] if summary.user_messages else session_id)
        sessions.append(summary)
    return sessions


def parse_claude_session(session_file: Path, target_day: date, tz: ZoneInfo) -> SessionSummary | None:
    day_start, day_end = local_day_bounds(target_day, tz)
    session_dir = session_file.with_suffix('')
    candidate_files = [session_file]
    if session_dir.exists():
        candidate_files.extend(sorted(session_dir.glob('subagents/*.jsonl')))

    activity_times: list[datetime] = []
    user_messages: list[str] = []
    tool_calls: Counter[str] = Counter()
    token_usage = TokenUsage()
    notes: list[str] = []
    transcript: list[TranscriptMessage] = []
    assistant_messages = 0
    cwd = ''
    title = session_file.stem
    seen_message_ids: set[str] = set()

    for candidate in candidate_files:
        source_name = 'main' if candidate == session_file else candidate.stem
        try:
            with candidate.open() as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    event = json.loads(raw_line)
                    event_ts_text = event.get('timestamp')
                    if not event_ts_text:
                        continue
                    event_ts = parse_iso8601(event_ts_text).astimezone(tz)
                    if not (day_start <= event_ts < day_end):
                        continue
                    activity_times.append(event_ts)
                    cwd = cwd or str(event.get('cwd', '')).strip()
                    event_type = event.get('type')

                    if event_type == 'user':
                        message = event.get('message')
                        if isinstance(message, dict):
                            text = extract_user_text_from_content(message.get('content'))
                            if text:
                                user_messages.append(text)
                                transcript.append(
                                    TranscriptMessage(
                                        timestamp=event_ts,
                                        role='user',
                                        text=text,
                                        tool='Claude Code',
                                        session_id=session_file.stem,
                                        cwd=cwd,
                                        source=source_name,
                                    )
                                )
                                if title == session_file.stem:
                                    title = text.splitlines()[0]
                    elif event_type == 'assistant':
                        message = event.get('message')
                        if not isinstance(message, dict):
                            continue
                        assistant_messages += 1
                        message_id = str(message.get('id') or event.get('uuid') or '')
                        usage = message.get('usage') or {}
                        if message_id and message_id not in seen_message_ids and usage:
                            token_usage.add_claude_usage(usage)
                            seen_message_ids.add(message_id)
                        content = message.get('content')
                        text = extract_assistant_text_from_content(content)
                        if text:
                            transcript.append(
                                TranscriptMessage(
                                    timestamp=event_ts,
                                    role='assistant',
                                    text=text,
                                    tool='Claude Code',
                                    session_id=session_file.stem,
                                    cwd=cwd,
                                    source=source_name,
                                )
                            )
                        if isinstance(content, list):
                            for item in content:
                                if not isinstance(item, dict):
                                    continue
                                if item.get('type') == 'tool_use':
                                    tool_calls[str(item.get('name', 'tool_use'))] += 1
                                elif item.get('type') == 'tool_result' and item.get('is_error'):
                                    notes.append(f"Tool error: {str(item.get('content', '')).splitlines()[0]}")
                    elif event_type == 'system' and event.get('subtype') == 'stop_hook_summary':
                        hook_count = event.get('hookCount')
                        if hook_count:
                            notes.append(f'Ran {hook_count} stop hooks.')
        except (json.JSONDecodeError, OSError) as exc:
            notes.append(f'Failed to parse {candidate.name}: {exc}')

    if not activity_times:
        return None

    return SessionSummary(
        tool='Claude Code',
        session_id=session_file.stem,
        title=title,
        cwd=cwd,
        started_at=min(activity_times),
        ended_at=max(activity_times),
        user_messages=dedupe_preserve_order(user_messages),
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        token_usage=token_usage,
        notes=dedupe_preserve_order(notes),
        transcript=transcript,
    )


def strip_kiro_path_prefix(text: str) -> str:
    """Remove leading file-path tokens from a Kiro message (e.g. dropped files)."""
    import re
    # Strip one or more path-like tokens at the start (absolute or relative paths followed by space/newline)
    cleaned = re.sub(r'^(?:\S*/\S+\s+)+', '', text).strip()
    return cleaned if cleaned else text


def collect_kiro_sessions(target_day: date, tz: ZoneInfo, home: Path) -> list[SessionSummary]:
    kiro_dir = home / '.kiro' / 'sessions' / 'cli'
    if not kiro_dir.exists():
        return []
    sessions: list[SessionSummary] = []
    for json_file in sorted(kiro_dir.glob('*.json')):
        jsonl_file = json_file.with_suffix('.jsonl')
        if not jsonl_file.exists():
            continue
        summary = parse_kiro_session(json_file, jsonl_file, target_day, tz)
        if summary:
            sessions.append(summary)
    return sessions


def parse_kiro_session(json_file: Path, jsonl_file: Path, target_day: date, tz: ZoneInfo) -> SessionSummary | None:
    day_start, day_end = local_day_bounds(target_day, tz)

    try:
        meta = json.loads(json_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    session_id = str(meta.get('session_id', json_file.stem))
    cwd = str(meta.get('cwd', '')).strip()
    raw_title = str(meta.get('title', '')).strip()
    # Kiro sometimes uses a truncated file path as title (ends with "..."); treat as generic
    title = '' if raw_title.endswith('...') and ('/' in raw_title) else raw_title

    created_text = meta.get('created_at', '')
    updated_text = meta.get('updated_at', '')
    if created_text and updated_text:
        try:
            created_ts = parse_iso8601(created_text).astimezone(tz)
            updated_ts = parse_iso8601(updated_text).astimezone(tz)
            if created_ts >= day_end or updated_ts < day_start:
                return None
        except (ValueError, TypeError):
            pass

    kiro_credits = 0.0
    session_state = meta.get('session_state') or {}
    conv_meta = session_state.get('conversation_metadata') or {}
    for turn in conv_meta.get('user_turn_metadatas') or []:
        for usage in turn.get('metering_usage') or []:
            kiro_credits += float(usage.get('value') or 0)

    activity_times: list[datetime] = []
    user_messages: list[str] = []
    tool_calls: Counter[str] = Counter()
    transcript: list[TranscriptMessage] = []
    assistant_messages = 0
    notes: list[str] = []
    last_in_day_ts: datetime | None = None  # carry timestamp forward for AssistantMessage (no meta.timestamp)

    try:
        with jsonl_file.open() as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                event = json.loads(raw_line)
                kind = event.get('kind')
                data = event.get('data') or {}
                content = data.get('content') or []

                ts_val = (data.get('meta') or {}).get('timestamp')
                if ts_val is not None:
                    try:
                        event_ts = datetime.fromtimestamp(int(ts_val), tz=timezone.utc).astimezone(tz)
                    except (ValueError, OSError):
                        event_ts = None
                    in_day = event_ts is not None and (day_start <= event_ts < day_end)
                    if in_day:
                        last_in_day_ts = event_ts
                    effective_ts = event_ts if in_day else None
                else:
                    effective_ts = last_in_day_ts  # inherit from last Prompt in the target day

                if effective_ts is None:
                    continue

                if ts_val is not None:
                    activity_times.append(effective_ts)

                if kind == 'Prompt':
                    text = '\n'.join(
                        str(item.get('data', '')).strip()
                        for item in content
                        if item.get('kind') == 'text' and item.get('data')
                    )
                    if text:
                        user_messages.append(text)
                        transcript.append(TranscriptMessage(
                            timestamp=effective_ts, role='user', text=text,
                            tool='Kiro', session_id=session_id, cwd=cwd,
                        ))
                elif kind == 'AssistantMessage':
                    assistant_messages += 1
                    text_parts: list[str] = []
                    for item in content:
                        item_kind = item.get('kind')
                        if item_kind == 'text' and item.get('data'):
                            text_parts.append(str(item['data']).strip())
                        elif item_kind == 'toolUse':
                            tool_name = str((item.get('data') or {}).get('name', 'tool_use'))
                            tool_calls[tool_name] += 1
                    text = '\n'.join(p for p in text_parts if p)
                    if text:
                        transcript.append(TranscriptMessage(
                            timestamp=effective_ts, role='assistant', text=text,
                            tool='Kiro', session_id=session_id, cwd=cwd,
                        ))
    except (json.JSONDecodeError, OSError) as exc:
        notes.append(f'Failed to parse Kiro jsonl: {exc}')

    if not activity_times:
        return None

    first_user_text = strip_kiro_path_prefix(user_messages[0]) if user_messages else ''
    summary = SessionSummary(
        tool='Kiro',
        session_id=session_id,
        title=normalize_title(title, first_user_text),
        cwd=cwd,
        started_at=min(activity_times),
        ended_at=max(activity_times),
        user_messages=dedupe_preserve_order(user_messages),
        assistant_messages=assistant_messages,
        tool_calls=tool_calls,
        kiro_credits=kiro_credits,
        notes=dedupe_preserve_order(notes),
        transcript=transcript,
    )
    return summary


def summarize_topics(sessions: list[SessionSummary], limit: int = 6) -> list[str]:
    return dedupe_preserve_order(session.brief_title() for session in sessions)[:limit]


def transcript_line(message: TranscriptMessage, truncate: int | None = None) -> str:
    text = message.text.strip()
    if truncate is not None and len(text) > truncate:
        text = text[:truncate].rstrip() + ' ...'
    text = text.replace('\r\n', '\n').strip()
    return f"[{message.timestamp.strftime('%H:%M')}] [{message.tool}] [{message.short_role()}] {text}"


def render_full_transcript(report: DailyReport) -> str:
    lines = [f'# Full Transcript Export - {report.report_date.isoformat()}', '']
    for directory in report.directory_groups():
        lines.append(f'## `{directory.cwd}`')
        lines.append(f'- Time: {directory.time_window()}')
        lines.append(f'- Sessions: {len(directory.sessions)}')
        lines.append(f'- Approx tokens: {directory.combined_tokens().to_markdown()}')
        lines.append(f'- Tools: {directory.tools_used()}')
        lines.append('')
        for session in directory.sessions:
            lines.append(f'### {session.tool} :: {session.brief_title()}')
            lines.append(f'- Session: `{session.session_id}`')
            lines.append(f'- Window: {session.time_window()}')
            lines.append(f'- Tools: {session.top_tools()}')
            lines.append('')
            if session.transcript:
                for message in session.transcript:
                    lines.append(transcript_line(message))
                    lines.append('')
            else:
                lines.append('_No text transcript extracted._')
                lines.append('')
    return '\n'.join(lines).strip() + '\n'


def render_summary_source(report: DailyReport) -> str:
    lines = [f'# Summary Source - {report.report_date.isoformat()}', '']
    lines.append('These notes are grouped by working directory and derived from real chat records.')
    lines.append('')
    for directory in report.directory_groups():
        lines.append(f'## Directory: `{directory.cwd}`')
        lines.append(f'- Time window: {directory.time_window()}')
        lines.append(f'- Sessions: {len(directory.sessions)}')
        lines.append(f'- Approx tokens: {directory.combined_tokens().to_markdown()}')
        lines.append(f'- Topics: ' + ' | '.join(f'`{topic}`' for topic in directory.topics()))
        lines.append(f'- Tools: {directory.tools_used()}')
        lines.append('### Chat transcript')
        for message in directory.combined_transcript():
            lines.append(f'- {transcript_line(message, truncate=SUMMARY_MESSAGE_LIMIT)}')
        lines.append('')
    return '\n'.join(lines).strip() + '\n'


def render_timeline_source(report: DailyReport) -> str:
    lines = [f'# Timeline Source - {report.report_date.isoformat()}', '']
    lines.append('These notes are ordered by time and derived from real chat records.')
    lines.append('')

    messages: list[tuple[SessionSummary, TranscriptMessage]] = []
    for session in report.all_sessions:
        for message in session.transcript:
            messages.append((session, message))

    messages.sort(key=lambda item: item[1].timestamp)
    last_session_key: tuple[str, str] | None = None

    for session, message in messages:
        session_key = (session.session_id, session.cwd)
        if session_key != last_session_key:
            lines.append(f'## `{session.cwd}` :: {session.tool} :: {session.brief_title()}')
            lines.append(f'- Session: `{session.session_id}`')
            lines.append(f'- Window: {session.time_window()}')
            lines.append(f'- Tools: {session.top_tools()}')
            lines.append('')
            last_session_key = session_key
        lines.append(f'- {transcript_line(message, truncate=SUMMARY_MESSAGE_LIMIT)}')
    return '\n'.join(lines).strip() + '\n'


def build_daily_outline_prompt(report: DailyReport, timeline_source_path: Path) -> str:
    date_str = report.report_date.isoformat()
    return f'''你是我的工作日志压缩助手。请先读取文件 {timeline_source_path}，然后根据里面来自 Codex、Claude Code、Kiro 的真实聊天记录，产出一份适合写个人工作日记的提纲，日期是 {date_str}。

目标：
- 先把原始聊天记录压缩成少量主线和少量次要事项。
- 不只提炼做了什么，还要提炼今天的判断、思路变化、方向感。
- 这一步不是正式日记，而是给正式日记做素材压缩。

要求：
1. 使用中文 Markdown 输出，直接从标题开始，不要写开场白。
2. 只保留当天最重要的 2 到 4 条主线；其余事情放到顺手处理和未收尾里，一条一句话。
3. 不要按早上/下午/晚上平铺叙述，也不要变成流水账；优先按今天主要围绕哪几件事打转来归并。
4. 默认不要写具体模块名、库名、API 名、分支名、commit 数量、文件名、版本号、函数名、命令名；能抽象表达就抽象表达。
5. 每条主线只说明三件事：当时在推进什么、为什么值得记、推进结果或当前状态。
6. 单独提炼今天想清楚的事：例如命名调整、架构边界、方案取舍、风险判断、工作方式变化、下一步方向。没有就宁缺毋滥。
7. 如果日志里出现很多技术实现，请主动向上抽象成基础设施搭建、版本同步、迁移验证、风险梳理、方案规划这类表述。
8. 如果某些判断只是从日志推断出来的，要明确写日志显示或看起来。
9. 不要杜撰日志里没有出现的事实。
10. 篇幅克制，提纲应该明显短于原始聊天记录。

输出结构：
# Daily Outline - {date_str}
## 今天在推进什么
- 每条 1 到 2 句话
## 今天想清楚的事
- 每条 1 到 2 句话，没有就少写
## 顺手处理的事
- 每条 1 句话
## 还没收尾的事
- 每条 1 句话
'''


def build_weekly_prompt(report: DailyReport, summary_source_path: Path) -> str:
    date_str = report.report_date.isoformat()
    return f'''你是我的工作复盘助手。请先读取文件 {summary_source_path}，然后根据里面来自 Codex、Claude Code、Kiro 的真实聊天记录，整理出一份适合做周复盘素材的总结，日期是 {date_str}。

要求：
1. 使用中文 Markdown 输出。
2. 以目录/项目为主线进行归并，同一目录下的多次会话要合并叙述。
3. 重点提炼：做了哪些分析、排查、修改、研究、决策、验证。
4. 输出风格偏总结、偏提炼，适合后续做周报或周复盘。
5. 如果某些结论只是从日志推断出来的，要明确写日志显示或看起来。
6. 不要杜撰日志中没有出现的事实。

输出结构：
# Weekly-ready Summary - {date_str}
## 今天做了什么
## 按目录回顾
## 关键产出 / 结论
## 后续线索
'''


def run_ai_summary(
    prompt: str,
    summarizer: str,
    workdir: Path,
    model: str | None,
) -> str:
    if summarizer == 'none':
        return ''
    if summarizer == 'claude':
        # 使用 --dangerously-skip-permissions 让 claude 能读取临时文件
        cmd = ['claude', '-p', '--output-format', 'text', '--dangerously-skip-permissions']
        if model:
            cmd.extend(['--model', model])
        proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"[claude error] returncode={proc.returncode}", file=sys.stderr)
            print(f"[claude error] stderr: {proc.stderr}", file=sys.stderr)
            print(f"[claude error] stdout: {proc.stdout[:500] if proc.stdout else 'None'}", file=sys.stderr)
            proc.check_returncode()  # 触发原来的异常
        return proc.stdout.strip()
    if summarizer == 'codex':
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'last.txt'
            cmd = [
                'codex',
                'exec',
                '-',
                '--skip-git-repo-check',
                '-C',
                str(workdir),
                '-s',
                'read-only',
                '--output-last-message',
                str(output_path),
            ]
            if model:
                cmd.extend(['-m', model])
            subprocess.run(cmd, input=prompt, text=True, capture_output=True, check=True)
            return output_path.read_text().strip()
    raise ValueError(f'Unsupported summarizer: {summarizer}')


def render_report(
    report: DailyReport,
    full_transcript_path: Path,
    timeline_source_path: Path,
    summary_source_path: Path,
    sessions_json_path: Path,
    ai_daily_outline_path: Path | None,
    ai_weekly_path: Path | None,
) -> str:
    codex_total = sum(session.token_usage.total_tokens for session in report.codex_sessions)
    claude_total = sum(session.token_usage.total_tokens for session in report.claude_sessions)
    kiro_credits_total = sum(session.kiro_credits for session in report.kiro_sessions)
    directories = report.directory_groups()
    topic_lines = summarize_topics(report.all_sessions)

    lines: list[str] = []
    lines.append(f'# Agent Journal - {report.report_date.isoformat()}')
    lines.append('')
    lines.append('## Snapshot')
    lines.append(f'- Local date: `{report.report_date.isoformat()}` ({report.timezone.key})')
    kiro_part = f' / `Kiro {len(report.kiro_sessions)}`' if report.kiro_sessions else ''
    lines.append(
        f'- Sessions: {len(report.all_sessions)} total (`Codex {len(report.codex_sessions)}` / `Claude Code {len(report.claude_sessions)}`{kiro_part})'
    )
    lines.append(f'- Directories: {len(directories)}')
    token_line = f'- Approx tokens: `Codex {codex_total:,}` / `Claude {claude_total:,}` / `Total {report.total_tokens():,}`'
    if kiro_credits_total:
        token_line += f' | Kiro credits: `{kiro_credits_total:.2f}`'
    lines.append(token_line)
    if topic_lines:
        lines.append('- Topics seen in logs: ' + ' | '.join(f'`{topic}`' for topic in topic_lines[:6]))
    lines.append('')

    lines.append('## Daily Outline')
    if report.ai_daily_outline:
        lines.append(report.ai_daily_outline.strip())
    else:
        lines.append('- AI daily outline was not generated in this run.')
    lines.append('')

    lines.append('## Weekly-ready Summary')
    if report.ai_weekly_ready:
        lines.append(report.ai_weekly_ready.strip())
    else:
        lines.append('- AI weekly-ready summary was not generated in this run.')
    lines.append('')

    lines.append('## Directory Worklog')
    if not directories:
        lines.append('- No active directories found for this day.')
    for directory in directories:
        lines.append(f'### `{directory.cwd}`')
        lines.append(f'- Time: {directory.time_window()}')
        lines.append(f'- Sessions: {len(directory.sessions)}')
        lines.append(f'- Approx tokens: {directory.combined_tokens().to_markdown()}')
        lines.append(f'- Topics: ' + ' | '.join(f'`{topic}`' for topic in directory.topics()))
        lines.append(f'- Tools: {directory.tools_used()}')
        clues = directory.transcript_clues()
        if clues:
            lines.append('- Chat clues: ' + '；'.join(clues[:4]))
        lines.append('')

    lines.append('## Exported Files')
    lines.append(f'- Full transcript: `{full_transcript_path}`')
    lines.append(f'- Timeline source: `{timeline_source_path}`')
    lines.append(f'- Summary source: `{summary_source_path}`')
    lines.append(f'- Structured session JSON: `{sessions_json_path}`')
    if ai_daily_outline_path:
        lines.append(f'- Raw AI daily outline: `{ai_daily_outline_path}`')
    if ai_weekly_path:
        lines.append(f'- Raw AI weekly-ready summary: `{ai_weekly_path}`')
    lines.append('')

    lines.append('## Feasibility Notes')
    lines.append('- This version uses real chat records instead of only session titles.')
    lines.append('- Work is deduplicated by `cwd`, so repeated conversations in the same directory are merged under one project path.')
    lines.append('- AI summarization is generated from grouped transcript exports, which makes the daily outline closer to the actual work process.')
    lines.append('- Token counts are still approximate; use them for workload ranking, not billing.')
    return '\n'.join(lines).strip() + '\n'


def report_to_dict(report: DailyReport) -> dict:
    return {
        'date': report.report_date.isoformat(),
        'timezone': report.timezone.key,
        'codex_sessions': [session_to_dict(item) for item in report.codex_sessions],
        'claude_sessions': [session_to_dict(item) for item in report.claude_sessions],
        'kiro_sessions': [session_to_dict(item) for item in report.kiro_sessions],
        'directories': [directory_to_dict(item) for item in report.directory_groups()],
        'ai_daily_outline': report.ai_daily_outline,
        'ai_weekly_ready': report.ai_weekly_ready,
    }


def session_to_dict(session: SessionSummary) -> dict:
    return {
        'tool': session.tool,
        'session_id': session.session_id,
        'title': session.title,
        'brief_title': session.brief_title(),
        'cwd': session.cwd,
        'started_at': session.started_at.isoformat() if session.started_at else None,
        'ended_at': session.ended_at.isoformat() if session.ended_at else None,
        'user_messages': session.user_messages,
        'assistant_messages': session.assistant_messages,
        'tool_calls': dict(session.tool_calls),
        'token_usage': {
            'input_tokens': session.token_usage.input_tokens,
            'output_tokens': session.token_usage.output_tokens,
            'cached_tokens': session.token_usage.cached_tokens,
            'reasoning_tokens': session.token_usage.reasoning_tokens,
            'total_tokens': session.token_usage.total_tokens,
        },
        'kiro_credits': session.kiro_credits,
        'notes': session.notes,
        'transcript': [
            {
                'timestamp': item.timestamp.isoformat(),
                'role': item.role,
                'text': item.text,
                'tool': item.tool,
                'source': item.source,
            }
            for item in session.transcript
        ],
    }


def directory_to_dict(directory: DirectorySummary) -> dict:
    tokens = directory.combined_tokens()
    return {
        'cwd': directory.cwd,
        'time_window': directory.time_window(),
        'topics': directory.topics(),
        'tools': dict(directory.combined_tools()),
        'token_usage': {
            'input_tokens': tokens.input_tokens,
            'output_tokens': tokens.output_tokens,
            'cached_tokens': tokens.cached_tokens,
            'reasoning_tokens': tokens.reasoning_tokens,
            'total_tokens': tokens.total_tokens,
        },
        'sessions': [item.session_id for item in directory.sessions],
        'transcript_clues': directory.transcript_clues(),
    }


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def cleanup_legacy_outputs(project_root: Path, target_day: date) -> None:
    legacy_paths = [
        project_root / 'exports' / target_day.isoformat(),
        project_root / 'reports' / f'{target_day.isoformat()}.md',
    ]
    for legacy_path in legacy_paths:
        remove_path(legacy_path)

    for empty_dir in (project_root / 'exports', project_root / 'reports'):
        if empty_dir.exists() and not any(empty_dir.iterdir()):
            empty_dir.rmdir()


def build_report(target_day: date, tz: ZoneInfo, home: Path) -> DailyReport:
    return DailyReport(
        report_date=target_day,
        timezone=tz,
        codex_sessions=collect_codex_sessions(target_day, tz, home),
        claude_sessions=collect_claude_sessions(target_day, tz, home),
        kiro_sessions=collect_kiro_sessions(target_day, tz, home),
    )


def main() -> int:
    args = parse_args()
    target_day = date.fromisoformat(args.date)
    tz = ZoneInfo(args.timezone)
    home = Path(args.home).expanduser()
    project_root = Path(__file__).resolve().parent

    result_dir = Path(args.result_dir).expanduser() if args.result_dir else DEFAULT_RESULT_DIR
    outline_output_path = result_dir / f'{target_day.isoformat()}_outline.md'
    weekly_output_path = result_dir / f'{target_day.isoformat()}_weekly-read.md'

    report = build_report(target_day, tz, home)
    timeline_source = render_timeline_source(report)
    summary_source = render_summary_source(report)

    if args.summarizer != 'none':
        with tempfile.TemporaryDirectory(prefix=f'agent-journal-{target_day.isoformat()}-') as tmpdir:
            temp_root = Path(tmpdir)
            write_text(temp_root / 'transcript-full.md', render_full_transcript(report))
            write_text(temp_root / 'timeline-source.md', timeline_source)
            write_text(temp_root / 'summary-source.md', summary_source)
            write_text(temp_root / 'sessions.json', json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))

            timeline_source_path = temp_root / 'timeline-source.md'
            summary_source_path = temp_root / 'summary-source.md'

            daily_outline_prompt = build_daily_outline_prompt(report, timeline_source_path)
            report.ai_daily_outline = run_ai_summary(daily_outline_prompt, args.summarizer, project_root, args.summary_model)
            weekly_prompt = build_weekly_prompt(report, summary_source_path)
            report.ai_weekly_ready = run_ai_summary(weekly_prompt, args.summarizer, project_root, args.summary_model)

        frontmatter = build_token_frontmatter(report)
        write_text(outline_output_path, frontmatter + report.ai_daily_outline + '\n')
        write_text(weekly_output_path, frontmatter + report.ai_weekly_ready + '\n')

    # Ensure the monthly journal file exists for Obsidian backlinks
    month_str = target_day.strftime('%Y-%m')
    monthly_file = result_dir.parent / f'{month_str}.md'
    if not monthly_file.exists():
        year_cn = target_day.strftime('%Y')
        month_cn = str(target_day.month)
        write_text(monthly_file, f'# {year_cn}年{month_cn}月工作日志\n\n')
        print(f'Created monthly journal: {monthly_file}')

    cleanup_legacy_outputs(project_root, target_day)

    if report.ai_daily_outline:
        print(f'Daily outline: {outline_output_path}')
    else:
        print('Daily outline was not generated because --summarizer is none.')
    if report.ai_weekly_ready:
        print(f'Weekly summary: {weekly_output_path}')
    else:
        print('Weekly summary was not generated because --summarizer is none.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
