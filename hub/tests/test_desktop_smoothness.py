"""Native move/resize work stays off the UI path; no real windows are opened."""
import base64
import json
from pathlib import Path
from unittest.mock import Mock

from codex_bridge import desktop_window as desktop
from test_desktop_window import host
from test_desktop_lifecycle import timers
from test_reading_browser import page


def test_move_resize_burst_batches_disk_writes_and_retains_latest_geometry(host, timers, monkeypatch):
    write = Mock(wraps=desktop._atomic_json)
    monkeypatch.setattr(desktop, '_atomic_json', write)
    for value in range(100):
        host.on_move(100 + value, 80 + value)
        host.on_resize(1000 + value, 700 + value)
    assert len(timers) == 1
    write.assert_not_called()
    timers[0].callback()
    assert write.call_count == 1
    saved = json.loads((host.state_dir / 'window.json').read_text())
    assert (saved['x'], saved['y'], saved['width'], saved['height']) == (199, 179, 1099, 799)


def test_close_flushes_last_position_and_rejects_late_timer_or_move(host, timers, monkeypatch):
    write = Mock(wraps=desktop._atomic_json)
    monkeypatch.setattr(desktop, '_atomic_json', write)
    host.on_move(220, 130)
    host.close_children()
    assert write.call_count == 1
    timers[0].callback()
    host.on_move(999, 999)
    host.close_children()
    assert write.call_count == 1
    assert json.loads((host.state_dir / 'window.json').read_text())['x'] == 220


def test_maximize_persists_immediately_and_invalidates_old_resize_timer(host, timers, monkeypatch):
    write = Mock(wraps=desktop._atomic_json)
    monkeypatch.setattr(desktop, '_atomic_json', write)
    host.on_resize(1200, 840)
    host.save_geometry(maximized=True)
    assert write.call_count == 1
    timers[0].callback()
    host.on_resize(1800, 1100)
    assert write.call_count == 1
    saved = json.loads((host.state_dir / 'window.json').read_text())
    assert saved['maximized'] and saved['width'] == 1200


def test_unwritable_optional_geometry_and_log_do_not_break_window_close(host, timers, monkeypatch):
    monkeypatch.setattr(desktop, '_atomic_json', Mock(side_effect=PermissionError('locked state directory')))
    host.log.write = Mock(side_effect=PermissionError('locked log'))
    host.on_move(160, 100)
    timers[0].callback()
    host.close_children()
    assert host._closed.is_set()


def test_offline_brand_is_shipped_owl_within_local_document_limit(host):
    source = desktop.offline_page(connecting=False)
    expected = Path(desktop.__file__).with_name('assets').joinpath('codex-owl-logo-lite.png').read_bytes()
    assert 'data:image/png;base64,' + base64.b64encode(expected).decode() in source
    assert '>h·<' not in source and 'Georgia' not in source
    host._local_html = source
    host._local_page = True
    target = 'data:text/html;charset=utf-8;base64,' + base64.b64encode(source.encode()).decode()
    assert len(target) < 262144
    assert host.is_local_html_navigation(target)


def test_native_loading_brand_and_black_type_render_without_network(page):
    page.emulate_media(reduced_motion='reduce')
    page.set_content(desktop.offline_page())
    assert page.locator('.mark img').evaluate('image => image.complete && image.naturalWidth > 0')
    assert 'Microsoft YaHei UI' in page.locator('body').evaluate('node => getComputedStyle(node).fontFamily')
    assert page.locator('h1').evaluate('node => getComputedStyle(node).fontWeight') == '600'
    assert page.locator('.dot').evaluate('node => getComputedStyle(node).animationName') == 'none'
