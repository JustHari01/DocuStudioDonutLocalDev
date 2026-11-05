from unittest.mock import Mock

import pytest

from docurandom.browser_input import BrowserInput
from docurandom.google_task_policy import GoogleBlocked, GoogleTaskPending


class Target:
    def __init__(self):
        self.calls = []
        self.nodes = [5]
        self.attrs = ['type', 'text']
        self.hit = 50
        self.focused = True
        self.box = [10, 20, 210, 20, 210, 60, 10, 60]

    def command(self, method, params):
        self.calls.append((method, params))
        if method == 'DOM.getDocument': return {'root': {'nodeId': 1}}
        if method == 'DOM.querySelectorAll': return {'nodeIds': self.nodes}
        if method == 'DOM.describeNode': return {'node': {'nodeName': 'INPUT', 'backendNodeId': 50, 'attributes': self.attrs}}
        if method == 'DOM.getBoxModel': return {'model': {'border': self.box}}
        if method == 'Page.getLayoutMetrics': return {'cssVisualViewport': {'clientWidth': 800, 'clientHeight': 600}}
        if method == 'DOM.getNodeForLocation': return {'backendNodeId': self.hit}
        if method == 'Accessibility.getPartialAXTree': return {'nodes': [{'backendDOMNodeId': 50, 'properties': [
            {'name': 'focused', 'value': {'value': self.focused}}]}]}
        return {}


def test_click_checks_unique_field_hit_and_focus_before_unicode_input():
    cdp = Target(); guard = Mock(); action = BrowserInput(cdp, guard)
    action.click_input('#title'); action.press('a', 'KeyA', 65, 2); action.insert_text('日本語 · Tiếng Việt')
    assert cdp.calls[-1] == ('Input.insertText', {'text': '日本語 · Tiếng Việt'})
    mouse = [p['type'] for m, p in cdp.calls if m == 'Input.dispatchMouseEvent']
    assert mouse == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert guard.call_count >= 10


@pytest.mark.parametrize('nodes', [[], [5, 6]])
def test_missing_or_ambiguous_input_never_clicks(nodes):
    cdp = Target(); cdp.nodes = nodes
    with pytest.raises(GoogleTaskPending): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m.startswith('Input.') for m, _ in cdp.calls)


@pytest.mark.parametrize('attrs', [['type', 'password'], ['type', 'file'], ['disabled', ''], ['readonly', '']])
def test_protected_or_disabled_fields_never_receive_input(attrs):
    cdp = Target(); cdp.attrs = attrs
    with pytest.raises((GoogleBlocked, GoogleTaskPending)): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m.startswith('Input.') for m, _ in cdp.calls)


def test_overlay_blocks_click_instead_of_clicking_foreign_element():
    cdp = Target(); cdp.hit = 999
    with pytest.raises(GoogleTaskPending, match='che'): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m.startswith('Input.') for m, _ in cdp.calls)


@pytest.mark.parametrize('box', [[0] * 8, [float('nan')] * 8, [900, 0, 950, 0, 950, 20, 900, 20]])
def test_hidden_invalid_or_offscreen_geometry_never_clicks(box):
    cdp = Target(); cdp.box = box
    with pytest.raises(GoogleTaskPending): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m.startswith('Input.') for m, _ in cdp.calls)


def test_failed_focus_is_not_reported_as_success():
    cdp = Target(); cdp.focused = False
    with pytest.raises(GoogleTaskPending, match='nhận bàn phím'): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m == 'Input.insertText' for m, _ in cdp.calls)


def test_focus_moved_after_click_prevents_keyboard_input():
    cdp = Target(); action = BrowserInput(cdp, Mock()); action.click_input('#title')
    cdp.focused = False
    with pytest.raises(GoogleTaskPending): action.insert_text('must not go to another field')
    with pytest.raises(GoogleTaskPending): action.press('a', 'KeyA', 65, 2)
    assert not any(m in {'Input.insertText', 'Input.dispatchKeyEvent'} for m, _ in cdp.calls)


def test_moving_input_is_not_clicked():
    cdp = Target(); original = cdp.command; reads = 0
    def command(method, params):
        nonlocal reads
        if method == 'DOM.getBoxModel':
            reads += 1
            if reads == 2: cdp.box = [v + 20 for v in cdp.box]
        return original(method, params)
    cdp.command = command
    with pytest.raises(GoogleTaskPending, match='di chuyển'): BrowserInput(cdp, Mock()).click_input('#title')
    assert not any(m.startswith('Input.') for m, _ in cdp.calls)


def test_key_release_happens_after_uncertain_keydown_reply():
    cdp = Target(); original = cdp.command
    def command(method, params):
        result = original(method, params)
        if method == 'Input.dispatchKeyEvent' and params['type'] == 'keyDown': raise TimeoutError('lost acknowledgement')
        return result
    cdp.command = command
    with pytest.raises(TimeoutError): BrowserInput(cdp, Mock()).press('Tab', 'Tab', 9)
    assert cdp.calls[-1][1]['type'] == 'keyUp'


def test_changed_target_guard_prevents_text_submission():
    cdp = Target(); guard = Mock(side_effect=GoogleBlocked('attention', 'different document'))
    with pytest.raises(GoogleBlocked): BrowserInput(cdp, guard).insert_text('must not type')
    assert cdp.calls == []


def test_failed_mousedown_still_releases_the_same_target_button():
    cdp = Target(); original = cdp.command
    def command(method, params):
        result = original(method, params)
        if method == 'Input.dispatchMouseEvent' and params['type'] == 'mousePressed': raise TimeoutError('lost acknowledgement')
        return result
    cdp.command = command
    with pytest.raises(TimeoutError): BrowserInput(cdp, Mock()).click_input('#title')
    assert cdp.calls[-1][1]['type'] == 'mouseReleased'
