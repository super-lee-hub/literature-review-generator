"""Release-candidate citation style and metadata regression tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from services.citation_catalog import (
    CitationCatalogEntry,
    format_in_text_citation,
    format_reference_entry,
    references_from_catalog_payload,
)
from services.citation_style import CitationStyleEngine, normalize_creators


def _entry(
    *,
    title: str = "A Study of Testing",
    authors: list[Any] | None = None,
    year: str = "2024",
    **kwargs: Any,
) -> CitationCatalogEntry:
    return CitationCatalogEntry(
        index=1,
        paper_id=kwargs.pop("paper_id", title),
        paper_key=kwargs.pop("paper_key", title),
        title=title,
        authors=list(authors or ["Smith, John"]),
        year=year,
        journal=kwargs.pop("journal", "Journal of Testing"),
        doi=kwargs.pop("doi", "10.1234/TEST.1"),
        aliases=[],
        **kwargs,
    )


def test_last_first_and_csl_creators_use_the_family_name() -> None:
    last_first = _entry(authors=["Smith, John"])
    canonical = _entry(
        authors=[],
        creators=[{"family": "Smith", "given": "John"}],
    )

    assert format_in_text_citation(last_first) == "(Smith, 2024)"
    assert format_in_text_citation(canonical) == "(Smith, 2024)"
    assert "John" not in format_in_text_citation(last_first)
    assert format_reference_entry(last_first).startswith("Smith, J. (2024).")


def test_apa_author_count_rules_and_narrative_mode() -> None:
    engine = CitationStyleEngine()
    one = _entry(authors=["Smith, John"])
    two = _entry(authors=["Smith, John", "Jones, Ann"])
    three = _entry(authors=["Smith, John", "Jones, Ann", "Brown, Bob"])

    assert engine.format_in_text(one, mode="parenthetical") == "(Smith, 2024)"
    assert engine.format_in_text(two, mode="parenthetical") == "(Smith & Jones, 2024)"
    assert engine.format_in_text(two, mode="narrative") == "Smith and Jones (2024)"
    assert engine.format_in_text(three, mode="parenthetical") == "(Smith et al., 2024)"

    twenty_one = _entry(
        authors=[f"Family{index}, Given{index}" for index in range(1, 22)]
    )
    reference = engine.format_reference(twenty_one).text
    assert "Family1, G." in reference
    assert "Family19, G." in reference
    assert "Family20" not in reference
    assert "… Family21, G." in reference


def test_same_author_year_gets_deterministic_title_ordered_suffixes() -> None:
    engine = CitationStyleEngine()
    later = _entry(title="Zeta Study", paper_id="zeta")
    earlier = _entry(title="Alpha Study", paper_id="alpha")

    suffixes = engine.disambiguation_suffixes([later, earlier])

    assert suffixes["alpha"] == "a"
    assert suffixes["zeta"] == "b"
    assert engine.format_in_text(earlier, year_suffix=suffixes["alpha"]) == "(Smith, 2024a)"


def test_same_author_year_suffixes_use_document_level_multi_author_identity() -> None:
    engine = CitationStyleEngine()
    first = _entry(title="Alpha", paper_id="first", authors=["Smith, John", "Jones, Ann"])
    second = _entry(title="Beta", paper_id="second", authors=["Smith, John", "Jones, Ann"])
    suffixes = engine.disambiguation_suffixes([second, first])
    assert suffixes == {"first": "a", "second": "b"}


def test_distinct_multi_author_same_first_author_gets_unique_in_text_prefix() -> None:
    engine = CitationStyleEngine()
    first = _entry(
        title="Alpha",
        paper_id="first",
        authors=["Smith, John", "Jones, Ann", "Brown, Bob"],
    )
    second = _entry(
        title="Beta",
        paper_id="second",
        authors=["Smith, John", "Taylor, Ann", "Wilson, Bob"],
    )
    counts = engine.disambiguation_author_counts([first, second])
    assert counts == {"first": 2, "second": 2}
    assert engine.format_in_text(
        replace(first, in_text_author_count=counts["first"])
    ) == "(Smith, Jones, et al., 2024)"
    assert engine.format_in_text(
        replace(second, in_text_author_count=counts["second"])
    ) == "(Smith, Taylor, et al., 2024)"


def test_same_family_prefix_with_ambiguous_initials_uses_full_given_names() -> None:
    engine = CitationStyleEngine()
    first = _entry(
        title="Alpha",
        paper_id="first",
        authors=["Smith, John", "Jones, Ann", "Brown, Bob"],
    )
    second = _entry(
        title="Beta",
        paper_id="second",
        authors=["Smith, Jane", "Jones, Ann", "Brown, Bob"],
    )
    counts = engine.disambiguation_author_counts([first, second])
    full_names = engine.disambiguation_author_full_names([first, second])
    assert full_names == {"first", "second"}
    assert engine.format_in_text(
        replace(
            first,
            in_text_author_count=counts["first"],
            in_text_include_initials=True,
            in_text_include_full_names=True,
        )
    ) == "(Smith, John, Jones, Ann, et al., 2024)"
    assert engine.format_in_text(
        replace(
            second,
            in_text_author_count=counts["second"],
            in_text_include_initials=True,
            in_text_include_full_names=True,
        )
    ) == "(Smith, Jane, Jones, Ann, et al., 2024)"


def test_author_initials_are_not_added_when_coauthor_prefix_already_disambiguates() -> None:
    engine = CitationStyleEngine()
    first = _entry(
        title="Alpha",
        paper_id="first",
        authors=["Smith, John", "Jones, Ann", "Brown, Bob"],
    )
    second = _entry(
        title="Beta",
        paper_id="second",
        authors=["Smith, Jane", "Taylor, Ann", "Wilson, Bob"],
    )
    assert engine.disambiguation_author_initials([first, second]) == set()
    assert engine.disambiguation_author_full_names([first, second]) == set()


def test_rich_apa_reference_has_real_metadata_and_no_markdown() -> None:
    entry = _entry(
        authors=[],
        creators=[{"literal": "World Health Organization"}],
        title="Global Testing Guidance",
        journal="Journal of Testing",
        volume="12",
        issue="3",
        pages="44-59",
        doi="https://doi.org/10.1234/ABC",
    )

    reference = CitationStyleEngine().format_reference(entry)

    assert reference.text == (
        "World Health Organization. (2024). Global Testing Guidance. "
        "Journal of Testing, 12(3), 44-59. https://doi.org/10.1234/abc"
    )
    assert "*" not in reference.text
    assert [segment.text for segment in reference.segments if segment.italic] == [
        "Journal of Testing",
        "12",
    ]


def test_cjk_and_missing_date_are_safe() -> None:
    cjk = _entry(authors=["张三"], year="")
    assert format_in_text_citation(cjk) == "(张三, n.d.)"
    assert "张三" in format_reference_entry(cjk)


def test_catalog_references_are_style_sorted() -> None:
    catalog = {
        "entries": [
            {
                "paper_id": "z",
                "canonical_paper_key": "z",
                "title": "Zeta",
                "authors": ["Smith, John"],
                "year": "2024",
            },
            {
                "paper_id": "a",
                "canonical_paper_key": "a",
                "title": "Alpha",
                "authors": ["Adams, Ann"],
                "year": "2023",
            },
        ]
    }

    references = references_from_catalog_payload(catalog)
    assert references[0].startswith("Adams, A.")
    assert references[1].startswith("Smith, J.")


def test_csl_creator_normalization_preserves_organization() -> None:
    assert normalize_creators(
        [
            {"creatorType": "author", "firstName": "John", "lastName": "Smith"},
            {"literal": "Research Council"},
        ]
    ) == [
        {"family": "Smith", "given": "John"},
        {"literal": "Research Council"},
    ]
