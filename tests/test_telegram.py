from datetime import datetime

import pytest
import requests

from weibo_tg.config import TelegramConfig
from weibo_tg.models import RepostContent, WeiboPost
from weibo_tg.telegram import (
    TelegramClient,
    TelegramDeliveryRetryable,
    TelegramDeliveryUnconfirmed,
    TelegramError,
)


def test_formats_repost_message():
    post = WeiboPost(
        post_id="500000000000001",
        uid="1000000001",
        author="账号名",
        text="转发说明",
        created_at=datetime(2026, 7, 14, 18, 0, 0),
        created_at_raw="",
        url="https://weibo.com/1000000001/abc",
        repost=RepostContent(author="原作者", text="原微博正文"),
    )

    text = TelegramClient._format_post(post)

    assert "微博更新：@账号名" in text
    assert "转发自：@原作者" in text
    assert text.endswith(post.url)


def test_long_text_is_split_within_telegram_limit():
    chunks = TelegramClient._split_text("内容" * 5000, limit=3900)
    assert len(chunks) > 1
    assert all(len(chunk) <= 3900 for chunk in chunks)


def test_qr_code_can_only_be_sent_to_private_admin_chat(tmp_path, monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30, "123")
    )
    path = tmp_path / "qr.png"
    path.write_bytes(b"png")

    def call(method, _data=None, _files=None):
        if method == "getChat":
            return {"id": 123, "type": "private"}
        return {"message_id": 9}

    monkeypatch.setattr(client, "_call", call)

    assert client.send_qr_code(path, "扫码") == [9]

    client.config = TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    with pytest.raises(TelegramError, match="TELEGRAM_ALERT_CHAT_ID"):
        client.send_qr_code(path, "扫码")


def test_channel_mode_sends_qr_and_alert_to_forward_channel(tmp_path, monkeypatch):
    client = TelegramClient(
        TelegramConfig(
            "token",
            "@forward_channel",
            "https://api.telegram.org",
            30,
            "123",
            True,
        )
    )
    path = tmp_path / "qr.png"
    path.write_bytes(b"png")
    calls = []

    def call(method, data=None, files=None):
        calls.append((method, data, files))
        return {"message_id": len(calls)}

    monkeypatch.setattr(client, "_call", call)

    client.send_qr_code(path, "扫码")
    client.send_alert("告警")

    assert calls[0][0] == "sendPhoto"
    assert calls[0][1]["chat_id"] == "@forward_channel"
    assert calls[1][0] == "sendMessage"
    assert calls[1][1]["chat_id"] == "@forward_channel"


