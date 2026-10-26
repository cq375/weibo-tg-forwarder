from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WeiboConfig:
    uids: tuple[str, ...]
    cookie: str
    poll_interval_seconds: int
    poll_jitter_seconds: int
    anonymous_retry_seconds: int
    max_pages: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class DouyinConfig:
    targets: tuple[tuple[str, str], ...]
    cookie: str
    poll_interval_seconds: int
    poll_jitter_seconds: int
    max_pages: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    channel_id: str
    api_base: str
    request_timeout_seconds: int
    alert_chat_id: str = ""
    notifications_use_channel: bool = False


@dataclass(frozen=True)
class StorageConfig:
    database: Path
    media_directory: Path
    keep_media_after_send: bool
    max_download_bytes: int
    cookie_file: Path | None = None
    douyin_database: Path | None = None
    douyin_media_directory: Path | None = None
    douyin_cookie_file: Path | None = None


@dataclass(frozen=True)
class ServiceConfig:
    bootstrap_mode: str
    alert_after_consecutive_failures: int = 3
    alert_repeat_seconds: int = 21600
    cookie_refresh_enabled: bool = True
    cookie_refresh_timeout_seconds: int = 300
    cookie_refresh_cooldown_seconds: int = 21600


@dataclass(frozen=True)
class AppConfig:
    weibo: WeiboConfig | None
    telegram: TelegramConfig
    storage: StorageConfig
    service: ServiceConfig
    douyin: DouyinConfig | None = None

    def require_telegram(self) -> None:
        missing = []
        if not self.telegram.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram.channel_id:
            missing.append("TELEGRAM_CHANNEL_ID")
        if missing:
            raise ConfigError(f"缺少 Telegram 配置：{', '.join(missing)}")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 {name} 必须是对象")
    return value


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在：{config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")

    weibo = _section(raw, "weibo")
    telegram = _section(raw, "telegram")
    storage = _section(raw, "storage")
    service = _section(raw, "service")

    targets = weibo.get("targets", [])
    if not isinstance(targets, list):
        raise ConfigError("weibo.targets 必须是列表")
    uids = tuple(
        str(item.get("uid", "")).strip()
        for item in targets
        if isinstance(item, dict) and str(item.get("uid", "")).strip()
    )
    if any(not uid.isdigit() for uid in uids):
        raise ConfigError("微博 UID 只能包含数字")

    douyin = _section(raw, "douyin")
    douyin_targets_raw = douyin.get("targets", [])
    if not isinstance(douyin_targets_raw, list):
        raise ConfigError("douyin.targets 必须是列表")
    douyin_targets: list[tuple[str, str]] = []
    for item in douyin_targets_raw:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid", "")).strip()
        sec_uid = str(item.get("sec_uid", "")).strip()
        if not uid and not sec_uid:
            continue
        if not uid:
            raise ConfigError("douyin.targets 中每个账号都必须配置 uid")
        if not sec_uid:
            raise ConfigError(f"抖音账号 {uid} 缺少 sec_uid")
        douyin_targets.append((uid, sec_uid))
    if not uids and not douyin_targets:
        raise ConfigError("至少需要配置一个微博 UID 或抖音账号")

    bootstrap_mode = str(service.get("bootstrap_mode", "seed"))
    if bootstrap_mode not in {"seed", "forward_latest"}:
        raise ConfigError("service.bootstrap_mode 只能是 seed 或 forward_latest")

    base = config_path.parent
    max_megabytes = int(storage.get("max_download_megabytes", 2000))
    if max_megabytes <= 0:
        raise ConfigError("storage.max_download_megabytes 必须大于 0")

    database_path = _resolve_path(base, str(storage.get("database", "./data/state.sqlite3")))
    media_directory = _resolve_path(
        base, str(storage.get("media_directory", "./data/media"))
    )
    cookie_file = _resolve_path(base, str(storage.get("cookie_file", "./data/weibo_cookie.txt")))
    environment_cookie = os.getenv("WEIBO_COOKIE", str(weibo.get("cookie", ""))).strip()
    persisted_cookie = ""
    if cookie_file.exists():
        persisted_cookie = cookie_file.read_text(encoding="utf-8").strip()
    douyin_database_path = _resolve_path(
        base, str(storage.get("douyin_database", "./data/douyin-state.sqlite3"))
    )
    douyin_media_directory = _resolve_path(
        base, str(storage.get("douyin_media_directory", "./data/douyin-media"))
    )
    douyin_cookie_file = _resolve_path(
        base, str(storage.get("douyin_cookie_file", "./data/douyin_cookie.txt"))
    )
    environment_douyin_cookie = os.getenv(
        "DOUYIN_COOKIE", str(douyin.get("cookie", ""))
    ).strip()
    persisted_douyin_cookie = ""
    if douyin_cookie_file.exists():
        persisted_douyin_cookie = douyin_cookie_file.read_text(encoding="utf-8").strip()

    return AppConfig(
        weibo=(
            WeiboConfig(
                uids=uids,
                cookie=persisted_cookie or environment_cookie,
                poll_interval_seconds=max(
                    15, int(weibo.get("poll_interval_seconds", 120))
                ),
                poll_jitter_seconds=max(
                    0, int(weibo.get("poll_jitter_seconds", 60))
                ),
                anonymous_retry_seconds=max(
                    60, int(weibo.get("anonymous_retry_seconds", 86400))
                ),
                max_pages=max(1, int(weibo.get("max_pages", 5))),
                request_timeout_seconds=max(
                    5, int(weibo.get("request_timeout_seconds", 25))
                ),
            )
            if uids
            else None
        ),
        douyin=(
            DouyinConfig(
                targets=tuple(douyin_targets),
                cookie=persisted_douyin_cookie or environment_douyin_cookie,
                poll_interval_seconds=max(
                    30, int(douyin.get("poll_interval_seconds", 120))
                ),
                poll_jitter_seconds=max(
                    0, int(douyin.get("poll_jitter_seconds", 60))
                ),
                max_pages=max(1, int(douyin.get("max_pages", 2))),
                request_timeout_seconds=max(
                    5, int(douyin.get("request_timeout_seconds", 25))
                ),
            )
            if douyin_targets
            else None
        ),
        telegram=TelegramConfig(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", str(telegram.get("bot_token", ""))).strip(),
            channel_id=os.getenv("TELEGRAM_CHANNEL_ID", str(telegram.get("channel_id", ""))).strip(),
            api_base=str(telegram.get("api_base", "https://api.telegram.org")).rstrip("/"),
            request_timeout_seconds=max(10, int(telegram.get("request_timeout_seconds", 90))),
            alert_chat_id=os.getenv(
                "TELEGRAM_ALERT_CHAT_ID", str(telegram.get("alert_chat_id", ""))
            ).strip(),
            notifications_use_channel=bool(telegram.get("notifications_use_channel", False)),
        ),
        storage=StorageConfig(
            database=database_path,
            media_directory=media_directory,
            keep_media_after_send=bool(storage.get("keep_media_after_send", False)),
            max_download_bytes=max_megabytes * 1024 * 1024,
            cookie_file=cookie_file,
            douyin_database=douyin_database_path,
            douyin_media_directory=douyin_media_directory,
            douyin_cookie_file=douyin_cookie_file,
        ),
        service=ServiceConfig(
            bootstrap_mode=bootstrap_mode,
            alert_after_consecutive_failures=max(
                1, int(service.get("alert_after_consecutive_failures", 3))
            ),
            alert_repeat_seconds=max(300, int(service.get("alert_repeat_seconds", 21600))),
            cookie_refresh_enabled=bool(service.get("cookie_refresh_enabled", True)),
            cookie_refresh_timeout_seconds=max(
                60, int(service.get("cookie_refresh_timeout_seconds", 300))
            ),
            cookie_refresh_cooldown_seconds=max(
                300, int(service.get("cookie_refresh_cooldown_seconds", 21600))
            ),
        ),
    )
