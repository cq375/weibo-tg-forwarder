from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import DownloadedMedia, MediaItem, Post
from .weibo import USER_AGENT

LOGGER = logging.getLogger(__name__)


class MediaDownloadError(RuntimeError):
    pass


class MediaDownloader:
    def __init__(self, root: Path, max_download_bytes: int, timeout_seconds: int):
        self.root = root
        self.max_download_bytes = max_download_bytes
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.referers: dict[str, str] = {}

    def download_post(self, post: Post) -> list[DownloadedMedia]:
        post_dir = self.root / post.uid / post.post_id
        post_dir.mkdir(parents=True, exist_ok=True)
        downloaded = [self._download_item(post, item, post_dir) for item in post.media]
        return sorted(downloaded, key=lambda item: item.order)

    def _download_item(
        self, post: Post, item: MediaItem, post_dir: Path
    ) -> DownloadedMedia:
        prefix = f"{item.order + 1:02d}-{self._safe_name(item.filename_hint)}"
        existing = next(
            (path for path in sorted(post_dir.glob(f"{prefix}.*")) if not path.name.endswith(".part")),
            None,
        )
        if existing and existing.stat().st_size > 0:
            return DownloadedMedia(item.kind, existing, item.order, item.urls[0])

        failures: list[str] = []
        for url in item.urls:
            try:
                return self._download_url(post, item, prefix, post_dir, url)
            except (requests.RequestException, MediaDownloadError) as exc:
                failures.append(f"{url}: {exc}")
                LOGGER.warning("媒体候选地址下载失败：%s", exc)
        raise MediaDownloadError("；".join(failures))

    def _download_url(
        self,
        post: Post,
        item: MediaItem,
        prefix: str,
        post_dir: Path,
        url: str,
    ) -> DownloadedMedia:
        with self.session.get(
            url,
            headers={"Referer": self.referers.get(url, post.url)},
            stream=True,
            allow_redirects=True,
            timeout=self.timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                raise MediaDownloadError(f"HTTP {response.status_code}")

            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type in {"text/html", "application/json", "text/plain"}:
                raise MediaDownloadError(f"返回了非媒体内容 {content_type}")

            announced = int(response.headers.get("Content-Length") or 0)
            if announced > self.max_download_bytes:
                raise MediaDownloadError(
                    f"文件大小 {announced} 字节超过配置上限 {self.max_download_bytes} 字节"
                )

            extension = self._extension(url, content_type, item.kind)
            final_path = post_dir / f"{prefix}.{extension}"
            temporary_path = final_path.with_suffix(final_path.suffix + ".part")
            written = 0
            try:
                with temporary_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self.max_download_bytes:
                            raise MediaDownloadError(
                                f"下载超过配置上限 {self.max_download_bytes} 字节"
                            )
                        handle.write(chunk)
                if written == 0:
                    raise MediaDownloadError("下载结果为空")
                temporary_path.replace(final_path)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise

        LOGGER.info("已下载媒体 %s（%.2f MB）", final_path, written / 1024 / 1024)
        return DownloadedMedia(item.kind, final_path, item.order, url)

    @staticmethod
    def _safe_name(value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
        return sanitized[:80] or "media"

    @staticmethod
    def _extension(url: str, content_type: str, kind: str) -> str:
        content_extensions = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "video/mp4": "mp4",
            "video/quicktime": "mov",
            "application/octet-stream": "",
        }
        if content_extensions.get(content_type):
            return content_extensions[content_type]

        suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
        if suffix in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "m4v"}:
            return "jpg" if suffix == "jpeg" else suffix
        return {
            "image": "jpg",
            "animation": "gif",
            "video": "mp4",
            "live_photo_video": "mov",
        }.get(kind, "bin")

    def cleanup_post(self, post: Post) -> None:
        post_dir = self.root / post.uid / post.post_id
        if post_dir.exists():
            shutil.rmtree(post_dir)

    def set_referer(self, url: str, referer: str) -> None:
        """Attach a per-resource referer without changing the public Post model."""
        self.referers[url] = referer
