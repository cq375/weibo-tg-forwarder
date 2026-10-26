from __future__ import annotations

import logging
import queue
import random
import re
import signal
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import AppConfig
from .cookie_refresh import WeiboQrRefresher
from .media import MediaDownloader
from .models import WeiboPost
from .state import StateStore
from .telegram import TelegramClient, TelegramDeliveryUnconfirmed
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
        if config.weibo is None:
            raise ValueError("微博监控配置未启用")
        self.config = config
        self.weibo = weibo
        self.downloader = downloader
        self.telegram = telegram
        self.state = state
        self.cookie_refresher = cookie_refresher
        self.stop_event = threading.Event()
        self._command_event = threading.Event()
        self._refresh_requests: queue.Queue[str] = queue.Queue()
        self._refresh_lock = threading.Lock()
        self._refresh_active = threading.Event()
        self._command_state_lock = threading.Lock()
        self._manual_refresh_pending = False
        self._telegram_thread: threading.Thread | None = None

    def run_once(self) -> tuple[int, int]:
        sent = 0
        failed = 0
        fetch_failures: dict[str, str] = {}
        assert self.config.weibo is not None
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
                except TelegramDeliveryUnconfirmed as exc:
                    failed += 1
                    self.state.mark_delivery_unconfirmed(post.post_id, str(exc))
                    LOGGER.exception(
                        "微博 %s 的 Telegram 发送结果未知，已停止自动重试以避免重复转发",
                        post.post_id,
                    )
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

        with self._command_state_lock:
            if self._manual_refresh_pending:
                LOGGER.info("管理员扫码续期请求已排队，跳过本轮自动续期")
                return True
            if not self._refresh_lock.acquire(blocking=False):
                LOGGER.info("已有微博 Cookie 扫码续期正在进行，跳过本轮自动续期")
                return True
            self._refresh_active.set()
        try:
            refresh_health = self.state.get_health_state(COOKIE_REFRESH_KEY)
            last_attempt_raw = refresh_health["last_alert_at"]
            if last_attempt_raw:
                last_attempt = datetime.fromisoformat(str(last_attempt_raw))
                elapsed = (datetime.now(timezone.utc) - last_attempt).total_seconds()
                if elapsed < self.config.service.cookie_refresh_cooldown_seconds:
                    return False

            return self._perform_cookie_refresh(
                trigger="自动",
                details=details,
                count=count,
                admin_chat_id=None,
            )
        finally:
            self._refresh_active.clear()
            self._refresh_lock.release()

    def _perform_cookie_refresh(
        self,
        *,
        trigger: str,
        details: str,
        count: int | None,
        admin_chat_id: str | None,
    ) -> bool:
        """Run one QR refresh; the caller owns the refresh lock."""
        assert self.cookie_refresher is not None
        self.state.mark_health_alerted(COOKIE_REFRESH_KEY)
        if trigger == "自动":
            self.state.mark_health_alerted(FETCH_HEALTH_KEY)
        if count is None:
            LOGGER.info("管理员请求微博 Cookie 扫码续期")
        else:
            LOGGER.warning("连续失败 %s 轮，启动微博扫码续期", count)

        try:
            cookie = self.cookie_refresher.refresh(
                lambda path: self.telegram.send_qr_code(
                    path,
                    self._refresh_qr_caption(trigger),
                )
            )
            assert self.config.weibo is not None
            self.weibo.validate_cookie_candidate(cookie, self.config.weibo.uids)
            self.cookie_refresher.persist(cookie)
            self.weibo.update_cookie(cookie)
            self.state.reset_health(FETCH_HEALTH_KEY)
            self.state.reset_health(COOKIE_REFRESH_KEY)
            LOGGER.info("微博 Cookie 扫码续期成功（%s）", trigger)
        except Exception as exc:
            LOGGER.exception("微博 Cookie 扫码续期失败（%s）", trigger)
            self._notify_refresh_failure(
                trigger=trigger,
                error=exc,
                details=details,
                admin_chat_id=admin_chat_id,
            )
            return False

        success_text = (
            f"微博 Cookie {trigger}续期成功\n\n"
            f"已验证账号：{', '.join(self.config.weibo.uids)}\n"  # type: ignore[union-attr]
            f"完成时间：{self._china_time()}\n"
            "监控已立即使用新登录态，无需重启容器。"
        )
        if admin_chat_id:
            self._send_private(admin_chat_id, success_text)
            if self.config.telegram.notifications_use_channel:
                self._send_channel_result(success_text)
        else:
            self._send_notification_result(success_text)
        return True

    @staticmethod
    def _refresh_qr_caption(trigger: str) -> str:
        title = "微博登录态需要续期" if trigger == "自动" else "微博扫码续期"
        return (
            f"{title}\n\n"
            "请在 5 分钟内使用微博 App 扫描二维码并确认登录。\n"
            "二维码仅用于本次登录，请勿转发。"
        )

    def _notify_refresh_failure(
        self,
        *,
        trigger: str,
        error: Exception,
        details: str,
        admin_chat_id: str | None,
    ) -> None:
        error_text = self._redact_sensitive(str(error))
        if trigger == "自动":
            text = (
                "微博 Cookie 自动续期失败\n\n"
                f"原因：{error_text}\n\n"
                f"最近抓取错误：\n{self._redact_sensitive(details)}\n\n"
                "系统稍后会重新尝试，也可以在管理员私聊发送 /refresh_cookie。"
            )
            try:
                self.telegram.send_alert(text)
            except Exception:
                LOGGER.exception("发送扫码续期失败通知时再次失败")
            return

        text = (
            "微博 Cookie 手动扫码续期失败\n\n"
            f"原因：{error_text}\n\n"
            "请稍后在本 Bot 管理员私聊再次发送 /refresh_cookie。"
        )
        self._send_private(admin_chat_id, text)

    def _send_notification_result(self, text: str) -> None:
        try:
            self.telegram.send_alert(text)
        except Exception:
            LOGGER.exception("发送 Cookie 续期结果通知失败")

    def _send_channel_result(self, text: str) -> None:
        try:
            self.telegram.send_alert(text)
        except Exception:
            LOGGER.exception("发送 Cookie 续期频道通知失败")

    def _send_private(self, chat_id: str | None, text: str) -> None:
        if not chat_id:
            return
        try:
            self.telegram.send_message(chat_id, text)
        except Exception:
            LOGGER.exception("发送 Telegram 管理员私聊通知失败")

    @staticmethod
    def _redact_sensitive(value: str) -> str:
        # Keep operational errors useful while never echoing credential values.
        value = re.sub(
            r"(?i)\b(SUBP?|WBPSESS|XSRF-TOKEN|ALF|SSOLoginState)=[^;\s]+",
            r"\1=[已隐藏]",
            value,
        )
        value = re.sub(
            r"(?i)\b(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}\b",
            "bot[已隐藏]",
            value,
        )
        return value[:2000]

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

        for page in range(1, self.config.weibo.max_pages + 1):  # type: ignore[union-attr]
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
                    self.config.weibo.max_pages,  # type: ignore[union-attr]
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
        LOGGER.info("开始监控 %s", ", ".join(self.config.weibo.uids))  # type: ignore[union-attr]
        self._start_telegram_listener()
        try:
            while not self.stop_event.is_set():
                # Clear before processing so a command arriving during this
                # iteration cannot be lost when the wait begins.
                self._command_event.clear()
                self._process_manual_refresh_requests()
                if self.stop_event.is_set():
                    break

                sent, failed = self.run_once()
                LOGGER.info("本轮结束：成功转发 %s 条，失败 %s 项", sent, failed)
                self._process_manual_refresh_requests()
                if self.stop_event.is_set():
                    break

                base = self.config.weibo.poll_interval_seconds  # type: ignore[union-attr]
                jitter = self.config.weibo.poll_jitter_seconds  # type: ignore[union-attr]
                wait_seconds = max(15, base + random.randint(-jitter, jitter) if jitter else base)
                self._command_event.wait(wait_seconds)
        finally:
            self.stop_event.set()
            self._command_event.set()
            self._stop_telegram_listener()
            LOGGER.info("服务已停止")

    def _start_telegram_listener(self) -> None:
        if not self.config.service.cookie_refresh_enabled or self.cookie_refresher is None:
            LOGGER.info("Telegram 手动扫码命令未启用：Cookie 扫码续期已关闭")
            return
        if not self.config.telegram.alert_chat_id:
            LOGGER.warning(
                "未配置 TELEGRAM_ALERT_CHAT_ID，Telegram 手动扫码命令已关闭；"
                "自动扫码仍按通知配置运行"
            )
            return

        try:
            get_me = getattr(self.telegram, "get_me", None)
            if callable(get_me):
                get_me()
        except Exception as exc:
            LOGGER.warning("无法获取 Bot 用户名，仍接受不带 @Bot 后缀的管理员命令：%s", exc)

        self._telegram_thread = threading.Thread(
            target=self._telegram_command_loop,
            name="telegram-command-listener",
            daemon=True,
        )
        self._telegram_thread.start()
        LOGGER.info("Telegram 手动扫码命令监听已启动")

    def _stop_telegram_listener(self) -> None:
        thread = self._telegram_thread
        if thread is None:
            return
        thread.join(timeout=30)
        if thread.is_alive():
            LOGGER.warning("Telegram 命令监听线程未在停止期限内退出")
        self._telegram_thread = None

    def _telegram_command_loop(self) -> None:
        offset: int | None = None
        while not self.stop_event.is_set():
            try:
                offset = self._discard_pending_updates()
                break
            except Exception as exc:
                LOGGER.warning(
                    "清理 Telegram 启动前旧消息失败，15 秒后重试：%s",
                    self._redact_sensitive(str(exc)),
                )
                self.stop_event.wait(15)

        while not self.stop_event.is_set():
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=20, limit=100)
            except Exception as exc:
                LOGGER.warning(
                    "Telegram 命令监听失败，15 秒后重试：%s",
                    self._redact_sensitive(str(exc)),
                )
                self.stop_event.wait(15)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                self._handle_telegram_update(update)

    def _discard_pending_updates(self) -> int | None:
        """Advance past messages that existed before this service started."""
        offset: int | None = None
        while not self.stop_event.is_set():
            updates = self.telegram.get_updates(offset=offset, timeout=0, limit=100)
            if not updates:
                return offset
            update_ids = [
                item.get("update_id")
                for item in updates
                if isinstance(item.get("update_id"), int)
            ]
            if not update_ids:
                return offset
            offset = max(update_ids) + 1
            if len(updates) < 100:
                # One empty read closes the small race between draining and
                # entering long polling, while retaining the newest offset.
                continue
        return offset

    def _handle_telegram_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        configured_chat_id = self.config.telegram.alert_chat_id
        if chat_id is None or str(chat_id) != str(configured_chat_id):
            return

        bot_username = str(getattr(self.telegram, "bot_username", "") or "")
        if not TelegramClient.is_refresh_command(message.get("text"), bot_username):
            return
        self._queue_manual_refresh(str(chat_id))

    def _queue_manual_refresh(self, chat_id: str) -> None:
        with self._command_state_lock:
            busy = self._manual_refresh_pending or self._refresh_active.is_set()
            if not busy:
                self._manual_refresh_pending = True
                self._refresh_requests.put(chat_id)
                self._command_event.set()
        if busy:
            self._send_private(
                chat_id,
                "微博 Cookie 扫码续期正在进行，请等待当前二维码流程结束。",
            )
        else:
            destination = (
                "转发频道"
                if self.config.telegram.notifications_use_channel
                else "本私聊"
            )
            self._send_private(
                chat_id,
                f"已收到请求，正在生成微博登录二维码，请留意{destination}。",
            )

    def _process_manual_refresh_requests(self) -> None:
        while True:
            try:
                chat_id = self._refresh_requests.get_nowait()
            except queue.Empty:
                return

            if not self.config.service.cookie_refresh_enabled or self.cookie_refresher is None:
                with self._command_state_lock:
                    self._manual_refresh_pending = False
                self._send_private(chat_id, "微博 Cookie 扫码续期功能当前未启用。")
                continue

            with self._command_state_lock:
                self._manual_refresh_pending = False
                acquired = self._refresh_lock.acquire(blocking=False)
                if acquired:
                    self._refresh_active.set()
            if not acquired:
                self._send_private(
                    chat_id,
                    "微博 Cookie 扫码续期正在进行，请等待当前二维码流程结束。",
                )
                continue

            try:
                self._perform_cookie_refresh(
                    trigger="手动",
                    details="",
                    count=None,
                    admin_chat_id=chat_id,
                )
            finally:
                self._refresh_active.clear()
                self._refresh_lock.release()

    def _install_signal_handlers(self) -> None:
        def stop(_signum: int, _frame: object) -> None:
            self.stop_event.set()
            self._command_event.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
