import stat

import weibo_tg.cookie_refresh as cookie_refresh
from weibo_tg.cookie_refresh import WeiboQrRefresher


def test_formats_only_weibo_cookies_and_requires_sub():
    cookies = [
        {"domain": ".weibo.com", "name": "SUB", "value": "login"},
        {"domain": ".weibo.com", "name": "WBPSESS", "value": "session"},
        {"domain": "example.com", "name": "unrelated", "value": "secret"},
    ]

    value = WeiboQrRefresher._format_cookie(cookies)

    assert "SUB=login" in value
    assert "WBPSESS=session" in value
    assert "unrelated" not in value
    assert WeiboQrRefresher._format_cookie(cookies[1:]) == ""


def test_persists_cookie_with_private_permissions(tmp_path):
    path = tmp_path / "weibo_cookie.txt"
    refresher = WeiboQrRefresher(path, 300)

    refresher.persist("SUB=new-cookie")

    assert path.read_text(encoding="utf-8") == "SUB=new-cookie"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_launch_options_use_sandboxed_chrome_on_macos(tmp_path, monkeypatch):
    chrome = tmp_path / "Google Chrome"
    chrome.write_bytes(b"binary")
    monkeypatch.setattr(cookie_refresh.sys, "platform", "darwin")
    monkeypatch.setattr(cookie_refresh, "MACOS_CHROME", chrome)
    monkeypatch.setattr(WeiboQrRefresher, "_is_container", classmethod(lambda cls: False))
    monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)

    options = WeiboQrRefresher._launch_options()

    assert options["executable_path"] == str(chrome)
    assert options["chromium_sandbox"] is True
    assert "--no-sandbox" not in options["args"]
    assert "channel" not in options


def test_launch_options_keep_container_root_compatible(monkeypatch):
    monkeypatch.setattr(cookie_refresh.sys, "platform", "linux")
    monkeypatch.setattr(cookie_refresh.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)

    options = WeiboQrRefresher._launch_options()

    assert options["chromium_sandbox"] is False
    assert "--no-sandbox" in options["args"]
    assert "channel" not in options
    assert "executable_path" not in options


def test_launch_options_use_bundled_chromium_without_root(monkeypatch):
    monkeypatch.setattr(cookie_refresh.sys, "platform", "linux")
    monkeypatch.setattr(cookie_refresh.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(WeiboQrRefresher, "_is_container", classmethod(lambda cls: False))
    monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)

    options = WeiboQrRefresher._launch_options()

    assert options["chromium_sandbox"] is True
    assert "--no-sandbox" not in options["args"]
    assert "channel" not in options
    assert "executable_path" not in options


def test_launch_options_custom_executable_in_container_root(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"binary")
    monkeypatch.setattr(cookie_refresh.sys, "platform", "linux")
    monkeypatch.setattr(cookie_refresh.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", str(chrome))

    options = WeiboQrRefresher._launch_options()

    assert options["executable_path"] == str(chrome)
    assert options["chromium_sandbox"] is False
    assert "--no-sandbox" in options["args"]


def test_launch_options_custom_executable_in_container_non_root(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"binary")
    monkeypatch.setattr(cookie_refresh.sys, "platform", "linux")
    monkeypatch.setattr(cookie_refresh.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(WeiboQrRefresher, "_is_container", classmethod(lambda cls: True))
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", str(chrome))

    options = WeiboQrRefresher._launch_options()

    assert options["executable_path"] == str(chrome)
    assert options["chromium_sandbox"] is False
    assert "--no-sandbox" in options["args"]
