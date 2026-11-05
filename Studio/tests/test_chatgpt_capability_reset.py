from datetime import datetime, timedelta
import pytest
from docurandom.ui import _chatgpt_rate_limit_cooldown_seconds, _is_chatgpt_rate_limit_error
from docurandom.workspace_scheduler import VIETNAM_TIMEZONE

NOW = datetime(2026,10,3,3,15,tzinfo=VIETNAM_TIMEZONE)

@pytest.mark.parametrize('clock,expected', [
    ('3:59 PM', 12*3600+44*60+60),
    ('3:59\u202fPM', 12*3600+44*60+60),
    ('tomorrow at 3:59 PM', 36*3600+44*60+60),
    ('3:59 AM', 44*60+60),
    ('12:00 PM', 8*3600+45*60+60),
    ('12:00 AM', 300),
    ('25:00 PM', 300),
    ('3:99 PM', 300),
])
def test_use_explicit_reset_clock_and_do_not_roll_stale_notice_to_tomorrow(clock, expected):
    message=f'CHATGPT_RATE_LIMITED: Capabilities reduced until {clock}. Responses may have lower quality.'
    assert not _is_chatgpt_rate_limit_error(message.replace('CHATGPT_RATE_LIMITED: ', ''))
    assert _chatgpt_rate_limit_cooldown_seconds(message,now=NOW) == expected

def test_generic_throttle_keeps_existing_cooldown():
    assert _is_chatgpt_rate_limit_error('CHATGPT_RATE_LIMITED: Too many requests')
    assert _chatgpt_rate_limit_cooldown_seconds('Too many requests',now=NOW) == 300

def test_legacy_bridge_wrapper_does_not_rearm_account_wide_pause():
    message = ('Đọc phản hồi: CHATGPT_RATE_LIMITED: ChatGPT đang tạm giới hạn do có quá nhiều yêu cầu. '
               'Tool đã giữ checkpoint và sẽ chờ trước khi thử lại; không cần đổi tài khoản · '
               'Capabilities reduced until tomorrow at 11:45 PM. Responses may have lower quality.')
    assert not _is_chatgpt_rate_limit_error(message)
    assert _is_chatgpt_rate_limit_error('Too many requests. ' + message)
