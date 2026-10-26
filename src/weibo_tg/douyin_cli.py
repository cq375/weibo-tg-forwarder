from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .media import MediaDownloader
from .state import StateStore
from .telegram import TelegramClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监控抖音并转发到 Telegram 频道")
    parser.add_argument("--config", default="config.yml", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="启动抖音监控服务")
    run_parser.add_argument("--once", action="store_true", help="只执行一轮后退出")

    preview_parser = subparsers.add_parser("preview", help="抓取并下载最新抖音，不发送 Telegram")
    preview_parser.add_argument("--uid", action="append", help="只预览指定抖音号，可重复传入")
    preview_parser.add_argument("--limit", type=int, default=1, help="每个账号预览的作品数量")
    preview_parser.add_argument("--no-download", action="store_true", help="只解析，不下载媒体")

    send_parser = subparsers.add_parser("test-send", help="将指定账号的最新抖音发送到 Telegram")
    send_parser.add_argument("--id", dest="account_id", required=True, help="要测试的抖音号")
    send_parser.add_argument("--keep-media", action="store_true", help="发送后保留下载的媒体")

    resolve_parser = subparsers.add_parser("resolve", help="解析抖音号对应的 sec_uid")
    resolve_parser.add_argument("account_id", help="抖音号、主页链接或 sec_uid")

    subparsers.add_parser("doctor", help="检查抖音、Telegram 和本地存储配置")
    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _downloader(config):
    assert config.douyin is not None
    assert config.storage.douyin_media_directory is not None
    return MediaDownloader(
        config.storage.douyin_media_directory,
        config.storage.max_download_bytes,
        config.douyin.request_timeout_seconds,
    )


def _state_path(config) -> Path:
    path = config.storage.douyin_database
    if path is None:
        raise ConfigError("未配置 storage.douyin_database")
    return path


def _preview(config, args: argparse.Namespace) -> int:
    from .douyin import DouyinClient
    from .douyin_service import DouyinForwardService

    assert config.douyin is not None
    client = DouyinClient(config.douyin)
    downloader = _downloader(config)
    configured = {uid for uid, _sec_uid in config.douyin.targets}
    requested = tuple(args.uid or config.douyin.targets)
    requested_ids = tuple(
        item[0] if isinstance(item, tuple) else str(item) for item in requested
    )
    unknown = set(requested_ids) - configured
    if unknown:
        raise ConfigError(f"抖音号未在配置中：{', '.join(sorted(unknown))}")

    for account_id in requested_ids:
        posts = client.fetch_page(account_id, 1)
        posts = sorted(
            posts,
            key=DouyinForwardService._post_sort_key,
            reverse=True,
        )[: max(1, args.limit)]
        for post in posts:
            print(f"\n@{post.author} ({post.uid})")
            print(post.text or "（无文字内容）")
            print(f"\n媒体：{len(post.media)} 项")
            for item in post.media:
                print(f"- {item.kind}：{item.filename_hint}")
            print(f"原文：{post.url}")
            if not args.no_download:
                set_referer = getattr(downloader, "set_referer", None)
                if callable(set_referer):
                    for item in post.media:
                        for url in item.urls:
                            set_referer(url, "https://www.douyin.com/")
                files = downloader.download_post(post)
                for item in files:
                    print(f"已下载：{item.path}")
    return 0


