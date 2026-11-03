"""Read-only summaries of retained history, never an additional usage log."""
from collections import Counter
from datetime import datetime, timedelta


def summarize(rows, now=None):
    now = now or datetime.now()
    today = now.date()
    week = today - timedelta(days=today.weekday())
    days = Counter()
    commands = Counter()
    holds = []
    durations = []
    dates = set()
    first = None
    total = weekly = daily = dictations = command_count = 0
    for row in rows:
        row = dict(row)
        try:
            stamp = datetime.fromisoformat(row['timestamp'])
            if stamp.tzinfo:
                stamp = stamp.astimezone().replace(tzinfo=None)
        except (KeyError, TypeError, ValueError):
            continue
        if stamp > now:
            continue
        day = stamp.date()
        first = min(first, day) if first else day
        if row.get('status', 'success') != 'success':
            continue
        dates.add(day)
        kind = row.get('entry_type', 'dictation')
        if kind in ('command', 'wake_command'):
            command_count += 1
            if row.get('matched_command') and not row.get('privacy_reason'):
                commands[row['matched_command']] += 1
        if kind != 'dictation':
            continue
        words = 0 if row.get('privacy_reason') else len((row.get('display_text') or '').split())
        duration = max(0, int(row.get('duration_ms') or 0))
        dictations += 1
        total += words
        weekly += words if day >= week else 0
        daily += words if day == today else 0
        days[day] += words
        durations.append(duration)
        if row.get('mode') == 'hold':
            holds.append((words, duration))
    cursor = today if today in dates else today - timedelta(days=1)
    streak = 0
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    span = (today - first).days + 1 if first else 0
    return dict(first=first, today=daily, week=weekly, total=total,
                dictations=dictations, commands=command_count,
                average_hold=sum(w for w, _ in holds) / len(holds) if holds else 0,
                longest_hold=max((d for _, d in holds), default=0),
                holds_per_day=len(holds) / span if span else 0,
                duration=sum(durations), streak=streak,
                top_commands=commands.most_common(10),
                daily=[(today - timedelta(days=i), days[today - timedelta(days=i)])
                       for i in reversed(range(30))])


def read_stats(store, now=None):
    def rows():
        if store is None:
            return
        before = None
        while True:
            batch = store.query(limit=1000, before_id=before)
            if not batch:
                return
            yield from batch
            next_id = min(row['id'] for row in batch)
            if before is not None and next_id >= before:
                raise ValueError('History pagination did not advance')
            before = next_id
    return summarize(rows(), now)
