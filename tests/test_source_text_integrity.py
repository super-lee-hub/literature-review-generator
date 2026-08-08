from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "output",
    "tmp",
    "venv",
}
TEXT_EXTENSIONS = {
    ".bat",
    ".example",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
ROOT_TEXT_FILES = {"AGENTS.md", "README", "README.md"}
HIGH_CONFIDENCE_THRESHOLD = 100


def _make_common_mojibake(text: str) -> str:
    return text.encode("utf-8").decode("gb18030", errors="ignore")


KNOWN_MOJIBAKE_FRAGMENTS = {
    _make_common_mojibake("令牌桶状态"),
    _make_common_mojibake("AI返回非字典格式，尝试手动解析"),
}


def _is_han(character: str) -> bool:
    return "\u4e00" <= character <= "\u9fff"


def _iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS and path.name not in ROOT_TEXT_FILES:
            continue
        yield path


def _score_chinese_likeness(text: str, han_frequency: Counter[str]) -> int:
    return sum(han_frequency.get(character, 0) for character in text if _is_han(character))


def _find_high_confidence_mojibake_candidates() -> list[str]:
    texts_by_path: dict[Path, str] = {}
    han_frequency: Counter[str] = Counter()

    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8")
        texts_by_path[path] = text
        han_frequency.update(character for character in text if _is_han(character))

    findings: list[str] = []
    for path, text in texts_by_path.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not any(_is_han(character) for character in line):
                continue

            best_match: tuple[int, str, str, str] | None = None
            for encoding in ("gb18030", "gbk"):
                for error_mode in ("strict", "ignore"):
                    try:
                        repaired = line.encode(encoding, errors=error_mode).decode("utf-8", errors=error_mode)
                    except UnicodeError:
                        continue
                    if repaired == line or not any(_is_han(character) for character in repaired):
                        continue

                    score_delta = _score_chinese_likeness(repaired, han_frequency) - _score_chinese_likeness(
                        line, han_frequency
                    )
                    if score_delta <= 0:
                        continue
                    if best_match is None or score_delta > best_match[0]:
                        best_match = (score_delta, encoding, error_mode, repaired)

            if best_match is None or best_match[0] < HIGH_CONFIDENCE_THRESHOLD:
                continue

            score_delta, encoding, error_mode, repaired = best_match
            findings.append(
                f"{path.relative_to(ROOT)}:{lineno} score+{score_delta} via {encoding}/{error_mode}\n"
                f"  original: {line!r}\n"
                f"  repaired: {repaired!r}"
            )

    return findings


def _find_replacement_character_findings() -> list[str]:
    findings: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "\ufffd" in line:
                findings.append(f"{path.relative_to(ROOT)}:{lineno} contains replacement character")
    return findings


def _find_known_mojibake_fragments() -> list[str]:
    findings: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for fragment in KNOWN_MOJIBAKE_FRAGMENTS:
                if fragment in line:
                    findings.append(f"{path.relative_to(ROOT)}:{lineno} contains known mojibake fragment: {fragment}")
    return findings


def test_source_files_do_not_contain_high_confidence_mojibake() -> None:
    findings = _find_high_confidence_mojibake_candidates()
    assert not findings, "Detected likely mojibake in source files:\n" + "\n".join(findings)


def test_source_files_do_not_contain_replacement_characters() -> None:
    findings = _find_replacement_character_findings()
    assert not findings, "Detected replacement characters in source files:\n" + "\n".join(findings)


def test_source_files_do_not_contain_known_mojibake_fragments() -> None:
    findings = _find_known_mojibake_fragments()
    assert not findings, "Detected known mojibake fragments in source files:\n" + "\n".join(findings)
