from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


SENTENCE_SEGMENTER_VERSION = "sentence_segmenter_v1"

_CITATION_TOKEN_RE = re.compile(r"\[\[(?:cite_ref|cite):[^\]]+\]\]")
_CLOSING_PUNCTUATION = frozenset('"\'”’」』】）》〉')
_COMMON_PERIOD_ABBREVIATIONS = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "fig",
        "no",
        "dept",
        "inc",
        "ltd",
        "co",
    }
)


@dataclass(frozen=True)
class SentenceSpanV1:
    span_start: int
    span_end: int
    raw_text: str
    display_text: str

    def to_dict(self, *, sentence_index: int | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = asdict(self)
        if sentence_index is not None:
            payload["sentence_index"] = sentence_index
        # Additive compatibility projection for Review Draft v2 readers.
        payload["text"] = self.display_text
        return payload


def _next_non_whitespace(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _previous_ascii_word(text: str, period_index: int) -> str:
    match = re.search(r"([A-Za-z]+)$", text[:period_index])
    return match.group(1) if match else ""


def _is_period_boundary(text: str, index: int) -> bool:
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""

    if previous.isdigit() and following.isdigit():
        return False
    if following and not following.isspace() and following.isalnum():
        return False

    previous_word = _previous_ascii_word(text, index)
    normalized_word = previous_word.casefold()
    if normalized_word in _COMMON_PERIOD_ABBREVIATIONS:
        return False
    if len(previous_word) == 1 and previous_word.isupper():
        return False

    # Handle common dotted abbreviations without splitting on either dot.
    prefix = text[max(0, index - 4):index + 1].casefold()
    if prefix.endswith(("e.g.", "i.e.")):
        return False
    return True


def _citation_token_ending_at_or_after(text: str, start: int) -> re.Match[str] | None:
    return _CITATION_TOKEN_RE.match(text, start)


def _attach_trailing_citations(text: str, sentence_end: int) -> int:
    end = sentence_end
    cursor = sentence_end
    while True:
        token_start = _next_non_whitespace(text, cursor)
        token_match = _citation_token_ending_at_or_after(text, token_start)
        if token_match is None:
            break
        end = token_match.end()
        cursor = end
    return end


def _append_span(spans: List[SentenceSpanV1], text: str, start: int, end: int) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return
    raw_text = text[start:end]
    spans.append(
        SentenceSpanV1(
            span_start=start,
            span_end=end,
            raw_text=raw_text,
            display_text=raw_text.strip(),
        )
    )


def segment_sentences(block_text: str) -> List[SentenceSpanV1]:
    """Segment block-local text while preserving exact, patch-safe source spans."""

    text = block_text or ""
    spans: List[SentenceSpanV1] = []
    sentence_start = _next_non_whitespace(text, 0)
    index = sentence_start

    while index < len(text):
        citation_match = _citation_token_ending_at_or_after(text, index)
        if citation_match is not None:
            index = citation_match.end()
            continue

        character = text[index]
        is_boundary = character in "。！？!?"
        if character == ".":
            is_boundary = _is_period_boundary(text, index)
        if not is_boundary:
            index += 1
            continue

        sentence_end = index + 1
        while sentence_end < len(text) and text[sentence_end] in "。！？!?.":
            if text[sentence_end] == "." and not _is_period_boundary(text, sentence_end):
                break
            sentence_end += 1
        while sentence_end < len(text) and text[sentence_end] in _CLOSING_PUNCTUATION:
            sentence_end += 1
        sentence_end = _attach_trailing_citations(text, sentence_end)

        _append_span(spans, text, sentence_start, sentence_end)
        sentence_start = _next_non_whitespace(text, sentence_end)
        index = sentence_start

    _append_span(spans, text, sentence_start, len(text))
    return spans


def sentence_span_entries(block_text: str) -> List[Dict[str, Any]]:
    return [
        span.to_dict(sentence_index=sentence_index)
        for sentence_index, span in enumerate(segment_sentences(block_text), start=1)
    ]


def build_sentence_span_map(block_text: str) -> Dict[str, Any]:
    return {
        "segmenter_version": SENTENCE_SEGMENTER_VERSION,
        "sentences": sentence_span_entries(block_text),
    }
