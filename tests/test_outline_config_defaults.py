"""Tests for Outline Intelligence v2 config defaults and validation."""

import pytest

from services.config_compat import CompatConfigView


def _make_config(**overrides):
    """Build a minimal config dict for testing outline defaults."""
    base = {
        "Paths": {"output_path": "./output"},
        "Primary_Reader_API": {"api_key": "pk", "model": "m"},
        "Writer_API": {"api_key": "wk", "model": "wm"},
        "Backup_Reader_API": {"api_key": "bk", "model": "bm"},
    }
    for section, values in overrides.items():
        base.setdefault(section, {})
        base[section].update(values)
    return base


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

def test_default_enable_outline_intelligence_v2_is_false():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_v2_enabled() is False


def test_default_candidate_count_is_3():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_candidate_count() == 3


def test_default_max_candidate_count_is_3():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_max_candidate_count() == 3


def test_default_enable_multi_model_critique_is_true():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_multi_model_critique_enabled() is True


def test_default_enable_coverage_audit_is_true():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_coverage_audit_enabled() is True


def test_default_require_explicit_adopt_is_true():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_require_explicit_adopt() is True


def test_default_allow_bibliometric_provider_is_false():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_allow_bibliometric_provider() is False


def test_default_outline_models_are_correct():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_model() == "Outline_API"
    assert view.structure_critic_model() == "Writer_API"
    assert view.coverage_critic_model() == "Primary_Reader_API"
    assert view.arbitrator_model() == "Outline_API"


def test_default_cost_control_values():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_max_critique_models() == 2
    assert view.outline_max_summary_refs_per_prompt() == 80
    assert view.outline_max_retry_count() == 2


# ---------------------------------------------------------------------------
# V2 sub-switches only effective when v2 enabled
# ---------------------------------------------------------------------------

def test_literature_map_enabled_defaults_true_but_only_effective_with_v2():
    cfg = _make_config()
    view = CompatConfigView.from_config(cfg)
    assert view.outline_literature_map_enabled() is True
    # But with v2 disabled, it should not matter
    assert view.outline_v2_enabled() is False


# ---------------------------------------------------------------------------
# Candidate count validation (production v2)
# ---------------------------------------------------------------------------



def test_production_v2_malformed_candidate_count_fails_validation():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "abc"})
    view = CompatConfigView.from_config(cfg)
    errors = view.validate_outline_v2_config()
    assert any("candidate_count" in e and "integer" in e for e in errors)

def test_production_v2_candidate_count_1_fails_validation():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "1"})
    view = CompatConfigView.from_config(cfg)
    errors = view.validate_outline_v2_config()
    assert any("candidate_count" in e.lower() for e in errors)


def test_production_v2_candidate_count_4_fails_validation():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "4"})
    view = CompatConfigView.from_config(cfg)
    errors = view.validate_outline_v2_config()
    assert any("candidate_count" in e.lower() for e in errors)


def test_production_v2_candidate_count_2_passes_validation():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "2"})
    view = CompatConfigView.from_config(cfg)
    errors = view.validate_outline_v2_config()
    assert not any("candidate_count" in e.lower() for e in errors)


def test_production_v2_candidate_count_3_passes_validation():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "3"})
    view = CompatConfigView.from_config(cfg)
    errors = view.validate_outline_v2_config()
    assert not any("candidate_count" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Invalid values are not clamped/coerced in production
# ---------------------------------------------------------------------------

def test_invalid_candidate_count_not_clamped():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "candidate_count": "99"})
    view = CompatConfigView.from_config(cfg)
    # The raw value should still be reported, not clamped
    count = view.outline_candidate_count()
    assert count == 99  # Raw value preserved; validation catches it
    errors = view.validate_outline_v2_config()
    assert len(errors) > 0


# ---------------------------------------------------------------------------
# Test/dev fixture mode isolation
# ---------------------------------------------------------------------------

def test_test_dev_fixture_mode_is_explicit():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true"})
    view = CompatConfigView.from_config(cfg)
    # Test/dev fixture is not the default production path
    assert view.outline_test_dev_fixture_mode() is False


def test_test_dev_fixture_mode_must_be_explicitly_enabled():
    cfg = _make_config(Outline={"enable_outline_intelligence_v2": "true", "test_dev_fixture_mode": "true"})
    view = CompatConfigView.from_config(cfg)
    assert view.outline_test_dev_fixture_mode() is True


# ---------------------------------------------------------------------------
# Config does not affect legacy behavior when v2 disabled
# ---------------------------------------------------------------------------

def test_legacy_mode_not_affected_by_v2_config():
    cfg = _make_config(
        Outline={
            "enable_outline_intelligence_v2": "false",
            "candidate_count": "5",  # Invalid in v2 but v2 is off
        }
    )
    view = CompatConfigView.from_config(cfg)
    # With v2 disabled, invalid v2 config should not cause issues
    assert view.outline_v2_enabled() is False
    # But validation of v2 config should still warn
    errors = view.validate_outline_v2_config()
    # When v2 is disabled, validate_outline_v2_config should be lenient
    assert len(errors) == 0


# ---------------------------------------------------------------------------
# Dependency safety
# ---------------------------------------------------------------------------

def test_dependency_manifests_unchanged():
    """Verify no new packages are required by v2 slice."""
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Read current deps
    req_path = os.path.join(repo_root, "requirements.txt")
    if os.path.exists(req_path):
        with open(req_path, "r") as f:
            reqs = f.read()

        # These packages must NOT be present
        forbidden = [
            "networkx",
            "bibliometrix",
            "crossref",
            "openalex",
            "semantic-scholar",
            "citeproc",
        ]
        for pkg in forbidden:
            assert pkg.lower() not in reqs.lower(), f"Forbidden package '{pkg}' found in requirements.txt"
