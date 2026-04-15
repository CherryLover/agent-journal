#!/usr/bin/env python3
"""Backfill token frontmatter to existing outline and weekly-read files."""

import re
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from journal_mvp import build_report, build_token_frontmatter, DEFAULT_HOME, DEFAULT_TIMEZONE

RESULT_DIR = DEFAULT_HOME / 'Documents' / 'ObCc' / '01_Journal' / 'Agent_Journal'
TZ = ZoneInfo(DEFAULT_TIMEZONE)


def extract_date_from_filename(filename: str) -> date | None:
    """Extract date from filename like 2026-04-14_outline.md."""
    match = re.match(r'(\d{4}-\d{2}-\d{2})_', filename)
    if match:
        return date.fromisoformat(match.group(1))
    return None


def update_frontmatter(file_path: Path, new_frontmatter: str) -> bool:
    """Replace existing frontmatter with new one."""
    content = file_path.read_text()

    # Check if file has frontmatter
    if content.startswith('---\n'):
        # Find end of frontmatter
        end_match = re.search(r'\n---\n', content[4:])
        if end_match:
            body = content[4 + end_match.end():]
            file_path.write_text(new_frontmatter + body)
            return True

    # No frontmatter, add it
    file_path.write_text(new_frontmatter + content)
    return True


def main():
    outline_files = sorted(RESULT_DIR.glob('*_outline.md'))
    weekly_files = sorted(RESULT_DIR.glob('*_weekly-read.md'))

    print(f'Found {len(outline_files)} outline files and {len(weekly_files)} weekly files')

    processed_dates = set()

    for outline_file in outline_files:
        target_date = extract_date_from_filename(outline_file.name)
        if not target_date:
            print(f'  Skip: {outline_file.name} (cannot parse date)')
            continue

        if target_date in processed_dates:
            continue
        processed_dates.add(target_date)

        print(f'Processing {target_date}...', end=' ')

        try:
            report = build_report(target_date, TZ, DEFAULT_HOME)
            frontmatter = build_token_frontmatter(report)

            # Update outline file
            update_frontmatter(outline_file, frontmatter)

            # Update weekly file if exists
            weekly_file = RESULT_DIR / f'{target_date.isoformat()}_weekly-read.md'
            if weekly_file.exists():
                update_frontmatter(weekly_file, frontmatter)

            total = report.total_tokens()
            print(f'OK (total: {total:,} tokens)')
        except Exception as e:
            print(f'ERROR: {e}')


if __name__ == '__main__':
    main()
