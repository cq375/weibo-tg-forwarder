from __future__ import annotations

import logging
import random
import signal
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import AppConfig
from .cookie_refresh import WeiboQrRefresher
from .media import MediaDownloader
from .models import WeiboPost
from .state import StateStore
from .telegram import TelegramClient
from .weibo import WeiboClient

LOGGER = logging.getLogger(__name__)
FETCH_HEALTH_KEY = "weibo_fetch"
COOKIE_REFRESH_KEY = "cookie_refresh"


class ForwardService:
    def __init__(
        self,
        config: AppConfig,
        weibo: WeiboClient,
        downloader: MediaDownloader,
        telegram: TelegramClient,
        state: StateStore,
        cookie_refresher: WeiboQrRefresher | None = None,
    ):
        self.config = config
        self.weibo = weibo
        self.downloader = downloader
        self.telegram = telegram
        self.state = state
        self.cookie_refresher = cookie_refresher
        self.stop_event = threading.Event()

    def run_once(self) -> tuple[int, int]:
        sent = 0
        failed = 0
        fetch_failures: dict[str, str] = {}
        for uid in self.config.weibo.uids:
            try:
                posts = self._collect_posts(uid)
            except Exception as exc:
                failed += 1
                fetch_failures[uid] = str(exc)
                LOGGER.exception("获取微博 UID %s 失败", uid)
                continue

            if not self.state.has_any(uid):
                if self.config.service.bootstrap_mode == "seed":
                    self.state.mark_seeded(posts)
                    LOGGER.info("UID %s 首次运行，已记录 %s 条现有微博，不转发历史内容", uid, len(posts))
                    continue
                posts = self._prepare_forward_latest(posts)

            pending = [
                post
                for post in posts
                if self.state.status(post.post_id) in {None, "failed", "processing"}
            ]
            for post in sorted(pending, key=self._post_sort_key):
                try:
                    self._process_post(post)
                    sent += 1
                except Exception as exc:
                    failed += 1
                    self.state.mark_failed(post.post_id, str(exc))
                    LOGGER.exception("微博 %s 转发失败，将在下次轮询重试", post.post_id)
        try:
            self._update_fetch_health(fetch_failures)
        except Exception:
            failed += 1
            LOGGER.exception("发送微博抓取健康状态通知失败，将在下轮重试")
        return sent, failed

    def _update_fetch_health(self, failures: dict[str, str]) -> None:
        if not failures:
            health = self.state.get_health_state(FETCH_HEALTH_KEY)
            if bool(health["alert_active"]):
                self.telegram.send_alert(
                    "微博监控恢复\n\n"
                    "所有目标账号已恢复正常抓取。\n"
                    f"恢复时间：{self._china_time()}"
                )
                LOGGER.info("已发送微博抓取恢复通知")
            self.state.reset_health(FETCH_HEALTH_KEY)
            return

        details = "\n".join(f"UID {uid}：{error}" for uid, error in failures.items())
        health = self.state.record_health_failure(FETCH_HEALTH_KEY, details)
        count = int(health["consecutive_failures"])
        threshold = self.config.service.alert_after_consecutive_failures
        if count < threshold:
            return

        if self._try_cookie_refresh(count, details):
            return
        health = self.state.get_health_state(FETCH_HEALTH_KEY)
        if not self._alert_due(health):
            return

        self.telegram.send_alert(
            "微博监控告警\n\n"
            f"连续 {count} 轮无法获取目标微博，Cookie 可能已经失效，"
            "也可能是微博风控或服务器网络异常。\n\n"
            f"{details}\n\n"
            f"告警时间：{self._china_time()}\n"
            "请登录服务器运行 doctor 检查。"
        )
        self.state.mark_health_alerted(FETCH_HEALTH_KEY)
        LOGGER.warning("已发送微博抓取异常告警，连续失败 %s 轮", count)

    def _try_cookie_refresh(self, count: int, details: str) -> bool:
        if not self.config.service.cookie_refresh_enabled or self.cookie_refresher is None:
            return False

        refresh_health = self.state.get_health_state(COOKIE_REFRESH_KEY)
        last_attempt_raw = refresh_health["last_alert_at"]
        if last_attempt_raw:
            last_attempt = datetime.fromisoformat(str(last_attempt_raw))
            elapsed = (datetime.now(timezone.utc) - last_attempt).total_seconds()
            if elapsed < self.config.service.cookie_refresh_cooldown_seconds:
                return False

        self.state.mark_health_alerted(COOKIE_REFRESH_KEY)
        self.state.mark_health_alerted(FETCH_HEALTH_KEY)
        LOGGER.warning("连续失败 %s 轮，启动微博扫码续期", count)
        try:
            cookie = self.cookie_refresher.refresh(
                lambda path: self.telegram.send_qr_code(
                    path,
                    "微博登录态需要续期\n\n"
                    "请在 5 分钟内使用微博 App 扫描二维码并确认登录。\n"
                    "二维码仅用于本次登录，请勿转发。",
                )
            )
            self.weibo.update_cookie(cookie)
            for uid in self.config.weibo.uids:
                self.weibo.validate_cookie(uid)
            self.cookie_refresher.persist(cookie)
            self.telegram.send_alert(
                "微博 Cookie 自动续期成功\n\n"
                f"已验证账号：{', '.join(self.config.weibo.uids)}\n"
                f"完成时间：{self._china_time()}\n"
                "监控已继续运行。"
            )
            self.state.reset_health(FETCH_HEALTH_KEY)
            self.state.reset_health(COOKIE_REFRESH_KEY)
            LOGGER.info("微博 Cookie 扫码续期成功")
            return True
        except Exception as exc:
            LOGGER.exception("微博 Cookie 扫码续期失败")
            try:
                self.telegram.send_alert(
                    "微博 Cookie 扫码续期失败\n\n"
                    f"原因：{exc}\n\n"
                    f"最近抓取错误：\n{details}\n\n"
                    "系统稍后会重新尝试，也可以在服务器手动运行 refresh-cookie。"
                )
            except Exception:
                LOGGER.exception("发送扫码续期失败通知时再次失败")
            return False

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
    def _china_time() -> str:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def _collect_posts(self, uid: str) -> list[WeiboPost]:
        posts: list[WeiboPost] = []
        seen_ids: set[str] = set()
        initialized = self.state.has_any(uid)

        for page in range(1, self.config.weibo.max_pages + 1):
            page_posts = self.weibo.fetch_page(uid, page)
            if not page_posts:
                break
            encountered_known = False
            for post in page_posts:
                if post.post_id in seen_ids:
                    continue
                seen_ids.add(post.post_id)
                posts.append(post)
                status = self.state.status(post.post_id)
                if status in {"sent", "seeded"}:
                    encountered_known = True
            if initialized and encountered_known:
                break
        else:
            if initialized and not any(self.state.contains(post.post_id) for post in posts):
                LOGGER.warning(
                    "UID %s 连续 %s 页均未遇到已知微博，停机期间可能发布过更多内容",
                    uid,
                    self.config.weibo.max_pages,
                )
        return posts

    def _prepare_forward_latest(self, posts: list[WeiboPost]) -> list[WeiboPost]:
        if not posts:
            return []
        latest = max(posts, key=self._post_sort_key)
        self.state.mark_seeded([post for post in posts if post.post_id != latest.post_id])
        return [latest]

    def _process_post(self, post: WeiboPost) -> None:
        self.state.mark_processing(post)
        LOGGER.info("处理新微博：@%s %s", post.author, post.url)
        media = self.downloader.download_post(post)
        message_ids = self.telegram.send_post(post, media)
        self.state.mark_sent(post.post_id, message_ids)
        LOGGER.info("微博 %s 已转发，共 %s 个 Telegram 消息", post.post_id, len(message_ids))
        if not self.config.storage.keep_media_after_send:
            self.downloader.cleanup_post(post)

    @staticmethod
    def _post_sort_key(post: WeiboPost) -> tuple[int, str]:
        numeric_id = int(post.post_id) if post.post_id.isdigit() else 0
        return numeric_id, post.post_id

    def run_forever(self) -> None:
        self._install_signal_handlers()
        LOGGER.info("开始监控 %s", ", ".join(self.config.weibo.uids))
        while not self.stop_event.is_set():
            sent, failed = self.run_once()
            LOGGER.info("本轮结束：成功转发 %s 条，失败 %s 项", sent, failed)
            base = self.config.weibo.poll_interval_seconds
            jitter = self.config.weibo.poll_jitter_seconds
            wait_seconds = max(15, base + random.randint(-jitter, jitter) if jitter else base)
            self.stop_event.wait(wait_seconds)
        LOGGER.info("服务已停止")

    def _install_signal_handlers(self) -> None:
        def stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
