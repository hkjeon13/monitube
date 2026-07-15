"""Small deterministic summaries that do not require an LLM or external model."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import re
from typing import Iterable

from .domain import CommentRecord, VideoRecord, utcnow


_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "this",
        "that",
        "with",
        "for",
        "from",
        "are",
        "was",
        "have",
        "has",
        "you",
        "your",
        "영상",
        "정말",
        "너무",
        "합니다",
    }
)


def top_words(comments: Iterable[CommentRecord], *, limit: int = 10) -> list[dict[str, int | str]]:
    """Return stable, language-agnostic word counts from public comment text."""

    counts: Counter[str] = Counter()
    for comment in comments:
        for token in _WORD.findall(comment.text_display or ""):
            normalized = token.lower()
            if normalized not in _STOP_WORDS:
                counts[normalized] += 1
    return [{"word": word, "count": count} for word, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def build_summary(
    videos: Iterable[VideoRecord], comments: Iterable[CommentRecord], *, generated_at: datetime | None = None
) -> dict[str, object]:
    video_items = list(videos)
    comment_items = list(comments)
    published_videos = [video.published_at for video in video_items if video.published_at is not None]
    published_comments = [comment.published_at for comment in comment_items if comment.published_at is not None]
    return {
        "videoCount": len(video_items),
        "commentCount": len(comment_items),
        "latestVideoPublishedAt": max(published_videos) if published_videos else None,
        "latestCommentPublishedAt": max(published_comments) if published_comments else None,
        "topWords": top_words(comment_items),
        "generatedAt": generated_at or utcnow(),
    }
