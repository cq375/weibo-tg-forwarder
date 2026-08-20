from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .cookie_refresh import WeiboQrRefresher
from .media import MediaDownloader
from .service import ForwardService
from .state import StateStore
from .telegram import TelegramClient
from .weibo import WeiboClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监控微博并转发到 Telegram 频道")
    parser.add_argument("--config", default="config.yml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="启动监控服务")
    run_parser.add_argument("--once", action="store_true", help="只执行一轮后退出")

    preview_parser = subparsers.add_parser("preview", help="抓取并下载最新微博，不发送 Telegram")
    preview_parser.add_argument("--uid", action="append", help="只预览指定 UID，可重复传入")
    preview_parser.add_argument("--limit", type=int, default=1, help="每个账号预览的微博数量")
    preview_parser.add_argument("--no-download", action="store_true", help="只解析，不下载媒体")

    send_parser = subparsers.add_parser("test-send", help="将指定账号的最新微博发送到 Telegram")
    send_parser.add_argument("--uid", required=True, help="要测试的微博 UID")
    send_parser.add_argument("--keep-media", action="store_true", help="发送后保留下载的媒体")

    refresh_parser = subparsers.add_parser("refresh-cookie", help="发送二维码并扫码刷新微博 Cookie")
    refresh_parser.add_argument("--timeout", type=int, help="等待扫码的秒数")

    subparsers.add_parser("telegram-chats", help="列出最近与 Bot 对话的私聊 ID")

    subparsers.add_parser("doctor", help="检查微博、Telegram 和本地存储配置")
    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _downloader(config):
    return MediaDownloader(
        config.storage.media_directory,
        config.storage.max_download_bytes,
        config.weibo.request_timeout_seconds,
    )


def _preview(config, args: argparse.Namespace) -> int:
    client = WeiboClient(config.weibo)
    downloader = _downloader(config)
    requested = tuple(args.uid or config.weibo.uids)
    unknown = set(requested) - set(config.weibo.uids)
    if unknown:
        raise ConfigError(f"UID 未在配置中：{', '.join(sorted(unknown))}")

    for uid in requested:
        posts = client.fetch_page(uid, 1)
        posts = sorted(posts, key=ForwardService._post_sort_key, reverse=True)[: max(1, args.limit)]
        for post in posts:
            print(f"\n@{post.author} ({post.uid})")
            print(post.text or "（无文字内容）")
            if post.repost:
                print(f"\n转发自 @{post.repost.author}\n{post.repost.text}")
            print(f"\n媒体：{len(post.media)} 项")
            print(f"原文：{post.url}")
            if not args.no_download:
                files = downloader.download_post(post)
                for item in files:
                    print(f"已下载：{item.path}")
    return 0


def _doctor(config) -> int:
    print(f"监控账号：{', '.join(config.weibo.uids)}")
    print(f"微博 Cookie：{'已配置（匿名失败时启用）' if config.weibo.cookie else '未配置（仅匿名）'}")
    client = WeiboClient(config.weibo)
    failed = False
    for uid in config.weibo.uids:
        try:
            posts = client.fetch_page(uid, 1)
            print(f"微博 UID {uid}：正常，获取 {len(posts)} 条")
        except Exception as exc:
            failed = True
            print(f"微博 UID {uid}：失败，{exc}")

    config.storage.database.parent.mkdir(parents=True, exist_ok=True)
    with StateStore(config.storage.database):
        print(f"SQLite：正常，{config.storage.database}")

    if config.telegram.bot_token and config.telegram.channel_id:
        try:
            bot = TelegramClient(config.telegram).verify()
            print(f"Telegram：正常，Bot @{bot.get('username', 'unknown')}")
            if config.telegram.notifications_use_channel:
                print("Telegram 通知和登录二维码：与微博转发频道一致")
            else:
                print(
                    "Telegram 管理员私聊："
                    + (
                        "已配置"
                        if config.telegram.alert_chat_id
                        else "未配置，无法自动发送登录二维码"
                    )
                )
        except Exception as exc:
            failed = True
            print(f"Telegram：失败，{exc}")
    else:
        print("Telegram：未配置，跳过联网检查")
    return 1 if failed else 0


def _test_send(config, args: argparse.Namespace) -> int:
    if args.uid not in config.weibo.uids:
        raise ConfigError(f"UID 未在配置中：{args.uid}")
    config.require_telegram()
    client = WeiboClient(config.weibo)
    posts = client.fetch_page(args.uid, 1)
    if not posts:
        raise RuntimeError(f"微博 UID {args.uid} 没有可发送的内容")

    post = max(posts, key=ForwardService._post_sort_key)
    downloader = _downloader(config)
    media = downloader.download_post(post)
    message_ids = TelegramClient(config.telegram).send_post(post, media)
    if not args.keep_media:
        downloader.cleanup_post(post)
    print(f"测试发送成功：微博 {post.post_id}，Telegram 消息 {message_ids}")
    return 0


def _cookie_refresher(config, timeout: int | None = None) -> WeiboQrRefresher:
    if config.storage.cookie_file is None:
        raise ConfigError("未配置 storage.cookie_file")
    return WeiboQrRefresher(
        config.storage.cookie_file,
        max(60, timeout or config.service.cookie_refresh_timeout_seconds),
    )


def _refresh_cookie(config, args: argparse.Namespace) -> int:
    config.require_telegram()
    telegram = TelegramClient(config.telegram)
    refresher = _cookie_refresher(config, args.timeout)
    cookie = refresher.refresh(
        lambda path: telegram.send_qr_code(
            path,
            "微博扫码续期测试\n\n"
            "请使用微博 App 扫描二维码并确认登录。\n"
            "二维码仅用于本次登录，请勿转发。",
        )
    )
    weibo = WeiboClient(config.weibo)
    weibo.update_cookie(cookie)
    for uid in config.weibo.uids:
        count = weibo.validate_cookie(uid)
        print(f"微博 UID {uid}：新 Cookie 验证正常，获取 {count} 条")
    refresher.persist(cookie)
    telegram.send_alert("微博 Cookie 手动扫码续期成功，重启监控容器后生效。")
    print(f"新的 Cookie 已保存到 {config.storage.cookie_file}")
    return 0


def _telegram_chats(config) -> int:
    if not config.telegram.bot_token:
        raise ConfigError("缺少 TELEGRAM_BOT_TOKEN")
    chats = TelegramClient(config.telegram).list_private_chats()
    if not chats:
        print("未找到私聊。请先在 Telegram 中向 Bot 发送 /start，再重新运行此命令。")
        return 1
    for chat in chats:
        name = chat.get("username") or " ".join(
            str(chat.get(key) or "") for key in ("first_name", "last_name")
        ).strip()
        print(f"私聊 ID：{chat['id']}  用户：{name or '未知'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env")
    try:
        config = load_config(config_path)
        if args.command == "preview":
            return _preview(config, args)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "test-send":
            return _test_send(config, args)
        if args.command == "refresh-cookie":
            return _refresh_cookie(config, args)
        if args.command == "telegram-chats":
            return _telegram_chats(config)

        config.require_telegram()
        with StateStore(config.storage.database) as state:
            service = ForwardService(
                config,
                WeiboClient(config.weibo),
                _downloader(config),
                TelegramClient(config.telegram),
                state,
                _cookie_refresher(config) if config.service.cookie_refresh_enabled else None,
            )
            if args.once:
                sent, failed = service.run_once()
                print(f"执行完成：成功转发 {sent} 条，失败 {failed} 项")
                return 1 if failed else 0
            service.run_forever()
            return 0
    except (ConfigError, RuntimeError, KeyboardInterrupt) as exc:
        if str(exc):
            print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
