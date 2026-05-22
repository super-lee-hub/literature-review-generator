from services.paper_identity import normalize_doi, normalize_paper_identity


def test_valid_doi_and_doi_url_normalize():
    assert normalize_doi("DOI:10.1000/ABC.") == "10.1000/abc"
    assert normalize_doi("https://doi.org/10.5555/Foo-Bar") == "10.5555/foo-bar"


def test_decorated_doi_url_suffix_normalizes_to_bare_doi():
    assert (
        normalize_doi("10.1016/j.jbusres.2022.03.017 <http://doi.org/10.1016/")
        == "10.1016/j.jbusres.2022.03.017"
    )


def test_polluted_doi_is_not_canonical_and_is_retained_in_diagnostics():
    polluted = "10.1000/ABC\nDownloaded from PDF full text"

    identity = normalize_paper_identity(
        {
            "title": "Polluted DOI Paper",
            "authors": ["Doe, J."],
            "year": 2024,
            "doi": polluted,
        }
    )

    assert normalize_doi(polluted) == ""
    assert identity["canonical_key"] == "polluted doi paper|doe|2024"
    assert identity["canonical_key_source"] == "normalized_title_first_author_year"
    assert identity["rejected_identity_values"][0]["value"] == polluted
    assert identity["rejected_identity_values"][0]["reason"] == "polluted_doi_value"


def test_polluted_explicit_canonical_doi_does_not_override_valid_title_identity():
    identity = normalize_paper_identity(
        {
            "canonical_paper_key": "https://doi.org/10.1234/bad copied prose",
            "title": "Fallback Identity",
            "authors": ["Alice Smith"],
            "year": 2025,
        }
    )

    assert identity["canonical_key"] == "fallback identity|smith|2025"
    assert identity["canonical_key_source"] == "normalized_title_first_author_year"
    assert identity["rejected_identity_values"][0]["field"] == "canonical_paper_key"
