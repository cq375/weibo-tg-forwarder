import time

from weibo_tg.config import WeiboConfig
from weibo_tg.weibo import WeiboClient, WeiboFetchError, html_to_text


def client() -> WeiboClient:
    return WeiboClient(
        WeiboConfig(
            uids=("1000000001",),
            cookie="",
            poll_interval_seconds=90,
            poll_jitter_seconds=20,
            anonymous_retry_seconds=600,
            max_pages=5,
            request_timeout_seconds=10,
        )
    )


def test_html_to_text_preserves_lines_and_emoji_alt():
    value = '<p>第一行<br>第二行<img alt="[笑]" src="x"></p>'
    assert html_to_text(value) == "第一行\n第二行[笑]"


def test_desktop_live_photo_downloads_still_and_motion_resource():
    mblog = {
        "idstr": "500000000000001",
        "mblogid": "LiveAbc",
        "created_at": "Tue Jul 14 18:00:00 +0800 2026",
        "text_raw": "一条 Live Photo 微博",
        "user": {"idstr": "1000000001", "screen_name": "测试账号"},
        "pic_ids": ["live-pic", "normal-pic"],
        "pic_infos": {
            "live-pic": {
                "type": "livephoto",
                "largest": {"url": "https://wx1.sinaimg.cn/large/live-pic.jpg"},
                "video": "https://f.video.weibocdn.com/live-pic.mov",
            },
            "normal-pic": {
                "type": "pic",
                "largest": {"url": "https://wx1.sinaimg.cn/large/normal-pic.jpg"},
            },
        },
    }

    post = client()._normalize_post(mblog, "1000000001")

    assert [item.kind for item in post.media] == ["image", "live_photo_video", "image"]
    assert post.media[0].urls[0].endswith("live-pic.jpg")
    assert post.media[1].urls[0].endswith("live-pic.mov")


def test_mobile_live_photo_list_is_matched_to_picture_id():
    mblog = {
        "idstr": "500000000000002",
        "text": "移动端 Live Photo",
        "user": {"id": 1000000002, "screen_name": "移动账号"},
        "pics": [
            {
                "pid": "mobile-live-pic",
                "large": {"url": "https://wx2.sinaimg.cn/large/mobile-live-pic.jpg"},
            }
        ],
        "live_photo": ["https://f.video.weibocdn.com/mobile-live-pic.mov"],
    }

    post = client()._normalize_post(mblog, "1000000002")

    assert [item.kind for item in post.media] == ["image", "live_photo_video"]
    assert post.media[1].filename_hint == "mobile-live-pic-live"


def test_repost_uses_original_media():
    mblog = {
        "idstr": "500000000000003",
        "text": "转发微博",
        "user": {"id": 1000000001, "screen_name": "转发账号"},
        "retweeted_status": {
            "idstr": "499999999999999",
            "text": "原微博",
            "user": {"id": 1, "screen_name": "原作者"},
            "pic_ids": ["origin-pic"],
            "pic_infos": {
                "origin-pic": {
                    "type": "pic",
                    "largest": {"url": "https://wx1.sinaimg.cn/large/origin-pic.jpg"},
                }
            },
        },
    }

    post = client()._normalize_post(mblog, "1000000001")

    assert post.repost is not None
    assert post.repost.author == "原作者"
    assert len(post.media) == 1


def test_selects_highest_video_candidate_first():
    entity = {
        "idstr": "500000000000004",
        "page_info": {
            "type": "video",
            "media_info": {
                "mp4_4k_mp4": "https://f.video.weibocdn.com/video-4k.mp4",
                "stream_url_hd": "https://f.video.weibocdn.com/video-hd.mp4",
            },
        },
    }

    media = client()._extract_video(entity)

    assert media is not None
    assert media.urls[0].endswith("video-4k.mp4")


def test_cookie_is_used_after_anonymous_sources_fail(monkeypatch):
    config = WeiboConfig(
        uids=("1000000001",),
        cookie="SUB=test",
        poll_interval_seconds=90,
        poll_jitter_seconds=20,
        anonymous_retry_seconds=600,
        max_pages=5,
        request_timeout_seconds=10,
    )
    weibo = WeiboClient(config)
    raw_post = {
        "idstr": "500000000000005",
        "text": "Cookie 回退成功",
        "user": {"id": 1000000001, "screen_name": "测试账号"},
    }

    def fake_mobile(session, _uid, _page):
        if session is weibo.anonymous:
            raise WeiboFetchError("匿名受限")
        return [raw_post]

    monkeypatch.setattr(weibo, "_fetch_mobile", fake_mobile)
    monkeypatch.setattr(
        weibo,
        "_fetch_desktop",
        lambda session, _uid, _page: [] if session is weibo.anonymous else [raw_post],
    )

    posts = weibo.fetch_page("1000000001", 1)

    assert [post.post_id for post in posts] == ["500000000000005"]


def test_short_text_with_long_text_flag_does_not_require_expansion():
    weibo = client()
    weibo._anonymous_blocked_until = time.monotonic() + 1000
    mblog = {
        "idstr": "500000000000006",
        "isLongText": True,
        "textLength": 8,
        "text_raw": "完整正文",
    }

    assert weibo._extract_full_text(mblog) == "完整正文"


def test_remembers_successful_cookie_desktop_source(monkeypatch):
    config = WeiboConfig(
        uids=("1000000001",),
        cookie="SUB=test",
        poll_interval_seconds=120,
        poll_jitter_seconds=60,
        anonymous_retry_seconds=86400,
        max_pages=5,
        request_timeout_seconds=10,
    )
    weibo = WeiboClient(config)
    weibo._anonymous_blocked_until = time.monotonic() + 1000
    calls = {"mobile": 0, "desktop": 0}
    raw_post = {
        "idstr": "500000000000007",
        "text": "桌面端成功",
        "user": {"id": 1000000001, "screen_name": "测试账号"},
    }

    def mobile(*_args):
        calls["mobile"] += 1
        raise WeiboFetchError("移动端失败")

    def desktop(*_args):
        calls["desktop"] += 1
        return [raw_post]

    monkeypatch.setattr(weibo, "_fetch_mobile", mobile)
    monkeypatch.setattr(weibo, "_fetch_desktop", desktop)

    weibo.fetch_page("1000000001", 1)
    weibo.fetch_page("1000000001", 1)

    assert calls == {"mobile": 1, "desktop": 2}
