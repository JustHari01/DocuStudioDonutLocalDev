"""Ordinary browser input on a caller-owned CDP target; no desktop cursor or timing model."""
from __future__ import annotations

import math

from .google_task_policy import GoogleBlocked, GoogleTaskPending


class BrowserInput:
    def __init__(self, cdp, check_target):
        self.cdp = cdp
        self.check_target = check_target
        self.focused_node = None

    def command(self, method, params=None):
        self.check_target()
        return self.cdp.command(method, params or {})

    def press(self, key, code, number, modifiers=0):
        params = {'key': key, 'code': code, 'windowsVirtualKeyCode': number,
                  'nativeVirtualKeyCode': number, 'modifiers': modifiers}
        # A failed response may still mean keyDown was delivered. Release only
        # that key on the same target even if cancellation/navigation followed.
        self.check_target()
        self.check_focus()
        try:
            self.cdp.command('Input.dispatchKeyEvent', {'type': 'keyDown', **params})
        finally:
            self.cdp.command('Input.dispatchKeyEvent', {'type': 'keyUp', **params})
        if key in {'Enter', 'Tab', 'Escape'}:
            self.focused_node = None  # These deliberately hand focus to the editor.

    def insert_text(self, text):
        if not isinstance(text, str) or len(text) > 100_000:
            raise ValueError('Nội dung nhập phải là văn bản trong giới hạn tác vụ')
        self.check_focus()
        self.command('Input.insertText', {'text': text})

    def check_focus(self):
        if self.focused_node is None: return
        node_id, backend_id = self.focused_node
        ax = self.command('Accessibility.getPartialAXTree', {'nodeId': node_id, 'fetchRelatives': False})
        focused = any(n.get('backendDOMNodeId') == backend_id and any(
            p.get('name') == 'focused' and p.get('value', {}).get('value') is True
            for p in n.get('properties', [])) for n in ax.get('nodes', []))
        if not focused:
            raise GoogleTaskPending('Ô nhập chưa xác nhận nhận bàn phím; chưa nhập nội dung')

    def click_input(self, selector):
        """Click one visible, unoccluded text field, then prove it owns focus."""
        self.focused_node = None
        if not isinstance(selector, str) or not selector or len(selector) > 500:
            raise ValueError('Bộ chọn ô nhập không hợp lệ')
        root = self.command('DOM.getDocument', {'depth': 0})['root']['nodeId']
        nodes = self.command('DOM.querySelectorAll', {'nodeId': root, 'selector': selector})['nodeIds']
        if len(nodes) != 1:
            raise GoogleTaskPending('Không có duy nhất ô nhập của đúng tài liệu')
        node_id = nodes[0]
        node = self.command('DOM.describeNode', {'nodeId': node_id})['node']
        pairs = node.get('attributes', [])
        attrs = {str(k).lower(): str(v) for k, v in zip(pairs[::2], pairs[1::2])}
        if node.get('nodeName') not in {'INPUT', 'TEXTAREA'} or attrs.get('type', '').lower() in {
                'password', 'hidden', 'file', 'submit', 'button', 'checkbox', 'radio'}:
            raise GoogleBlocked('attention', 'Từ chối nhập vào ô mật khẩu hoặc phần tử không phải ô văn bản')
        if 'disabled' in attrs or 'readonly' in attrs:
            raise GoogleTaskPending('Ô nhập đang bị khóa; chưa gửi thao tác bàn phím')
        self.command('DOM.scrollIntoViewIfNeeded', {'nodeId': node_id})
        quad = self.command('DOM.getBoxModel', {'nodeId': node_id})['model']['border']
        if len(quad) != 8 or any(type(v) not in {int, float} or not math.isfinite(v) for v in quad):
            raise GoogleTaskPending('Chưa xác định được vùng nhấp của ô nhập')
        xs, ys = quad[::2], quad[1::2]
        if max(xs) <= min(xs) or max(ys) <= min(ys):
            raise GoogleTaskPending('Ô nhập chưa có vùng hiển thị')
        x, y = sum(xs) / 4, sum(ys) / 4
        metrics = self.command('Page.getLayoutMetrics')
        viewport = metrics.get('cssVisualViewport') or metrics.get('cssLayoutViewport') or {}
        if not (0 <= x < viewport.get('clientWidth', 0) and 0 <= y < viewport.get('clientHeight', 0)):
            raise GoogleTaskPending('Ô nhập chưa nằm trong cửa sổ hiển thị')
        hit = self.command('DOM.getNodeForLocation', {
            'x': round(x), 'y': round(y), 'includeUserAgentShadowDOM': False})
        backend_id = node.get('backendNodeId')
        if not backend_id or hit.get('backendNodeId') != backend_id:
            raise GoogleTaskPending('Ô nhập đang bị phần tử khác che; chưa nhấp')
        stable = self.command('DOM.getBoxModel', {'nodeId': node_id})['model']['border']
        if len(stable) != 8 or any(type(b) not in {int, float} or not math.isfinite(b) or abs(a-b) > 1
                                   for a, b in zip(quad, stable)):
            raise GoogleTaskPending('Ô nhập đang di chuyển; chờ giao diện ổn định trước khi nhấp')
        point = {'x': x, 'y': y, 'button': 'left', 'clickCount': 1}
        self.command('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
        self.check_target()
        try:
            self.cdp.command('Input.dispatchMouseEvent', {'type': 'mousePressed', **point})
        finally:
            self.cdp.command('Input.dispatchMouseEvent', {'type': 'mouseReleased', **point})
        self.focused_node = (node_id, backend_id)
        self.check_focus()
