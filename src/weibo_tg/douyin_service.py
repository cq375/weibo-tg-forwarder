from __future__ import annotations

import logging
import random
import signal
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import AppConfig
from .douyin import DouyinClient
from .media import MediaDownloader
from .models import DouyinPost
from .state import StateStore
from .telegram import TelegramClient, TelegramDeliveryUnconfirmed

LOGGER = logging.getLogger(__name__)
VIDEO_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


class DouyinForwardService:
    def __init__(
        self,
        config: AppConfig,
        douyin: DouyinClient,
        downloader: MediaDownloader,
        telegram: TelegramClient,
        state: StateStore,
    ):
        if config.douyin is None:
            raise ValueError("抖音监控配置未启用")
        self.config = config
        self.douyin = douyin
        self.downloader = downloader
        self.telegram = telegram
        self.state = state
        self.stop_event = threading.Event()
        for uid, _sec_uid in config.douyin.targets:
            self.state.get_health_state(f"douyin_fetch:{uid}")

    def run_once(self) -> tuple[int, int]:
        assert self.config.douyin is not None
        sent = 0
        failed = 0
        auth_modes: dict[str, int] = {}
        for uid, _sec_uid in self.config.douyin.targets:
            fetch_key = f"douyin_fetch:{uid}"
            try:
                posts = self._collect_posts(uid)
            except Exception as exc:
                failed += 1
                LOGGER.exception("获取抖音账号 %s 失败", uid)
                self._record_fetch_failure(uid, exc)
                continue
            auth_mode = self.douyin.last_auth_mode or "unknown"
            auth_modes[auth_mode] = auth_modes.get(auth_mode, 0) + 1

            if not self.state.has_any(uid):
                if self.config.service.bootstrap_mode == "seed":
                    self.state.mark_seeded(posts)
                    LOGGER.info(
                        "抖音账号 %s 首次运行，已记录 %s 条现有作品，不转发历史内容",
                        uid,
                        len(posts),
                    )
                    continue
                posts = self._prepare_forward_latest(posts)
            self._reset_health(fetch_key)

            pending = [
                post
                for post in posts
                if self.state.status(post.post_id) in {None, "failed", "processing"}
            ]
            for post in sorted(pending, key=self._post_sort_key):
                try:
                    self._process_post(post)
                    sent += 1
                except TelegramDeliveryUnconfirmed as exc:
                    failed += 1
                    self.state.mark_delivery_unconfirmed(post.post_id, str(exc))
                    LOGGER.exception(
                        "抖音 %s 的 Telegram 发送结果未知，已停止自动重试以避免重复转发",
                        post.post_id,
                    )
                except Exception as exc:
                    failed += 1
                    self.state.mark_failed(post.post_id, str(exc))
                    LOGGER.exception("抖音 %s 转发失败，将在下次轮询重试", post.post_id)
        if auth_modes:
            LOGGER.info("本轮抓取身份：%s", self._format_auth_modes(auth_modes))
        return sent, failed

    def _collect_posts(self, uid: str) -> list[DouyinPost]:
        assert self.config.douyin is not None
        posts: list[DouyinPost] = []
        seen: set[str] = set()
        initialized = self.state.has_any(uid)
        for page in range(1, self.config.douyin.max_pages + 1):
            page_posts = self.douyin.fetch_page(uid, page)
            if not page_posts:
                break
            encountered_known = False
            for post in page_posts:
                if post.post_id in seen:
                    continue
                seen.add(post.post_id)
                posts.append(post)
                if self.state.status(post.post_id) in {"sent", "seeded"}:
                    encountered_known = True
            if initialized and encountered_known:
                break
        return posts

    def _prepare_forward_latest(self, posts: list[DouyinPost]) -> list[DouyinPost]:
        if not posts:
            return []
        latest = max(posts, key=self._post_sort_key)
        self.state.mark_seeded([post for post in posts if post.post_id != latest.post_id])
        return [latest]

    def _process_post(self, post: DouyinPost) -> None:
        self.state.mark_processing(post)
        LOGGER.info("处理新抖音：@%s %s", post.author, post.url)
        set_referer = getattr(self.downloader, "set_referer", None)
        if callable(set_referer):
            for item in post.media:
                for url in item.urls:
                    set_referer(url, "https://www.douyin.com/")
        media = self.downloader.download_post(post)
        sendable = [
            item
            for item in media
            if item.path.stat().st_size <= VIDEO_UPLOAD_LIMIT_BYTES
        ]
        oversized = [
            item
            for item in media
            if item.path.stat().st_size > VIDEO_UPLOAD_LIMIT_BYTES
        ]
        message_ids = self.telegram.send_post(post, sendable)
        if oversized:
            lines = [
                "部分抖音媒体超过 Telegram Bot API 50MB 上传上限，未发送：",
                *[f"- {item.path.name}" for item in oversized],
                "",
                f"原文：{post.url}",
            ]
            message_ids.extend(
                self.telegram.send_message(
                    self.config.telegram.channel_id,
                    "\n".join(lines),
                )
            )
        self.state.mark_sent(post.post_id, message_ids)
        LOGGER.info(
            "抖音 %s 已转发，共 %s 个 Telegram 消息",
            post.post_id,
            len(message_ids),
        )
        if not self.config.storage.keep_media_after_send:
            # Keep oversized originals. They were intentionally not uploaded.
            if not oversized:
                self.downloader.cleanup_post(post)

    def _record_fetch_failure(self, uid: str, exc: Exception) -> None:
        assert self.config.douyin is not None
        key = f"douyin_fetch:{uid}"
        health = self.state.record_health_failure(key, str(exc))
        count = int(health["consecutive_failures"])
        threshold = self.config.service.alert_after_consecutive_failures
        if count < threshold or not self._alert_due(health):
            return
        try:
            self.telegram.send_alert(
                "抖音监控告警\n\n"
                f"账号 {uid} 连续 {count} 轮抓取失败。\n"
                f"错误：{str(exc)[:500]}\n\n"
                f"告警时间：{self._china_time()}\n"
                "请登录服务器运行 douyin-tg doctor 检查。"
            )
            self.state.mark_health_alerted(key)
            LOGGER.warning("已发送抖音抓取异常告警：%s，连续失败 %s 轮", uid, count)
        except Exception:
            LOGGER.exception("发送抖音抓取异常告警失败")

    def _format_auth_modes(self, modes: dict[str, int]) -> str:
        labels = {
            "cookie": "登录",
            "cookie_fallback": "登录失败后匿名",
            "anonymous": "匿名",
            "unknown": "未知",
        }
        text = "，".join(
            f"{labels.get(mode, mode)} {count} 个账号"
            for mode, count in sorted(modes.items())
        )
        if self.config.douyin and self.config.douyin.cookie:
            degraded = [
                count
                for mode, count in modes.items()
                if mode in {"cookie_fallback", "anonymous", "unknown"}
            ]
            if degraded:
                text += f"；警告：登录 Cookie 未用于 {sum(degraded)} 个账号"
        return text

    def _reset_health(self, key: str) -> None:
        self.state.get_health_state(key)
        self.state.reset_health(key)

    def _alert_due(self, health) -> bool:
        if not bool(health["alert_active"]):
            return True
        last_alert_raw = health["last_alert_at"]
        if not last_alert_raw:
            return True
        last_alert = datetime.fromisoformat(str(last_alert_raw))
        elapsed = (datetime.now(timezone.utc) - last_alert).total_seconds()
        return elapsed >= self.config.service.alert_repeat_seconds

    @staticmethod
    def _post_sort_key(post: DouyinPost) -> tuple[int, str]:
        if post.created_at is not None:
            return int(post.created_at.timestamp()), post.post_id
        numeric_id = int(post.post_id) if post.post_id.isdigit() else 0
        return numeric_id, post.post_id

    @staticmethod
    def _china_time() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def run_forever(self) -> None:
        self._install_signal_handlers()
        assert self.config.douyin is not None
        LOGGER.info("开始监控抖音 %s", ", ".join(self.douyin.uids))
        while not self.stop_event.is_set():
            sent, failed = self.run_once()
            LOGGER.info("本轮结束：成功转发 %s 条，失败 %s 项", sent, failed)
            if self.stop_event.is_set():
                break
            base = self.config.douyin.poll_interval_seconds
            jitter = self.config.douyin.poll_jitter_seconds
            wait_seconds = max(
                30,
                base + random.randint(-jitter, jitter) if jitter else base,
            )
            self.stop_event.wait(wait_seconds)
        LOGGER.info("抖音服务已停止")

    def _install_signal_handlers(self) -> None:
        def stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
