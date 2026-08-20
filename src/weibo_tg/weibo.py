from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import WeiboConfig
from .models import MediaItem, RepostContent, WeiboPost

LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class WeiboFetchError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")
        elif tag == "img":
            attributes = dict(attrs)
            self.parts.append(attributes.get("alt") or attributes.get("title") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\u200b", "")
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return parser.text()


def _new_session(cookie: str = "") -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
    )
    if cookie:
        session.headers["Cookie"] = cookie
        match = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
        if match:
            session.headers["x-xsrf-token"] = match.group(1)
    return session


class WeiboClient:
    def __init__(self, config: WeiboConfig):
        self.config = config
        self.anonymous = _new_session()
        self.authenticated = _new_session(config.cookie) if config.cookie else None
        self._anonymous_blocked_until = 0.0
        self._long_text_cache: dict[str, str] = {}
        self._preferred_authenticated_source: str | None = None

    def fetch_page(self, uid: str, page: int = 1) -> list[WeiboPost]:
        errors: list[str] = []
        now = time.monotonic()
        anonymous_attempted = False

        if now >= self._anonymous_blocked_until:
            anonymous_attempted = True
            for source, fetcher in (
                ("匿名移动端", self._fetch_mobile),
                ("匿名桌面端", self._fetch_desktop),
            ):
                try:
                    mblogs = fetcher(self.anonymous, uid, page)
                    if page == 1 and not mblogs:
                        raise WeiboFetchError("首页未返回微博，可能被匿名接口限流")
                    self._anonymous_blocked_until = 0.0
                    LOGGER.info("微博 UID %s 第 %s 页由%s获取，共 %s 条", uid, page, source, len(mblogs))
                    return [self._normalize_post(item, uid) for item in mblogs]
                except (WeiboFetchError, requests.RequestException) as exc:
                    errors.append(f"{source}: {exc}")
        else:
            remaining = int(self._anonymous_blocked_until - now)
            errors.append(f"匿名接口处于冷却期（剩余约 {remaining} 秒）")

        if anonymous_attempted and errors and self.config.anonymous_retry_seconds:
            self._anonymous_blocked_until = time.monotonic() + self.config.anonymous_retry_seconds

        if self.authenticated is not None:
            authenticated_sources = [
                ("Cookie 移动端", self._fetch_mobile),
                ("Cookie 桌面端", self._fetch_desktop),
            ]
            if self._preferred_authenticated_source:
                authenticated_sources.sort(
                    key=lambda item: item[0] != self._preferred_authenticated_source
                )
            for source, fetcher in authenticated_sources:
                try:
                    mblogs = fetcher(self.authenticated, uid, page)
                    if page == 1 and not mblogs:
                        raise WeiboFetchError("首页未返回微博，请检查 Cookie 或账号状态")
                    self._preferred_authenticated_source = source
                    LOGGER.info("微博 UID %s 第 %s 页由%s获取，共 %s 条", uid, page, source, len(mblogs))
                    return [self._normalize_post(item, uid) for item in mblogs]
                except (WeiboFetchError, requests.RequestException) as exc:
                    errors.append(f"{source}: {exc}")
        else:
            errors.append("未配置 WEIBO_COOKIE，无法执行登录态回退")

        raise WeiboFetchError("；".join(errors))

    def update_cookie(self, cookie: str) -> None:
        if not cookie.strip():
            raise WeiboFetchError("新的微博 Cookie 为空")
        self.authenticated = _new_session(cookie.strip())
        self._preferred_authenticated_source = "Cookie 桌面端"
        self._long_text_cache.clear()

    def validate_cookie(self, uid: str) -> int:
        if self.authenticated is None:
            raise WeiboFetchError("没有可验证的微博 Cookie")
        mblogs = self._fetch_desktop(self.authenticated, uid, 1)
        if not mblogs:
            raise WeiboFetchError(f"新 Cookie 无法获取 UID {uid} 的首页微博")
        return len(mblogs)

    def _fetch_mobile(
        self, session: requests.Session, uid: str, page: int
    ) -> list[dict[str, Any]]:
        url = "https://m.weibo.cn/api/container/getIndex"
        response = session.get(
            url,
            params={
                "type": "uid",
                "value": uid,
                "containerid": f"107603{uid}",
                "page": page,
            },
            headers={"Referer": f"https://m.weibo.cn/u/{uid}"},
            timeout=self.config.request_timeout_seconds,
        )
        payload = self._response_json(response, "移动端时间线")
        if payload.get("ok") != 1:
            raise WeiboFetchError(str(payload.get("msg") or payload.get("message") or "接口拒绝请求"))

        cards = payload.get("data", {}).get("cards", [])
        mblogs: list[dict[str, Any]] = []
        for card in cards if isinstance(cards, list) else []:
            if not isinstance(card, dict):
                continue
            candidates = [card]
            group = card.get("card_group")
            if isinstance(group, list):
                candidates.extend(item for item in group if isinstance(item, dict))
            for candidate in candidates:
                mblog = candidate.get("mblog")
                if candidate.get("card_type") == 9 and isinstance(mblog, dict):
                    mblogs.append(mblog)
        return self._deduplicate_mblogs(mblogs)

    def _fetch_desktop(
        self, session: requests.Session, uid: str, page: int
    ) -> list[dict[str, Any]]:
        response = session.get(
            "https://weibo.com/ajax/statuses/mymblog",
            params={"uid": uid, "page": page, "feature": 0},
            headers={"Referer": f"https://weibo.com/u/{uid}"},
            timeout=self.config.request_timeout_seconds,
        )
        payload = self._response_json(response, "桌面端时间线")
        data = payload.get("data", {})
        mblogs = data.get("list", []) if isinstance(data, dict) else []
        if not isinstance(mblogs, list):
            raise WeiboFetchError("返回结构中缺少微博列表")
        if payload.get("ok") == 0 and not mblogs:
            raise WeiboFetchError(str(payload.get("message") or "接口拒绝请求"))
        return self._deduplicate_mblogs([item for item in mblogs if isinstance(item, dict)])

    @staticmethod
    def _response_json(response: requests.Response, label: str) -> dict[str, Any]:
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("message") or body.get("error") or "")
            except (ValueError, AttributeError):
                detail = response.text[:120].strip()
            suffix = f"：{detail}" if detail else ""
            raise WeiboFetchError(f"{label} HTTP {response.status_code}{suffix}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeiboFetchError(f"{label}未返回 JSON，可能出现登录验证") from exc
        if not isinstance(payload, dict):
            raise WeiboFetchError(f"{label}返回结构异常")
        return payload

    @staticmethod
    def _deduplicate_mblogs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            post_id = str(item.get("idstr") or item.get("id") or "")
            if post_id and post_id not in seen:
                seen.add(post_id)
                result.append(item)
        return result

    def _normalize_post(self, mblog: dict[str, Any], target_uid: str) -> WeiboPost:
        post_id = str(mblog.get("idstr") or mblog.get("id") or "")
        if not post_id:
            raise WeiboFetchError("微博缺少 ID")

        user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
        uid = str(user.get("idstr") or user.get("id") or target_uid)
        author = str(user.get("screen_name") or uid)
        text = self._extract_full_text(mblog)
        bid = str(mblog.get("mblogid") or mblog.get("bid") or "")
        post_url = f"https://weibo.com/{uid}/{bid}" if bid else f"https://m.weibo.cn/status/{post_id}"

        repost = None
        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            repost_user = retweeted.get("user") if isinstance(retweeted.get("user"), dict) else {}
            repost = RepostContent(
                author=str(repost_user.get("screen_name") or "未知账号"),
                text=self._extract_full_text(retweeted),
            )

        created_raw = str(mblog.get("created_at") or "")
        return WeiboPost(
            post_id=post_id,
            uid=uid,
            author=author,
            text=text,
            created_at=self._parse_created_at(created_raw),
            created_at_raw=created_raw,
            url=post_url,
            media=self._extract_media(mblog),
            repost=repost,
        )

    def _extract_full_text(self, mblog: dict[str, Any]) -> str:
        initial = str(mblog.get("text_raw") or mblog.get("text") or "")
        initial_text = html_to_text(initial)
        if not mblog.get("isLongText"):
            return initial_text

        post_id = str(mblog.get("idstr") or mblog.get("id") or "")
        if not post_id:
            return initial_text
        if post_id in self._long_text_cache:
            return self._long_text_cache[post_id]

        expected_length = int(mblog.get("textLength") or 0)
        timeline_length = sum(1 if character.isascii() else 2 for character in initial_text)
        if expected_length and expected_length <= timeline_length:
            self._long_text_cache[post_id] = initial_text
            return initial_text

        user = mblog.get("user") if isinstance(mblog.get("user"), dict) else {}
        uid = str(user.get("idstr") or user.get("id") or "")
        bid = str(mblog.get("mblogid") or mblog.get("bid") or post_id)
        desktop_referer = f"https://weibo.com/{uid}/{bid}" if uid else "https://weibo.com/"
        mobile_referer = f"https://m.weibo.cn/status/{post_id}"

        sessions: list[requests.Session] = []
        if time.monotonic() >= self._anonymous_blocked_until:
            sessions.append(self.anonymous)
        if self.authenticated is not None:
            sessions.append(self.authenticated)

        for session in sessions:
            mobile_endpoint = (
                f"https://m.weibo.cn/statuses/extend?id={post_id}",
                mobile_referer,
            )
            desktop_endpoint = (
                f"https://weibo.com/ajax/statuses/longtext?id={post_id}",
                desktop_referer,
            )
            endpoints = (
                (desktop_endpoint, mobile_endpoint)
                if session is self.authenticated
                else (mobile_endpoint, desktop_endpoint)
            )
            for url, referer in endpoints:
                try:
                    response = session.get(
                        url,
                        headers={"Referer": referer},
                        timeout=self.config.request_timeout_seconds,
                    )
                    payload = self._response_json(response, "长微博正文")
                    data = payload.get("data", {})
                    if isinstance(data, dict):
                        full_text = (
                            data.get("longTextContent_raw")
                            or data.get("longTextContent")
                            or data.get("longText")
                        )
                        if full_text:
                            text = html_to_text(str(full_text))
                            self._long_text_cache[post_id] = text
                            return text
                except (requests.RequestException, WeiboFetchError):
                    continue

        LOGGER.warning("长微博 %s 无法展开，使用时间线中的摘要", post_id)
        return initial_text

    @staticmethod
    def _parse_created_at(value: str) -> datetime | None:
        for pattern in (
            "%a %b %d %H:%M:%S %z %Y",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(value, pattern)
            except ValueError:
                continue
        return None

    def _extract_media(self, mblog: dict[str, Any]) -> list[MediaItem]:
        result: list[MediaItem] = []
        seen: set[str] = set()

        def add(item: MediaItem) -> None:
            urls = tuple(url for url in item.urls if url and url not in seen)
            if not urls:
                return
            seen.update(urls)
            result.append(
                MediaItem(
                    kind=item.kind,
                    urls=urls,
                    filename_hint=item.filename_hint,
                    order=len(result),
                )
            )

        for entity in (mblog, mblog.get("retweeted_status")):
            if not isinstance(entity, dict):
                continue
            for item in self._extract_mixed_media(entity):
                add(item)
            for item in self._extract_pictures(entity):
                add(item)
            video = self._extract_video(entity)
            if video:
                add(video)

        return result

    @staticmethod
    def _extract_pictures(entity: dict[str, Any]) -> list[MediaItem]:
        pic_infos = entity.get("pic_infos") if isinstance(entity.get("pic_infos"), dict) else {}
        pics = entity.get("pics") if isinstance(entity.get("pics"), list) else []
        if not pics:
            pic_ids = entity.get("pic_ids") if isinstance(entity.get("pic_ids"), list) else []
            pics = [{"pid": pic_id} for pic_id in pic_ids]

        live_photo_raw = entity.get("live_photo")
        live_photo_urls: list[str] = []
        if isinstance(live_photo_raw, list):
            for value in live_photo_raw:
                if isinstance(value, str) and value.startswith("http"):
                    live_photo_urls.append(value)
                elif isinstance(value, dict):
                    url = value.get("url") or value.get("video")
                    if isinstance(url, str) and url.startswith("http"):
                        live_photo_urls.append(url)

        result: list[MediaItem] = []
        used_live_urls: set[str] = set()
        for index, pic in enumerate(pics):
            if not isinstance(pic, dict):
                continue
            pid = str(pic.get("pid") or pic.get("id") or f"image-{index + 1}")
            info = pic_infos.get(pid, {}) if isinstance(pic_infos, dict) else {}
            if not isinstance(info, dict):
                info = {}

            picture_type = next(
                (
                    str(value).lower()
                    for value in (pic.get("type"), info.get("type"), info.get("object_type"))
                    if value
                ),
                "pic",
            )
            is_live_photo = picture_type in {"livephoto", "live_photo"}
            is_gif = any(
                str(value).lower() == "gif"
                for value in (pic.get("type"), info.get("type"), info.get("object_type"))
            )

            candidates: list[str] = []
            for source in (info, pic):
                for key in ("largest", "original", "large", "bmiddle"):
                    variant = source.get(key)
                    if isinstance(variant, dict) and variant.get("url"):
                        candidates.append(str(variant["url"]))
                    elif isinstance(variant, str) and variant.startswith("http"):
                        candidates.append(variant)
                if source.get("url"):
                    candidates.append(str(source["url"]))

            if any(urlparse(url).path.lower().endswith(".gif") for url in candidates):
                is_gif = True

            if is_gif and pid:
                gif_candidates = [url for url in candidates if urlparse(url).path.lower().endswith(".gif")]
                if candidates:
                    parsed = urlparse(candidates[0])
                    if parsed.scheme and parsed.netloc:
                        gif_candidates.append(f"{parsed.scheme}://{parsed.netloc}/large/{pid}.gif")
                candidates = gif_candidates + candidates

            unique = tuple(dict.fromkeys(url for url in candidates if url.startswith("http")))
            if unique:
                result.append(
                    MediaItem(
                        kind="animation" if is_gif else "image",
                        urls=unique,
                        filename_hint=pid,
                        order=len(result),
                    )
                )

            live_candidates: list[str] = []
            for source in (info, pic):
                video = source.get("video")
                if isinstance(video, str) and video.startswith("http"):
                    live_candidates.append(video)
                elif isinstance(video, dict):
                    for key in ("url", "stream_url", "mp4_url", "mov_url"):
                        value = video.get(key)
                        if isinstance(value, str) and value.startswith("http"):
                            live_candidates.append(value)

            matching = [url for url in live_photo_urls if pid and pid in url]
            live_candidates.extend(matching)
            used_live_urls.update(matching)
            if is_live_photo and not live_candidates:
                unused = [url for url in live_photo_urls if url not in used_live_urls]
                if unused:
                    live_candidates.append(unused[0])
                    used_live_urls.add(unused[0])
            used_live_urls.update(url for url in live_candidates if url in live_photo_urls)

            live_unique = tuple(dict.fromkeys(live_candidates))
            if live_unique:
                result.append(
                    MediaItem(
                        kind="live_photo_video",
                        urls=live_unique,
                        filename_hint=f"{pid}-live",
                        order=len(result),
                    )
                )

        for index, url in enumerate(live_photo_urls):
            if url in used_live_urls:
                continue
            result.append(
                MediaItem(
                    kind="live_photo_video",
                    urls=(url,),
                    filename_hint=f"live-photo-{index + 1}",
                    order=len(result),
                )
            )
        return result

    @classmethod
    def _extract_mixed_media(cls, entity: dict[str, Any]) -> list[MediaItem]:
        mix = entity.get("mix_media_info")
        items = mix.get("items") if isinstance(mix, dict) else None
        if not isinstance(items, list):
            return []

        result: list[MediaItem] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
                continue
            data = item["data"]
            media_type = str(item.get("type") or data.get("type") or "").lower()
            if media_type in {"pic", "image", "livephoto", "live_photo"}:
                pic_id = str(data.get("pic_id") or data.get("pid") or f"mixed-{len(result) + 1}")
                synthetic = {
                    "pics": [{**data, "pid": pic_id, "type": data.get("type") or media_type}],
                    "pic_infos": {pic_id: data},
                }
                result.extend(cls._extract_pictures(synthetic))
            elif media_type == "video":
                video = cls._extract_video({"video_info": data.get("media_info") or data})
                if video:
                    result.append(video)
        return result

    @staticmethod
    def _extract_video(entity: dict[str, Any]) -> MediaItem | None:
        page_info = entity.get("page_info") if isinstance(entity.get("page_info"), dict) else {}
        media_info = page_info.get("media_info") if isinstance(page_info.get("media_info"), dict) else {}
        if not media_info and isinstance(entity.get("video_info"), dict):
            media_info = entity["video_info"]

        page_type = str(page_info.get("type") or page_info.get("object_type") or "").lower()
        if not media_info and "video" not in page_type:
            return None

        candidates: list[str] = []
        for key in (
            "mp4_8k_mp4",
            "mp4_4k_mp4",
            "mp4_2k_mp4",
            "mp4_1080p_mp4",
            "mp4_720p_mp4",
            "stream_url_hd",
            "mp4_hd_url",
            "stream_url",
            "mp4_sd_url",
            "stream_url_ld",
            "h5_url",
        ):
            value = media_info.get(key)
            if isinstance(value, str):
                candidates.append(value)

        playback = media_info.get("playback_list")
        if isinstance(playback, list):
            ranked: list[tuple[int, str]] = []
            for item in playback:
                if not isinstance(item, dict):
                    continue
                play_info = item.get("play_info") if isinstance(item.get("play_info"), dict) else item
                url = play_info.get("url") or play_info.get("stream_url")
                if not isinstance(url, str):
                    continue
                width = int(play_info.get("width") or 0)
                bitrate = int(play_info.get("bitrate") or 0)
                ranked.append((width * 1_000_000 + bitrate, url))
            candidates = [url for _, url in sorted(ranked, reverse=True)] + candidates

        for urls in (page_info.get("urls"), media_info.get("urls")):
            if isinstance(urls, dict):
                for value in urls.values():
                    if isinstance(value, str):
                        candidates.append(value)
                    elif isinstance(value, dict) and isinstance(value.get("url"), str):
                        candidates.append(value["url"])

        unique = tuple(
            dict.fromkeys(
                url
                for url in candidates
                if url.startswith("http") and ".m3u8" not in urlparse(url).path.lower()
            )
        )
        if not unique:
            return None
        post_id = str(entity.get("idstr") or entity.get("id") or "video")
        return MediaItem(kind="video", urls=unique, filename_hint=post_id, order=0)
