from datetime import datetime, timedelta, timezone

from weibo_tg.config import (
    AppConfig,
    DouyinConfig,
    ServiceConfig,
    StorageConfig,
    TelegramConfig,
)
from weibo_tg.douyin_service import DouyinForwardService
from weibo_tg.models import DouyinPost
from weibo_tg.state import StateStore
from weibo_tg.telegram import TelegramDeliveryUnconfirmed


def make_post(post_id: str, created_at: datetime) -> DouyinPost:
    return DouyinPost(
        post_id=post_id,
        uid="account",
        sec_uid="sec",
        author="作者",
        text=f"作品 {post_id}",
        created_at=created_at,
        created_at_raw="",
        url=f"https://www.douyin.com/video/{post_id}",
    )


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        weibo=None,
        telegram=TelegramConfig("token", "channel", "https://api.telegram.org", 30),
        storage=StorageConfig(
            tmp_path / "weibo.sqlite3",
            tmp_path / "media",
            False,
            1024,
        ),
        service=ServiceConfig("seed", alert_after_consecutive_failures=3),
        douyin=DouyinConfig(
            targets=(("account", "sec"),),
            cookie="",
            poll_interval_seconds=180,
            poll_jitter_seconds=60,
            max_pages=2,
            request_timeout_seconds=10,
        ),
    )


def replace_douyin_config(config: AppConfig, douyin: DouyinConfig) -> AppConfig:
    from dataclasses import replace

    return replace(config, douyin=douyin)


class FakeDouyin:
    def __init__(self, posts):
        self.posts = posts
        self.uids = ("account",)
        self.last_auth_mode = "anonymous"

    def fetch_page(self, _uid, page):
        return self.posts if page == 1 else []


class FailingDouyin(FakeDouyin):
    def __init__(self, posts):
        super().__init__(posts)
        self.last_auth_mode = ""

    def fetch_page(self, _uid, _page):
        raise RuntimeError("抖音风控")


class FakeDownloader:
    def download_post(self, _post):
        return []

    def cleanup_post(self, _post):
        pass


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.alerts = []

    def send_post(self, post, _media):
        self.sent.append(post.post_id)
        return [101]

    def send_alert(self, text):
        self.alerts.append(text)
        return [201]

    def send_message(self, _chat_id, _text):
        return [202]


def test_seed_then_only_send_new_in_chronological_order(tmp_path):
    config = make_config(tmp_path)
    now = datetime.now(timezone.utc)
    existing = make_post("10", now - timedelta(days=1))
    douyin = FakeDouyin([existing])
    telegram = FakeTelegram()

    with StateStore(tmp_path / "douyin.sqlite3") as state:
        service = DouyinForwardService(
            config, douyin, FakeDownloader(), telegram, state
        )
        assert service.run_once() == (0, 0)
        assert state.status(existing.post_id) == "seeded"

        newer = make_post("12", now)
        middle = make_post("11", now - timedelta(hours=1))
        douyin.posts = [newer, existing, middle]
        assert service.run_once() == (2, 0)

    assert telegram.sent == ["11", "12"]


def test_three_failures_alert_once(tmp_path):
    config = make_config(tmp_path)
    telegram = FakeTelegram()
    with StateStore(tmp_path / "douyin.sqlite3") as state:
        service = DouyinForwardService(
            config, FailingDouyin([]), FakeDownloader(), telegram, state
        )
        service.run_once()
        service.run_once()
        assert telegram.alerts == []
        service.run_once()
        assert len(telegram.alerts) == 1
        assert "连续 3 轮" in telegram.alerts[0]


def test_reports_cookie_fallback_when_login_is_configured(tmp_path):
    config = replace_douyin_config(
        make_config(tmp_path),
        DouyinConfig(
            targets=(("account", "sec"),),
            cookie="sessionid=secret",
            poll_interval_seconds=120,
            poll_jitter_seconds=60,
            max_pages=2,
            request_timeout_seconds=10,
        ),
    )
    douyin = FakeDouyin([])
    douyin.last_auth_mode = "cookie_fallback"
    with StateStore(tmp_path / "douyin.sqlite3") as state:
        service = DouyinForwardService(
            config, douyin, FakeDownloader(), FakeTelegram(), state
        )
        service.run_once()
        message = service._format_auth_modes({"cookie_fallback": 1})

    assert "登录失败后匿名 1 个账号" in message
    assert "登录 Cookie 未用于 1 个账号" in message


def test_unconfirmed_delivery_is_not_retried(tmp_path):
    config = make_config(tmp_path)
    post = make_post("99", datetime.now(timezone.utc))

    class Unconfirmed(FakeTelegram):
        def send_post(self, _post, _media):
            raise TelegramDeliveryUnconfirmed("timeout")

    telegram = Unconfirmed()
    with StateStore(tmp_path / "douyin.sqlite3") as state:
        state.mark_seeded([make_post("1", datetime.now(timezone.utc))])
        service = DouyinForwardService(
            config, FakeDouyin([post]), FakeDownloader(), telegram, state
        )
        assert service.run_once() == (0, 1)
        assert state.status(post.post_id) == "delivery_unconfirmed"
        assert service.run_once() == (0, 0)
