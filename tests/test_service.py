from weibo_tg.config import (
    AppConfig,
    ServiceConfig,
    StorageConfig,
    TelegramConfig,
    WeiboConfig,
)
from weibo_tg.models import WeiboPost
from weibo_tg.service import ForwardService
from weibo_tg.state import StateStore


def make_post(post_id: str) -> WeiboPost:
    return WeiboPost(
        post_id=post_id,
        uid="1000000001",
        author="测试账号",
        text=f"微博 {post_id}",
        created_at=None,
        created_at_raw="",
        url=f"https://m.weibo.cn/status/{post_id}",
    )


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        weibo=WeiboConfig(
            uids=("1000000001",),
            cookie="",
            poll_interval_seconds=90,
            poll_jitter_seconds=20,
            anonymous_retry_seconds=600,
            max_pages=2,
            request_timeout_seconds=10,
        ),
        telegram=TelegramConfig("token", "channel", "https://api.telegram.org", 30),
        storage=StorageConfig(tmp_path / "state.sqlite3", tmp_path / "media", False, 1024),
        service=ServiceConfig("seed"),
    )


class FakeWeibo:
    def __init__(self, posts):
        self.posts = posts

    def fetch_page(self, _uid, page):
        return self.posts if page == 1 else []


class FakeDownloader:
    def download_post(self, _post):
        return []

    def cleanup_post(self, _post):
        return None


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

    def send_qr_code(self, path, caption):
        self.alerts.append(f"二维码：{caption}")
        return [202]


class FailingWeibo:
    def fetch_page(self, _uid, _page):
        raise RuntimeError("Cookie 已失效")


class RefreshableWeibo(FailingWeibo):
    def __init__(self):
        self.cookie = ""

    def update_cookie(self, cookie):
        self.cookie = cookie

    def validate_cookie(self, _uid):
        return 20


class FakeRefresher:
    def __init__(self, path):
        self.path = path
        self.persisted = ""

    def refresh(self, on_qr_ready):
        self.path.write_bytes(b"qr")
        on_qr_ready(self.path)
        return "SUB=new-cookie"

    def persist(self, cookie):
        self.persisted = cookie


def test_first_run_seeds_then_only_sends_new_post(tmp_path):
    config = make_config(tmp_path)
    old_post = make_post("500000000000001")
    new_post = make_post("500000000000002")
    weibo = FakeWeibo([old_post])
    telegram = FakeTelegram()

    with StateStore(config.storage.database) as state:
        service = ForwardService(config, weibo, FakeDownloader(), telegram, state)
        assert service.run_once() == (0, 0)
        assert state.status(old_post.post_id) == "seeded"

        weibo.posts = [new_post, old_post]
        assert service.run_once() == (1, 0)
        assert telegram.sent == [new_post.post_id]
        assert state.status(new_post.post_id) == "sent"


def test_fetch_failures_alert_once_and_recovery_notifies(tmp_path):
    config = make_config(tmp_path)
    telegram = FakeTelegram()

    with StateStore(config.storage.database) as state:
        service = ForwardService(config, FailingWeibo(), FakeDownloader(), telegram, state)
        service.run_once()
        service.run_once()
        assert telegram.alerts == []

        service.run_once()
        assert len(telegram.alerts) == 1
        assert "连续 3 轮" in telegram.alerts[0]

        service.run_once()
        assert len(telegram.alerts) == 1

        service.weibo = FakeWeibo([])
        service.run_once()
        assert len(telegram.alerts) == 2
        assert "监控恢复" in telegram.alerts[1]


def test_fetch_failures_trigger_qr_cookie_refresh(tmp_path):
    config = make_config(tmp_path)
    telegram = FakeTelegram()
    weibo = RefreshableWeibo()
    refresher = FakeRefresher(tmp_path / "qr.png")

    with StateStore(config.storage.database) as state:
        service = ForwardService(
            config,
            weibo,
            FakeDownloader(),
            telegram,
            state,
            refresher,
        )
        service.run_once()
        service.run_once()
        service.run_once()

        assert any(message.startswith("二维码：") for message in telegram.alerts)
        assert any("自动续期成功" in message for message in telegram.alerts)
        assert weibo.cookie == "SUB=new-cookie"
        assert refresher.persisted == "SUB=new-cookie"
        assert state.get_health_state("weibo_fetch")["consecutive_failures"] == 0
