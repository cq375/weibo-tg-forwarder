from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import TelegramConfig
from .models import DownloadedMedia, WeiboPost

LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.session = requests.Session()
        # Long polling may wait for up to 20 seconds. Keep it on a separate
        # session so a pending getUpdates call cannot delay a QR or alert send.
        self.poll_session = requests.Session()
        self._send_lock = threading.Lock()
        self.base_url = f"{config.api_base}/bot{config.bot_token}"
        self.bot_username = ""

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe")
        if not isinstance(result, dict):
            raise TelegramError("Telegram getMe 返回结构异常")
        username = result.get("username")
        if username:
            self.bot_username = str(username)
        return result

    def verify(self) -> dict[str, Any]:
        result = self.get_me()
        self._call("getChat", {"chat_id": self.config.channel_id})
        if self.config.alert_chat_id:
            alert_chat = self._call("getChat", {"chat_id": self.config.alert_chat_id})
            if not isinstance(alert_chat, dict) or alert_chat.get("type") != "private":
                raise TelegramError("TELEGRAM_ALERT_CHAT_ID 必须是管理员与 Bot 的私聊 ID")
        return result if isinstance(result, dict) else {}

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 25,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch message updates with a bounded long-poll timeout."""
        # Keep the HTTP timeout longer than Telegram's long-poll timeout. The
        # config has a minimum of 10 seconds, so this also works in tests and
        # small deployments with a short request timeout.
        poll_timeout = max(
            0,
            min(int(timeout), max(0, self.config.request_timeout_seconds - 5)),
        )
        data: dict[str, Any] = {
            "limit": max(1, min(int(limit), 100)),
            "timeout": poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            data["offset"] = int(offset)
        result = self._call("getUpdates", data)
        return (
            [item for item in result if isinstance(item, dict)]
            if isinstance(result, list)
            else []
        )

    def list_private_chats(self) -> list[dict[str, Any]]:
        result = self.get_updates(timeout=0, limit=100)
        chats: dict[str, dict[str, Any]] = {}
        for update in result:
            message = update.get("message") if isinstance(update, dict) else None
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and chat.get("type") == "private" and chat.get("id"):
                chats[str(chat["id"])] = chat
        return list(chats.values())

    def send_message(self, chat_id: str, text: str) -> list[int]:
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": json.dumps({"is_disabled": True}),
            },
        )
        return self._message_ids(result)

    def send_alert(self, text: str) -> list[int]:
        return self.send_message(self._notification_chat_id(), text)

    def send_qr_code(self, path: Path, caption: str) -> list[int]:
        target_chat_id = self._notification_chat_id(require_private_fallback=True)
        with path.open("rb") as handle:
            result = self._call(
                "sendPhoto",
                {"chat_id": target_chat_id, "caption": caption},
                {"photo": handle},
            )
        return self._message_ids(result)

    def _notification_chat_id(self, require_private_fallback: bool = False) -> str:
        if self.config.notifications_use_channel:
            return self.config.channel_id
        if not self.config.alert_chat_id:
            if require_private_fallback:
                raise TelegramError(
                    "未配置 TELEGRAM_ALERT_CHAT_ID；如需向转发频道发送二维码，"
                    "请启用 telegram.notifications_use_channel"
                )
            return self.config.channel_id
        if require_private_fallback:
            chat = self._call("getChat", {"chat_id": self.config.alert_chat_id})
            if not isinstance(chat, dict) or chat.get("type") != "private":
                raise TelegramError("TELEGRAM_ALERT_CHAT_ID 必须是管理员与 Bot 的私聊 ID")
        return self.config.alert_chat_id

    def send_post(
        self, post: WeiboPost, media: list[DownloadedMedia]
    ) -> list[int]:
        message_ids: list[int] = []
        for chunk in self._split_text(self._format_post(post)):
            result = self._call(
                "sendMessage",
                {
                    "chat_id": self.config.channel_id,
                    "text": chunk,
                    "link_preview_options": json.dumps({"is_disabled": True}),
                },
            )
            message_ids.extend(self._message_ids(result))

        index = 0
        while index < len(media):
            item = media[index]
            if item.kind == "image":
                group: list[DownloadedMedia] = []
                while index < len(media) and media[index].kind == "image" and len(group) < 10:
                    group.append(media[index])
                    index += 1
                message_ids.extend(self._send_images(group))
                continue
            if item.kind == "animation":
                message_ids.extend(self._send_animation(item))
            elif item.kind in {"video", "live_photo_video"}:
                message_ids.extend(self._send_video(item))
            else:
                message_ids.extend(self._send_document(item.path))
            index += 1
        return message_ids

    def _send_images(self, items: list[DownloadedMedia]) -> list[int]:
        if len(items) == 1:
            item = items[0]
            try:
                with item.path.open("rb") as handle:
                    result = self._call(
                        "sendPhoto",
                        {"chat_id": self.config.channel_id},
                        {"photo": handle},
                    )
                return self._message_ids(result)
            except TelegramError:
                LOGGER.warning("图片无法按照片发送，改为文件发送：%s", item.path)
                return self._send_document(item.path)

        try:
            with ExitStack() as stack:
                files: dict[str, Any] = {}
                media: list[dict[str, str]] = []
                for index, item in enumerate(items):
                    key = f"file{index}"
                    files[key] = stack.enter_context(item.path.open("rb"))
                    media.append({"type": "photo", "media": f"attach://{key}"})
                result = self._call(
                    "sendMediaGroup",
                    {"chat_id": self.config.channel_id, "media": json.dumps(media)},
                    files,
                )
            return self._message_ids(result)
        except TelegramError:
            LOGGER.warning("图片相册发送失败，改为逐个文件发送")
            message_ids: list[int] = []
            for item in items:
                message_ids.extend(self._send_document(item.path))
            return message_ids

    def _send_animation(self, item: DownloadedMedia) -> list[int]:
        try:
            with item.path.open("rb") as handle:
                result = self._call(
                    "sendAnimation",
                    {"chat_id": self.config.channel_id},
                    {"animation": handle},
                )
            return self._message_ids(result)
        except TelegramError:
            LOGGER.warning("动图无法按动画发送，改为文件发送：%s", item.path)
            return self._send_document(item.path)

    def _send_video(self, item: DownloadedMedia) -> list[int]:
        try:
            with item.path.open("rb") as handle:
                result = self._call(
                    "sendVideo",
                    {"chat_id": self.config.channel_id, "supports_streaming": "true"},
                    {"video": handle},
                )
            return self._message_ids(result)
        except TelegramError:
            LOGGER.warning("视频无法按流媒体发送，改为文件发送：%s", item.path)
            return self._send_document(item.path)

    def _send_document(self, path: Path) -> list[int]:
        with path.open("rb") as handle:
            result = self._call(
                "sendDocument",
                {"chat_id": self.config.channel_id},
                {"document": handle},
            )
        return self._message_ids(result)

    def _call(
        self,
        method: str,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        if method == "getUpdates":
            return self._request(method, data, files, self.poll_session)
        with self._send_lock:
            return self._request(method, data, files, self.session)

    def _request(
        self,
        method: str,
        data: dict[str, Any] | None,
        files: dict[str, Any] | None,
        request_session: requests.Session,
    ) -> Any:
        request_timeout = self.config.request_timeout_seconds
        if method == "getUpdates" and data is not None:
            request_timeout = min(
                request_timeout,
                max(5, int(data.get("timeout", 0)) + 5),
            )
        last_error = ""
        for attempt in range(1, 4):
            if files:
                for value in files.values():
                    if hasattr(value, "seek"):
                        value.seek(0)
            try:
                response = request_session.post(
                    f"{self.base_url}/{method}",
                    data=data,
                    files=files,
                    timeout=request_timeout,
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise TelegramError(f"Telegram {method} 返回了非 JSON 内容") from exc
                if response.status_code < 400 and payload.get("ok") is True:
                    return payload.get("result")
                description = payload.get("description") or response.text[:200]
                last_error = f"Telegram {method} HTTP {response.status_code}: {description}"
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                retry_after = payload.get("parameters", {}).get("retry_after", attempt)
                time.sleep(min(int(retry_after), 10))
            except requests.RequestException as exc:
                last_error = f"Telegram {method} 网络错误：{exc}"
                if attempt < 3:
                    time.sleep(attempt)
        raise TelegramError(last_error or f"Telegram {method} 请求失败")

    @staticmethod
    def _message_ids(result: Any) -> list[int]:
        values = result if isinstance(result, list) else [result]
        return [
            int(item["message_id"])
            for item in values
            if isinstance(item, dict) and "message_id" in item
        ]

    @staticmethod
    def _format_post(post: WeiboPost) -> str:
        lines = [f"微博更新：@{post.author}"]
        timestamp = TelegramClient._format_time(post.created_at, post.created_at_raw)
        if timestamp:
            lines.append(f"时间：{timestamp}")
        lines.extend(["", post.text or "（无文字内容）"])
        if post.repost:
            lines.extend(["", f"转发自：@{post.repost.author}", post.repost.text])
        lines.extend(["", f"原文：{post.url}"])
        return "\n".join(lines).strip()

    @staticmethod
    def _format_time(value: datetime | None, raw: str) -> str:
        if value is None:
            return raw
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _split_text(value: str, limit: int = 3900) -> list[str]:
        if len(value) <= limit:
            return [value]
        chunks: list[str] = []
        remaining = value
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            boundary = remaining.rfind("\n", 0, limit)
            if boundary < limit // 2:
                boundary = limit
            chunks.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
        return chunks

    @staticmethod
    def is_refresh_command(text: Any, bot_username: str = "") -> bool:
        """Return whether the first token is an accepted cookie-refresh command."""
        if not isinstance(text, str):
            return False
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return False
        token = parts[0]
        if not token.startswith("/"):
            return False
        command, separator, mention = token.partition("@")
        if command.casefold() not in {"/refresh-cookie", "/refresh_cookie", "/refresh"}:
            return False
        if separator and not mention:
            return False
        if separator and (
            not bot_username or mention.casefold() != bot_username.casefold()
        ):
            return False
        return True
