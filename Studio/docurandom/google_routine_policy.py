"""Explicit work schedules and local usage accounting, independent of account reputation."""
from __future__ import annotations

from datetime import datetime, timedelta
import re

DEFAULTS = {
    'daily_cycle_limit': 3,
    'max_cycle_minutes': 10,
    'active_start': '00:00',
    'active_end': '00:00',
    'weekdays': list(range(7)),
}


def validate(value):
    c = {**DEFAULTS, **value}
    if type(c['daily_cycle_limit']) is not int or not 1 <= c['daily_cycle_limit'] <= 24:
        raise ValueError('Số lượt mới/ngày phải từ 1 đến 24')
    if type(c['max_cycle_minutes']) is not int or not 1 <= c['max_cycle_minutes'] <= 60:
        raise ValueError('Thời gian mỗi phiên phải từ 1 đến 60 phút')
    for field in ['active_start', 'active_end']:
        if not isinstance(c[field], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', c[field]):
            raise ValueError('Giờ chạy phải ở dạng HH:MM (00:00–23:59)')
    days = c['weekdays']
    if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days):
        raise ValueError('Chọn ít nhất một ngày chạy trong tuần')
    c['weekdays'] = sorted(set(days))
    return c


def _minute(text):
    h, m = map(int, text.split(':'))
    return h * 60 + m


def in_window(c, clock):
    start, end = _minute(c['active_start']), _minute(c['active_end'])
    minute = clock.hour * 60 + clock.minute
    if start == end:
        return clock.weekday() in c['weekdays']
    if start < end:
        return clock.weekday() in c['weekdays'] and start <= minute < end
    # After midnight belongs to the selected day on which this window began.
    if minute >= start:
        return clock.weekday() in c['weekdays']
    return minute < end and (clock - timedelta(days=1)).weekday() in c['weekdays']


def next_window(c, clock):
    candidate = clock.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        if in_window(c, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError('Không tìm được khung giờ chạy trong tuần')


def gate(config, state, clock):
    c = validate(config)
    if not in_window(c, clock):
        return {'status': 'waiting_window', 'detail': 'Chờ khung giờ Google đã chọn (giờ Việt Nam)',
                'next_run_at': next_window(c, clock).isoformat()}
    day = clock.date().isoformat()
    used = len(state.get('daily_usage', {}).get(day, {}).get('cycles', []))
    # Recovery continues the same logical cycle; it never grants a new quota slot.
    if not(state.get('cycle_id')and state.get('plan')) and used >= c['daily_cycle_limit']:
        tomorrow = (clock + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        when = tomorrow if in_window(c, tomorrow) else next_window(c, tomorrow)
        return {'status': 'daily_limit', 'detail': 'Đã đủ số lượt Google mới trong ngày; giữ tác vụ đang có',
                'next_run_at': when.isoformat()}
    return None


def reserve(state, cycle_id, clock):
    """Persist together with the plan before opening a profile; retry is charged once."""
    usage = state.setdefault('daily_usage', {})
    # A recovery spanning midnight retains its original reservation.
    if not any(cycle_id in row.get('cycles', []) for row in usage.values()):
        row = usage.setdefault(clock.date().isoformat(), {'cycles': [], 'elapsed_seconds': 0})
        row['cycles'].append(cycle_id)
    cutoff = (clock.date() - timedelta(days=8)).isoformat()
    for day in list(usage):
        if day < cutoff:
            del usage[day]


def account_elapsed(state, clock, seconds):
    row = state.setdefault('daily_usage', {}).setdefault(clock.date().isoformat(), {'cycles': [], 'elapsed_seconds': 0})
    row['elapsed_seconds'] = round(float(row.get('elapsed_seconds', 0)) + max(0, seconds), 3)


def summary(state, events, clock):
    day = clock.date().isoformat()
    todays = [e for e in events if str(e.get('at', '')).startswith(day)]
    complete = {e.get('activity_cycle_id') for e in todays if e.get('status') == 'complete' and e.get('activity_cycle_id')}
    row = state.get('daily_usage', {}).get(day, {})
    return {'date': day, 'cycles_started': len(row.get('cycles', [])),
            'cycles_complete_in_retained_history': len(complete),
            'elapsed_seconds': row.get('elapsed_seconds', 0),
            'recovery_events_in_retained_history': sum(e.get('status') == 'recovering' for e in todays),
            'last_success_by_app': state.get('last_success_by_app', {}),
            'latest_status': state.get('status'), 'history_limit': 100,
            'measurement': 'local_task_results_only'}
