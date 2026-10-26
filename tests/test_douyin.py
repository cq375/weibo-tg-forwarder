from datetime import datetime, timezone

from weibo_tg.config import DouyinConfig
from weibo_tg.douyin import DouyinClient
from weibo_tg.douyin_service import DouyinForwardService


def client(cookie: str = "") -> DouyinClient:
    return DouyinClient(
        DouyinConfig(
            targets=(("test-user", "MS4wLjABAAAAtarget"),),
            cookie=cookie,
            poll_interval_seconds=180,
            poll_jitter_seconds=60,
            max_pages=2,
            request_timeout_seconds=10,
        )
    )


def test_filters_non_target_author_and_normalizes_video(monkeypatch):
    target_sec = "MS4wLjABAAAAtarget"
    raw_posts = [
        {
            "aweme_id": "100",
            "desc": "目标视频",
            "create_time": 1_700_000_000,
            "author": {"sec_uid": target_sec, "nickname": "目标作者"},
            "video": {
                "bit_rate": [
                    {
                        "bit_rate": 900,
                        "play_addr": {
                            "width": 720,
                            "height": 1280,
                            "url_list": ["https://cdn/720.mp4"],
                        },
                    },
                    {
                        "bit_rate": 500,
                        "play_addr": {
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://cdn/1080.mp4"],
                        },
                    },
                ]
            },
        },
        {
            "aweme_id": "101",
            "author": {"sec_uid": "MS4wLjABAAAAother", "nickname": "混入作者"},
            "video": {"play_addr": {"url_list": ["https://cdn/other.mp4"]}},
        },
    ]
    douyin = client()
    monkeypatch.setattr(
        douyin,
        "_get_signed_json",
        lambda _endpoint, _params, **_kwargs: {
            "status_code": 0,
            "aweme_list": raw_posts,
        },
    )

    posts = douyin.fetch_page("test-user", 1)

    assert [post.post_id for post in posts] == ["100"]
    assert posts[0].media[0].kind == "video"
    assert posts[0].media[0].urls == ("https://cdn/1080.mp4",)
    assert posts[0].created_at == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
    )


def test_normalizes_image_album_and_live_video(monkeypatch):
    target_sec = "MS4wLjABAAAAtarget"
    raw = {
        "aweme_id": "200",
        "desc": "图集",
        "create_time": 1_700_000_001,
        "author": {"sec_uid": target_sec, "nickname": "目标作者"},
        "images": [
            {
                "url_list": [
                    "https://cdn/one.webp",
                    "https://cdn/one.jpeg",
                ],
                "video": {
                    "bit_rate": [
                        {
                            "is_h265": True,
                            "bit_rate": 2,
                            "play_addr": {
                                "width": 720,
                                "height": 1280,
                                "url_list": ["https://cdn/live-h265.mp4"],
                            },
                        },
                        {
                            "is_h265": False,
                            "bit_rate": 1,
                            "play_addr": {
                                "width": 720,
                                "height": 1280,
                                "url_list": ["https://cdn/live-h264.mp4"],
                            },
                        },
                    ]
                },
            },
            {"url_list": ["https://cdn/two.jpeg"]},
        ],
    }
    douyin = client()
    monkeypatch.setattr(
        douyin,
        "_get_signed_json",
        lambda _endpoint, _params, **_kwargs: {
            "status_code": 0,
            "aweme_list": [raw],
        },
    )

    post = douyin.fetch_page("test-user", 1)[0]

    assert [item.kind for item in post.media] == [
        "image",
        "live_photo_video",
        "image",
    ]
    assert post.media[0].urls[0] == "https://cdn/one.jpeg"
    assert post.media[1].urls[0] == "https://cdn/live-h264.mp4"
    assert post.media[2].filename_hint == "image-02"


def test_cookie_is_preferred_and_anonymous_is_fallback(monkeypatch):
    douyin = client(cookie="sessionid=secret")
    calls = []

    def request(_endpoint, _params, *, cookie):
        calls.append(cookie)
        return {"status_code": 0, "aweme_list": []}

    monkeypatch.setattr(douyin, "_request_signed", request)
    assert douyin._get_signed_json("https://example", {})["status_code"] == 0
    assert douyin.last_auth_mode == "cookie"
    assert calls == ["sessionid=secret"]

    calls.clear()
    douyin._login_retry_at = 0

    def blocked_login(_endpoint, _params, *, cookie):
        calls.append(cookie)
        if cookie == "sessionid=secret":
            raise RuntimeError("login expired")
        return {"status_code": 0, "aweme_list": []}

    monkeypatch.setattr(douyin, "_request_signed", blocked_login)
    assert douyin._get_signed_json("https://example", {})["status_code"] == 0
    assert douyin.last_auth_mode == "cookie_fallback"
    assert calls[0] == "sessionid=secret"
    assert calls[1].startswith("ttwid=")


def test_anonymous_identity_skips_login_cookie(monkeypatch):
    douyin = client(cookie="sessionid=secret")
    calls = []

    def request(_endpoint, _params, *, cookie):
        calls.append(cookie)
        return {"status_code": 0, "aweme_list": []}

    monkeypatch.setattr(douyin, "_request_signed", request)
    douyin._get_signed_json("https://example", {}, identity="anonymous")
    assert calls[0].startswith("ttwid=")
    assert all(cookie != "sessionid=secret" for cookie in calls)


def test_all_non_target_authors_are_a_fetch_failure(monkeypatch):
    douyin = client()
    monkeypatch.setattr(
        douyin,
        "_get_signed_json",
        lambda _endpoint, _params, **_kwargs: {
            "status_code": 0,
            "aweme_list": [
                {
                    "aweme_id": "300",
                    "author": {"sec_uid": "MS4wLjABAAAAother"},
                }
            ],
        },
    )

    import pytest
    from weibo_tg.douyin import DouyinFetchError

    with pytest.raises(DouyinFetchError, match="作者均不匹配"):
        douyin.fetch_page("test-user", 1)


def test_sort_key_is_chronological_for_initial_forward():
    old = type(
        "Post",
        (),
        {
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "post_id": "10",
        },
    )()
    new = type(
        "Post",
        (),
        {
            "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "post_id": "11",
        },
    )()

    assert sorted(
        [new, old], key=DouyinForwardService._post_sort_key
    ) == [old, new]
