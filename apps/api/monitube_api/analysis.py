"""Small deterministic summaries that do not require an LLM or external model."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from functools import lru_cache
import re
from typing import Iterable

from kiwipiepy import Kiwi

from .domain import CommentRecord, VideoRecord, utcnow


_QUESTION = re.compile(
    r"[?？]|(?:어떻게|왜|언제|어디|무엇|뭐|누구|몇|인가요|있나요|없나요|할까요|될까요)"
)
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
_CONTENT_NOUN_TAGS = frozenset({"NNG", "NNP"})


@lru_cache(maxsize=1)
def _korean_analyzer() -> Kiwi:
    """Load the Korean morphological analyzer once per API process."""

    return Kiwi()


def _content_word(token_form: str, token_tag: str) -> str | None:
    """Return a display lemma for content nouns and lexical verbs only."""

    if token_tag in _CONTENT_NOUN_TAGS:
        return token_form.lower()
    if token_tag.startswith("VV"):
        return f"{token_form}다"
    return None


def top_words_from_texts(texts: Iterable[str | None], *, limit: int = 10) -> list[dict[str, int | str]]:
    """Count Korean content nouns and lexical verb lemmas from public text."""

    counts: Counter[str] = Counter()
    normalized_texts = [
        normalized
        for text in texts
        if (normalized := (text or "").strip())
    ]
    for tokens in _korean_analyzer().tokenize(normalized_texts):
        for token in tokens:
            word = _content_word(token.form, token.tag)
            if word and word not in _STOP_WORDS:
                counts[word] += 1
    return [{"word": word, "count": count} for word, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def top_words(comments: Iterable[CommentRecord], *, limit: int = 10) -> list[dict[str, int | str]]:
    """Return Korean noun and verb frequencies from public comments."""

    return top_words_from_texts((comment.text_display for comment in comments), limit=limit)


def question_signals_from_texts(
    texts: Iterable[str | None],
) -> dict[str, int | float]:
    """Return a transparent, bounded heuristic for question-like comments."""

    sample_size = 0
    question_count = 0
    for text in texts:
        normalized = (text or "").strip()
        if not normalized:
            continue
        sample_size += 1
        question_count += bool(_QUESTION.search(normalized))
    return {
        "questionCount": question_count,
        "questionSampleSize": sample_size,
        "questionRate": (
            round(question_count / sample_size * 100, 2)
            if sample_size
            else 0
        ),
    }


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
