"""Google task outcomes and persisted waits, independent of browser identity."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re


class GoogleTaskPending(RuntimeError):
    """Observe the existing receipt; never replay an uncertain write."""


class GoogleBlocked(PermissionError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status


class GoogleTransient(GoogleTaskPending):
    def __init__(self, status: str, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status = status
        self.retry_after = retry_after


BLOCKED = {
    'account_required': 'Chọn đúng tài khoản Google trong profile này',
    'auth_required': 'Cần đăng nhập ứng dụng trong đúng profile',
    'permission_required': 'Ứng dụng/tài liệu cần quyền truy cập hợp lệ',
    'verification_required': 'Google yêu cầu xác minh; chờ người dùng xử lý',
}
TRANSIENT = {
    'rate_limited': 'Ứng dụng đang giới hạn tạm thời; giữ biên nhận và chờ',
    'network_error': 'Trang chưa tải được do kết nối; giữ đúng tab và biên nhận',
    'service_unavailable': 'Dịch vụ tạm thời chưa sẵn sàng; giữ biên nhận',
}


def retry_after_seconds(value, *, clock: datetime | None = None) -> int | None:
    """Accept HTTP delta-seconds or an HTTP date without shortening the wait."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    try:
        if re.fullmatch(r'\d+', raw):
            seconds = int(raw)
            if seconds > 31536000:
                raise GoogleBlocked('attention', 'Máy chủ yêu cầu chờ quá lâu; giữ tác vụ để kiểm tra, không thử sớm')
            return seconds
        deadline = parsedate_to_datetime(raw)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        clock = clock or datetime.now(timezone.utc)
        seconds = max(0, math.ceil((deadline - clock).total_seconds()))
        if seconds > 31536000:
            raise GoogleBlocked('attention', 'Máy chủ yêu cầu chờ quá lâu; giữ tác vụ để kiểm tra, không thử sớm')
        return seconds
    except (ValueError, TypeError, OverflowError):
        return None


def raise_for_reply(reply: dict) -> None:
    """Use structural page state first; don't persist page text or account names."""
    status = str(reply.get('page_status') or reply.get('status') or '')
    try:
        http_status = int(reply.get('http_status') or 0)
    except (TypeError, ValueError):
        http_status = 0
    error = str(reply.get('error') or '')
    if any(code in error for code in (
        'GOOGLE_TAB_OWNERSHIP_UNCERTAIN', 'GOOGLE_TAB_APP_MISMATCH',
        'GOOGLE_TAB_DOCUMENT_CHANGED', 'GOOGLE_RECEIPT_REQUEST_MISMATCH',
        'GOOGLE_UPLOAD_RECEIPT_MISMATCH', 'GOOGLE_OWNED_TAB_GONE',
    )):
        raise GoogleBlocked('attention', 'Tab hoặc biên nhận đã thay đổi; giữ tác vụ để đối soát, không chọn tab khác')
    if status not in BLOCKED and status not in TRANSIENT:
        for name in BLOCKED:
            if ('GOOGLE_' + name.upper()) in error:
                status = name
                break
    if status not in BLOCKED:
        if http_status == 429 or 'GOOGLE_RATE_LIMITED' in error:
            status = 'rate_limited'
        elif http_status >= 500 or 'GOOGLE_SERVICE_UNAVAILABLE' in error:
            status = 'service_unavailable'
        elif 'GOOGLE_NETWORK_ERROR' in error:
            status = 'network_error'
        elif http_status == 401:
            status = 'auth_required'
        elif http_status == 403:
            status = 'permission_required'
    if status in BLOCKED:
        raise GoogleBlocked(status, BLOCKED[status])
    if status in TRANSIENT:
        wait = reply.get('retry_after_seconds')
        if wait is None:
            wait = reply.get('retry_after')
        raise GoogleTransient(status, TRANSIENT[status], retry_after_seconds(wait))


def classify_exception(exc: Exception) -> Exception:
    if isinstance(exc, (GoogleBlocked, GoogleTransient, InterruptedError)):
        return exc
    text = str(exc)
    try:
        raise_for_reply({'error': text})
    except (GoogleBlocked, GoogleTransient) as known:
        return known
    match = re.search(r'(?:HTTP(?: Error)?|status[=:])\s*(429|5\d\d)\b', text, re.I)
    if match:
        status = 'rate_limited' if match[1] == '429' else 'service_unavailable'
        wait = re.search(r'retry[-_ ]after(?:[-_ ]seconds)?\s*[=:]\s*(\d+)', text, re.I)
        return GoogleTransient(status, TRANSIENT[status], retry_after_seconds(wait[1]) if wait else None)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return GoogleTransient('network_error', TRANSIENT['network_error'])
    return exc
