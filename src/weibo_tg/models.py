from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class MediaItem:
    """一项待下载媒体，候选地址按清晰度从高到低排列。"""

    kind: str
    urls: tuple[str, ...]
    filename_hint: str
    order: int


@dataclass(frozen=True)
class RepostContent:
    author: str
    text: str


@dataclass
class WeiboPost:
    post_id: str
    uid: str
    author: str
    text: str
    created_at: datetime | None
    created_at_raw: str
    url: str
    media: list[MediaItem] = field(default_factory=list)
    repost: RepostContent | None = None


@dataclass
class DouyinPost:
    post_id: str
    uid: str
    sec_uid: str
    author: str
    text: str
    created_at: datetime | None
    created_at_raw: str
    url: str
    media: list[MediaItem] = field(default_factory=list)


Post = WeiboPost | DouyinPost


@dataclass(frozen=True)
class DownloadedMedia:
    kind: str
    path: Path
    order: int
    source_url: str
