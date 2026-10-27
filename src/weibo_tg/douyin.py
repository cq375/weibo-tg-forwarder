from __future__ import annotations

import html
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DouyinConfig
from .models import DouyinPost, MediaItem

LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PLACEHOLDER_SEC_UID_PREFIX = "MS4wLjABAAAA"


class DouyinFetchError(RuntimeError):
    pass


class DouyinClient:
    """Small Douyin client using F2's request signer only.

    F2's full network handler is intentionally not used because its current
    httpx transport can receive empty Douyin responses. The signing, ttwid,
    and model pieces are still stable and Apache-2.0 licensed.

    A configured login cookie is preferred because Douyin can expose a
    different post set to logged-in and anonymous visitors. Anonymous access
    remains an automatic fallback when the login state is unavailable.
    """

    def __init__(self, config: DouyinConfig):
        try:
            from f2.apps.douyin.api import DouyinAPIEndpoints
            from f2.apps.douyin.model import UserPost
            from f2.apps.douyin.utils import (
                ABogusManager,
                GatewayHeaderManager,
                TokenManager,
            )
        except ImportError as exc:
            raise RuntimeError(
                "抖音功能缺少依赖，请安装：pip install -e '.[douyin]'"
            ) from exc

        self._user_post_type = UserPost
        self._endpoints = DouyinAPIEndpoints
        self._abogus_manager = ABogusManager
        self._gateway_header_manager = GatewayHeaderManager
        self._token_manager = TokenManager
        self.config = config
        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._sec_uid_cache: dict[str, str] = {
            uid: sec_uid
            for uid, sec_uid in config.targets
            if sec_uid.startswith(PLACEHOLDER_SEC_UID_PREFIX)
        }
        self._page_cursors: dict[tuple[str, int], int] = {
            (uid, 1): 0 for uid, _sec_uid in config.targets
        }
        self._anonymous_cookie_value = ""
        self._anonymous_blocked_until = 0.0
        self._login_retry_at = 0.0
        self._last_auth_mode = ""
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._request_gap_seconds = 1.2

    @property
    def uids(self) -> tuple[str, ...]:
        return tuple(uid for uid, _sec_uid in self.config.targets)

    @property
    def last_auth_mode(self) -> str:
        return self._last_auth_mode

    @last_auth_mode.setter
    def last_auth_mode(self, value: str) -> None:
        self._last_auth_mode = value

    def resolve_sec_uid(self, uid_or_url: str) -> str:
        """Resolve a numeric Douyin account, unique ID, or profile URL."""
        value = uid_or_url.strip()
        if value.startswith(PLACEHOLDER_SEC_UID_PREFIX):
            return value
        known = self._sec_uid_cache.get(value)
        if known:
            return known

        if value.startswith(("http://", "https://")):
            resolved = self._resolve_profile_url(value)
            if resolved:
                self._sec_uid_cache[value] = resolved
                return resolved

        # Douyin web profile pages include the sec_uid in the redirect URL.
        profile_url = f"https://www.douyin.com/user/{value}"
        resolved = self._resolve_profile_url(profile_url)
        if resolved:
            self._sec_uid_cache[value] = resolved
            return resolved

        # If the caller already has a sec_uid but it is not the normal
        # MS4w... shape, the profile endpoint is also a useful validator.
        response = self._request_profile(value)
        user = response.get("user") if isinstance(response, dict) else None
        sec_uid = str((user or {}).get("sec_uid", "")).strip()
        if sec_uid:
            self._sec_uid_cache[value] = sec_uid
            return sec_uid
        raise DouyinFetchError(f"无法解析抖音账号：{uid_or_url}")

    def fetch_page(self, uid: str, page: int) -> list[DouyinPost]:
        return self._fetch_page(uid, page, identity="primary")

    def fetch_page_anonymous(self, uid: str, page: int) -> list[DouyinPost]:
        """Fetch with a fresh anonymous visitor for visibility comparison."""
        if page < 1:
            return []
        return self._fetch_page(uid, page, identity="anonymous")

    def _fetch_page(
        self, uid: str, page: int, *, identity: str
    ) -> list[DouyinPost]:
        if page < 1:
            return []
        sec_uid = self.resolve_sec_uid(uid)
        cursor = self._page_cursor(uid, sec_uid, page)
        params = self._user_post_type(
            sec_user_id=sec_uid,
            max_cursor=cursor,
            count=20,
        )
        payload = self._get_signed_json(
            self._endpoints.USER_POST,
            params.model_dump(),
            identity=identity,
        )
        next_cursor = payload.get("max_cursor")
        if next_cursor is not None and payload.get("has_more"):
            self._page_cursors[(uid, page + 1)] = int(next_cursor)
        status = payload.get("status_code")
        if status not in (None, 0):
            raise DouyinFetchError(f"抖音接口 status_code={status}")
        awemes = payload.get("aweme_list") or []
        if not isinstance(awemes, list):
            raise DouyinFetchError("抖音接口返回的 aweme_list 结构异常")

        posts: list[DouyinPost] = []
        matched = 0
        for aweme in awemes:
            if not isinstance(aweme, dict):
                continue
            author = aweme.get("author") or {}
            author_sec_uid = str(author.get("sec_uid", "")).strip()
            if author_sec_uid != sec_uid:
                LOGGER.warning(
                    "丢弃非目标作者作品：目标 %s，作品 %s 来自 %s",
                    uid,
                    aweme.get("aweme_id", ""),
                    author_sec_uid or "unknown",
                )
                continue
            post = self._normalize_post(aweme, uid=uid, sec_uid=sec_uid)
            if post is not None:
                posts.append(post)
                matched += 1
        if awemes and matched == 0:
            raise DouyinFetchError(
                f"抖音账号 {uid} 返回了 {len(awemes)} 条作品，但作者均不匹配"
            )
        return posts

    def _page_cursor(self, uid: str, sec_uid: str, page: int) -> int:
        """Return the cursor for a page by walking the previous page.

        Douyin's cursor is a timestamp-derived opaque number, not arithmetic.
        The monitor only requests the first two pages; this helper is cached so
        preview/service code can still use the same page abstraction as Weibo.
        """
        cached = self._page_cursors.get((uid, page))
        if cached is not None:
            return cached
        cursor = 0
        for current_page in range(1, page):
            params = self._user_post_type(
                sec_user_id=sec_uid,
                max_cursor=cursor,
                count=20,
            )
            payload = self._get_signed_json(
                self._endpoints.USER_POST,
                params.model_dump(),
            )
            next_cursor = payload.get("max_cursor")
            if next_cursor is None or not payload.get("has_more"):
                return 0
            cursor = int(next_cursor)
            self._page_cursors[(uid, current_page + 1)] = cursor
        return cursor

    def _normalize_post(
        self, aweme: dict[str, Any], *, uid: str, sec_uid: str
    ) -> DouyinPost | None:
        post_id = str(aweme.get("aweme_id") or "").strip()
        if not post_id:
            return None
        author = aweme.get("author") or {}
        author_name = str(
            author.get("nickname")
            or author.get("unique_id")
            or author.get("short_id")
            or uid
        ).strip()
        created_at = self._timestamp(aweme.get("create_time"))
        text = html.unescape(
            str(aweme.get("desc") or aweme.get("caption") or "")
        ).strip()
        media: list[MediaItem] = []

        images = aweme.get("images")
        if isinstance(images, list) and images:
            order = 0
            for index, image in enumerate(images, start=1):
                if not isinstance(image, dict):
                    continue
                image_urls = self._image_urls(image)
                if image_urls:
                    media.append(
                        MediaItem(
                            kind="image",
                            urls=image_urls,
                            filename_hint=f"image-{index:02d}",
                            order=order,
                        )
                    )
                    order += 1
                live_video = image.get("video")
                live_urls = self._video_urls(live_video)
                if live_urls:
                    media.append(
                        MediaItem(
                            kind="live_photo_video",
                            urls=live_urls,
                            filename_hint=f"image-{index:02d}-live",
                            order=order,
                        )
                    )
                    order += 1
        else:
            video_urls = self._video_urls(aweme.get("video"))
            if video_urls:
                media.append(
                    MediaItem(
                        kind="video",
                        urls=video_urls,
                        filename_hint="video",
                        order=0,
                    )
                )

        if not media:
            LOGGER.warning("抖音作品 %s 没有可用媒体，仍转发文字", post_id)

        return DouyinPost(
            post_id=post_id,
            uid=uid,
            sec_uid=sec_uid,
            author=author_name,
            text=text,
            created_at=created_at,
            created_at_raw=str(aweme.get("create_time") or ""),
            url=f"https://www.douyin.com/video/{post_id}",
            media=media,
        )

    @staticmethod
    def _image_urls(image: dict[str, Any]) -> tuple[str, ...]:
        urls = [
            str(url).strip()
            for url in image.get("url_list") or []
            if isinstance(url, str) and url.strip()
        ]
        # Prefer actual JPEG over WebP for Telegram photo compatibility.
        jpeg = [url for url in urls if ".jpeg" in url.lower() or ".jpg" in url.lower()]
        webp = [url for url in urls if ".webp" in url.lower()]
        rest = [url for url in urls if url not in jpeg and url not in webp]
        return tuple(dict.fromkeys(jpeg + webp + rest))

    @staticmethod
    def _video_urls(video: Any) -> tuple[str, ...]:
        if not isinstance(video, dict):
            return ()
        candidates: list[tuple[int, int, int, list[str]]] = []
        for item in video.get("bit_rate") or []:
            if not isinstance(item, dict):
                continue
            play_addr = item.get("play_addr") or {}
            urls = [
                str(url).strip()
                for url in play_addr.get("url_list") or []
                if isinstance(url, str) and url.strip()
            ]
            if not urls:
                continue
            width = DouyinClient._to_int(play_addr.get("width"))
            height = DouyinClient._to_int(play_addr.get("height"))
            bitrate = DouyinClient._to_int(item.get("bit_rate"))
            h264_rank = 0 if not item.get("is_h265") else 1
            candidates.append((width * height, h264_rank, bitrate, urls))
        if candidates:
            # Highest resolution, H.264 before H.265, then highest bitrate.
            candidates.sort(
                key=lambda value: (value[0], -value[1], value[2]), reverse=True
            )
            return tuple(dict.fromkeys(candidates[0][3]))

        play_addr = video.get("play_addr") or {}
        urls = [
            str(url).strip()
            for url in play_addr.get("url_list") or []
            if isinstance(url, str) and url.strip()
        ]
        return tuple(dict.fromkeys(urls))

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _get_signed_json(
        self,
        base_endpoint: str,
        params: dict[str, Any],
        *,
        identity: str = "primary",
    ) -> dict[str, Any]:
        self._pace_request()
        if identity in {"primary", "anonymous"}:
            self._last_auth_mode = ""
        if (
            identity != "anonymous"
            and self.config.cookie
            and time.monotonic() >= self._login_retry_at
        ):
            try:
                payload = self._request_signed(
                    base_endpoint,
                    params,
                    cookie=self.config.cookie,
                )
                if identity == "primary":
                    self._last_auth_mode = "cookie"
                return payload
            except (DouyinFetchError, RuntimeError) as exc:
                LOGGER.warning(
                    "抖音登录 Cookie 请求失败，暂时改用匿名接口：%s",
                    exc,
                )
                self._login_retry_at = time.monotonic() + 600
        if time.monotonic() >= self._anonymous_blocked_until:
            try:
                payload = self._request_signed(
                    base_endpoint,
                    params,
                    cookie=self._anonymous_cookie(),
                )
                if identity == "primary":
                    self._last_auth_mode = "cookie_fallback" if self.config.cookie else "anonymous"
                return payload
            except (DouyinFetchError, RuntimeError) as exc:
                self._anonymous_blocked_until = time.monotonic() + 600
                if not self.config.cookie:
                    raise
        if (
            identity != "anonymous"
            and self.config.cookie
            and time.monotonic() >= self._login_retry_at
        ):
            payload = self._request_signed(
                base_endpoint,
                params,
                cookie=self.config.cookie,
            )
            if identity == "primary":
                self._last_auth_mode = "cookie"
            return payload
        raise DouyinFetchError(
            "抖音匿名接口处于冷却期，且登录 Cookie 未成功；"
            "请先检查服务器网络、出口 IP 和 Cookie 状态"
        )

    def _request_signed(
        self, base_endpoint: str, params: dict[str, Any], *, cookie: str
    ) -> dict[str, Any]:
        signed_url = self._abogus_manager.model_2_endpoint(
            USER_AGENT, base_endpoint, params
        )
        headers = self._gateway_header_manager.merge_headers(
            {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            },
            cookie,
        )
        headers["Cookie"] = cookie
        try:
            response = self.session.get(
                signed_url,
                headers=headers,
                timeout=self.config.request_timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise DouyinFetchError(f"抖音请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise DouyinFetchError(f"抖音 HTTP {response.status_code}")
        if not response.content:
            raise DouyinFetchError("抖音接口返回空内容")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DouyinFetchError("抖音接口未返回 JSON") from exc
        if not isinstance(payload, dict):
            raise DouyinFetchError("抖音接口返回结构异常")
        return payload

    def _request_profile(self, value: str) -> dict[str, Any]:
        from f2.apps.douyin.api import DouyinAPIEndpoints
        from f2.apps.douyin.model import UserProfile

        params = UserProfile(sec_user_id=value)
        return self._get_signed_json(
            DouyinAPIEndpoints.USER_DETAIL,
            params.model_dump(),
        )

    def _resolve_profile_url(self, url: str) -> str:
        try:
            response = self.session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"},
                timeout=self.config.request_timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException:
            return ""
        final_url = response.url
        match = re.search(r"/user/([^/?#]+)", final_url)
        if match:
            return match.group(1)
        match = re.search(r"[?&]sec_uid=([^&#]+)", final_url)
        return match.group(1) if match else ""

    def _anonymous_cookie(self) -> str:
        if not self._anonymous_cookie_value:
            self._anonymous_cookie_value = (
                f"ttwid={self._token_manager.gen_ttwid()};"
            )
        return self._anonymous_cookie_value

    def _pace_request(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._request_gap_seconds - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.25))
            self._last_request_at = time.monotonic()