def _doctor(config) -> int:
    from .douyin import DouyinClient

    assert config.douyin is not None
    account_ids = [uid for uid, _sec_uid in config.douyin.targets]
    print(f"监控抖音：{', '.join(account_ids)}")
    print(
        "抖音 Cookie："
        + ("已配置（登录优先，匿名兜底）" if config.douyin.cookie else "未配置（匿名访问）")
    )
    client = DouyinClient(config.douyin)
    failed = False
    for uid, sec_uid in config.douyin.targets:
        try:
            login_post_ids: set[str] = set()
            login_ok = False
            if config.douyin.cookie:
                try:
                    login_posts = client.fetch_page(uid, 1)
                    login_post_ids = {post.post_id for post in login_posts}
                    login_ok = True
                    print(f"抖音账号 {uid}：登录态正常，获取 {len(login_posts)} 条")
                except Exception as exc:
                    print(f"抖音账号 {uid}：登录态失败，尝试匿名检查，{exc}")
            posts = client.fetch_page(uid, 1)
            authors = {post.author for post in posts}
            print(
                f"抖音账号 {uid}：{client.last_auth_mode}，获取 {len(posts)} 条，"
                f"作者 {', '.join(sorted(authors)) or '无'}"
            )
            print(f"  sec_uid：{sec_uid[:20]}...（已配置）")
            if login_ok:
                try:
                    anonymous_posts = client.fetch_page_anonymous(uid, 1)
                    anonymous_ids = {post.post_id for post in anonymous_posts}
                    login_only = login_post_ids - anonymous_ids
                    anonymous_only = anonymous_ids - login_post_ids
                    print(
                        f"  可见范围：登录 {len(login_post_ids)} 条，"
                        f"匿名 {len(anonymous_ids)} 条，"
                        f"登录独有 {len(login_only)} 条，"
                        f"匿名独有 {len(anonymous_only)} 条"
                    )
                    if login_only:
                        print(f"  登录独有作品：{', '.join(sorted(login_only))}")
                    if anonymous_only:
                        print(f"  匿名独有作品：{', '.join(sorted(anonymous_only))}")
                except Exception as exc:
                    print(f"  匿名可见范围对比失败：{exc}")
        except Exception as exc:
            failed = True
            print(f"抖音账号 {uid}：失败，{exc}")

    state_path = _state_path(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with StateStore(state_path):
        print(f"SQLite：正常，{state_path}")

    if config.telegram.bot_token and config.telegram.channel_id:
        try:
            bot = TelegramClient(config.telegram).verify()
            print(f"Telegram：正常，Bot @{bot.get('username', 'unknown')}")
        except Exception as exc:
            failed = True
            print(f"Telegram：失败，{exc}")
    else:
        print("Telegram：未配置，跳过联网检查")
    return 1 if failed else 0


def _test_send(config, args: argparse.Namespace) -> int:
    from .douyin import DouyinClient
    from .douyin_service import DouyinForwardService

    assert config.douyin is not None
    configured = {uid for uid, _sec_uid in config.douyin.targets}
    if args.account_id not in configured:
        raise ConfigError(f"抖音号未在配置中：{args.account_id}")
    client = DouyinClient(config.douyin)
    posts = client.fetch_page(args.account_id, 1)
    if not posts:
        raise RuntimeError(f"抖音账号 {args.account_id} 没有可发送的内容")
    post = max(posts, key=DouyinForwardService._post_sort_key)
    downloader = _downloader(config)
    set_referer = getattr(downloader, "set_referer", None)
    if callable(set_referer):
        for item in post.media:
            for url in item.urls:
                set_referer(url, "https://www.douyin.com/")
    media = downloader.download_post(post)
    message_ids = TelegramClient(config.telegram).send_post(post, media)
    if not args.keep_media:
        downloader.cleanup_post(post)
    print(f"测试发送成功：抖音 {post.post_id}，Telegram 消息 {message_ids}")
    return 0


def _resolve(config, args: argparse.Namespace) -> int:
    from .douyin import DouyinClient

    assert config.douyin is not None
    sec_uid = DouyinClient(config.douyin).resolve_sec_uid(args.account_id)
    print(f"{args.account_id} -> {sec_uid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env")
    try:
        config = load_config(config_path)
        if config.douyin is None:
            raise ConfigError("配置中没有 douyin.targets")
        if args.command == "preview":
            return _preview(config, args)
        if args.command == "doctor":
            return _doctor(config)
        if args.command == "test-send":
            config.require_telegram()
            return _test_send(config, args)
        if args.command == "resolve":
            return _resolve(config, args)

        config.require_telegram()
        from .douyin import DouyinClient
        from .douyin_service import DouyinForwardService

        with StateStore(_state_path(config)) as state:
            service = DouyinForwardService(
                config,
                DouyinClient(config.douyin),
                _downloader(config),
                TelegramClient(config.telegram),
                state,
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
