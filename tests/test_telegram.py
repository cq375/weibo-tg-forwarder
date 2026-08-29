from datetime import datetime

import pytest

from weibo_tg.config import TelegramConfig
from weibo_tg.models import RepostContent, WeiboPost
from weibo_tg.telegram import TelegramClient, TelegramError


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
