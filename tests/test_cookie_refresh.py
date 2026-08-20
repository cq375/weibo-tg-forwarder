import stat

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