def test_lists_private_bot_chats(monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    monkeypatch.setattr(
        client,
        "_call",
        lambda *_args, **_kwargs: [
            {"message": {"chat": {"id": 123, "type": "private", "username": "admin"}}},
            {"message": {"chat": {"id": -100, "type": "channel"}}},
        ],
    )

    assert client.list_private_chats() == [
        {"id": 123, "type": "private", "username": "admin"}
    ]


@pytest.mark.parametrize(
    "text",
    ["/refresh", "/refresh_cookie", "/refresh-cookie", "/REFRESH_COOKIE now"],
)
def test_accepts_refresh_command_aliases(text):
    assert TelegramClient.is_refresh_command(text)


def test_accepts_command_for_this_bot_only():
    assert TelegramClient.is_refresh_command("/refresh_cookie@taopli_bot", "taopli_bot")
    assert not TelegramClient.is_refresh_command("/refresh_cookie@other_bot", "taopli_bot")
    assert not TelegramClient.is_refresh_command("/refresh_cookie@taopli_bot", "")


@pytest.mark.parametrize("text", ["refresh_cookie", "/refresh-cookie@", "/refresh-cookie-now"])
def test_rejects_invalid_refresh_commands(text):
    assert not TelegramClient.is_refresh_command(text)


def test_get_updates_passes_offset_and_allowed_updates(monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    calls = []

    def call(method, data=None, files=None):
        calls.append((method, data, files))
        return [{"update_id": 8, "message": {}}]

    monkeypatch.setattr(client, "_call", call)

    assert client.get_updates(offset=9, timeout=20, limit=200) == [
        {"update_id": 8, "message": {}}
    ]
    assert calls == [
        (
            "getUpdates",
            {
                "offset": 9,
                "limit": 100,
                "timeout": 20,
                "allowed_updates": '["message"]',
            },
            None,
        )
    ]


def test_network_errors_do_not_expose_bot_token(monkeypatch):
    token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    client = TelegramClient(
        TelegramConfig(token, "@channel", "https://api.telegram.org", 30)
    )
    monkeypatch.setattr("weibo_tg.telegram.time.sleep", lambda _seconds: None)

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError(
            f"connection failed for https://api.telegram.org/bot{token}/sendMessage"
        )

    monkeypatch.setattr(client.session, "post", fail)

    calls = 0

    def counted_fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        return fail(*args, **kwargs)

    monkeypatch.setattr(client.session, "post", counted_fail)

    with pytest.raises(TelegramDeliveryUnconfirmed) as caught:
        client.send_message("123", "test")
    assert calls == 1
    assert token not in str(caught.value)
    assert "[已隐藏]" in str(caught.value)


def test_unconfirmed_photo_delivery_is_not_retried_as_document(tmp_path, monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"image")
    calls = []

    def call(method, _data=None, _files=None):
        calls.append(method)
        raise TelegramDeliveryUnconfirmed("sendPhoto timeout")

    monkeypatch.setattr(client, "_call", call)

    from weibo_tg.models import DownloadedMedia

    with pytest.raises(TelegramDeliveryUnconfirmed):
        client._send_images([DownloadedMedia("image", path, 0, "https://example/image")])
    assert calls == ["sendPhoto"]


def test_rate_limited_photo_is_not_retried_as_document(tmp_path, monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"image")
    calls = []

    def call(method, _data=None, _files=None):
        calls.append(method)
        raise TelegramDeliveryRetryable("sendPhoto rate limited")

    monkeypatch.setattr(client, "_call", call)

    from weibo_tg.models import DownloadedMedia

    with pytest.raises(TelegramDeliveryRetryable):
        client._send_images([DownloadedMedia("image", path, 0, "https://example/image")])
    assert calls == ["sendPhoto"]


def test_non_object_delivery_response_is_unconfirmed(tmp_path, monkeypatch):
    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"image")

    class Response:
        status_code = 200
        text = "[]"

        @staticmethod
        def json():
            return []

    monkeypatch.setattr(client.session, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(TelegramDeliveryUnconfirmed):
        client.send_message("123", "test")


def test_douyin_live_album_groups_stills_before_motion(tmp_path, monkeypatch):
    from weibo_tg.models import DownloadedMedia, DouyinPost

    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    post = DouyinPost(
        post_id="200",
        uid="account",
        sec_uid="sec",
        author="作者",
        text="Live 图集",
        created_at=None,
        created_at_raw="",
        url="https://www.douyin.com/video/200",
    )
    media = [
        DownloadedMedia("image", tmp_path / "01.jpg", 0, "https://example/1"),
        DownloadedMedia(
            "live_photo_video", tmp_path / "01-live.mp4", 1, "https://example/1v"
        ),
        DownloadedMedia("image", tmp_path / "02.jpg", 2, "https://example/2"),
        DownloadedMedia(
            "live_photo_video", tmp_path / "02-live.mp4", 3, "https://example/2v"
        ),
    ]
    for item in media:
        item.path.write_bytes(b"x")
    calls = []

    monkeypatch.setattr(client, "_call", lambda *_args, **_kwargs: {"message_id": 1})
    monkeypatch.setattr(
        client,
        "_send_images",
        lambda items: calls.append(("images", [item.order for item in items])) or [2],
    )
    monkeypatch.setattr(
        client,
        "_send_video",
        lambda item, caption=None: calls.append(
            ("video", item.order, caption)
        )
        or [3],
    )

    assert client.send_post(post, media) == [1, 2, 3, 3]
    assert calls == [
        ("images", [0, 2]),
        ("video", 1, "Live 动态图"),
        ("video", 3, "Live 动态图"),
    ]


def test_weibo_live_media_keeps_original_still_then_motion_order(
    tmp_path, monkeypatch
):
    from weibo_tg.models import DownloadedMedia, WeiboPost

    client = TelegramClient(
        TelegramConfig("token", "@channel", "https://api.telegram.org", 30)
    )
    post = WeiboPost(
        post_id="500",
        uid="account",
        author="作者",
        text="微博 Live",
        created_at=None,
        created_at_raw="",
        url="https://weibo.com/account/500",
    )
    media = [
        DownloadedMedia("image", tmp_path / "still.jpg", 0, "https://example/s"),
        DownloadedMedia(
            "live_photo_video", tmp_path / "live.mov", 1, "https://example/m"
        ),
        DownloadedMedia("image", tmp_path / "normal.jpg", 2, "https://example/n"),
    ]
    for item in media:
        item.path.write_bytes(b"x")
    calls = []

    monkeypatch.setattr(client, "_call", lambda *_args, **_kwargs: {"message_id": 1})
    monkeypatch.setattr(
        client,
        "_send_images",
        lambda items: calls.append(("images", [item.order for item in items])) or [2],
    )
    monkeypatch.setattr(
        client,
        "_send_video",
        lambda item, caption=None: calls.append(("video", item.order, caption))
        or [3],
    )

    assert client.send_post(post, media) == [1, 2, 3, 2]
    assert calls == [
        ("images", [0]),
        ("video", 1, "Live 动态图"),
        ("images", [2]),
    ]
