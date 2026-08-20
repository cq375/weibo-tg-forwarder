from datetime import datetime, timezone

from weibo_tg.models import WeiboPost
from weibo_tg.state import StateStore


def post(post_id: str = "500000000000001") -> WeiboPost:
    return WeiboPost(
        post_id=post_id,
        uid="1000000001",
        author="测试账号",
        text="测试",
        created_at=datetime.now(timezone.utc),
        created_at_raw="",
        url=f"https://weibo.com/1000000001/{post_id}",
    )


def test_state_lifecycle(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        item = post()
        assert not state.has_any(item.uid)
        state.mark_processing(item)
        assert state.status(item.post_id) == "processing"
        state.mark_failed(item.post_id, "网络错误")
        assert state.status(item.post_id) == "failed"
        state.mark_processing(item)
        state.mark_sent(item.post_id, [100, 101])
        assert state.status(item.post_id) == "sent"


def test_seeded_posts_are_known(tmp_path):
    with StateStore(tmp_path / "state.sqlite3") as state:
        item = post()
        state.mark_seeded([item])
        assert state.has_any(item.uid)
        assert state.contains(item.post_id)
        assert state.status(item.post_id) == "seeded"


def test_health_state_is_persistent(tmp_path):
    path = tmp_path / "state.sqlite3"
    with StateStore(path) as state:
        health = state.record_health_failure("weibo_fetch", "Cookie 失效")
        assert health["consecutive_failures"] == 1
        state.mark_health_alerted("weibo_fetch")

    with StateStore(path) as state:
        health = state.get_health_state("weibo_fetch")
        assert health["alert_active"] == 1
        state.reset_health("weibo_fetch")
        assert state.get_health_state("weibo_fetch")["consecutive_failures"] == 0
