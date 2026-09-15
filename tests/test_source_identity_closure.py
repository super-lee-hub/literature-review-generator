from __future__ import annotations

from pathlib import Path

import pytest

from runtime.stage_contracts import PaperWorkItem, SourceBundle
from tests.test_current_stage1_generation import _service


def _item(*, canonical_key: str, source_pdf: Path, source_paper_id: str) -> PaperWorkItem:
    return PaperWorkItem(
        paper_info={},
        source_descriptor={},
        source_mode="direct",
        canonical_paper_key=canonical_key,
        source_paper_id=source_paper_id,
        source_pdf=str(source_pdf),
    )


@pytest.mark.parametrize(
    ("first_key", "second_key", "same_pdf", "reason"),
    [
        ("same-paper", "same-paper", False, "source_identity_duplicate_canonical_paper_key"),
        ("paper-one", "paper-two", True, "source_identity_duplicate_pdf_sha256"),
    ],
)
def test_stage1_rejects_duplicate_source_identity_before_preprocess_or_provider_call(
    monkeypatch,
    tmp_path: Path,
    first_key: str,
    second_key: str,
    same_pdf: bool,
    reason: str,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"%PDF-1.4 first source")
    second.write_bytes(first.read_bytes() if same_pdf else b"%PDF-1.4 second source")
    provider_calls: list[dict[str, object]] = []
    preprocess_calls: list[tuple[object, ...]] = []

    def reader(**kwargs: object) -> dict[str, object]:
        provider_calls.append(dict(kwargs))
        raise AssertionError("provider reader must not run after source identity rejection")

    service, _ = _service(tmp_path, first, reader)

    def prohibit_preprocess(*args: object, **kwargs: object) -> object:
        preprocess_calls.append(args)
        raise AssertionError("preprocess must not run after source identity rejection")

    monkeypatch.setattr(service, "_preprocess", prohibit_preprocess)
    bundle = SourceBundle(
        source_mode="direct",
        project_name="duplicate-source",
        paper_work_items=[
            _item(canonical_key=first_key, source_pdf=first, source_paper_id="first"),
            _item(canonical_key=second_key, source_pdf=second, source_paper_id="second"),
        ],
    )

    with pytest.raises(ValueError, match=reason):
        service.run(bundle)

    assert preprocess_calls == []
    assert provider_calls == []
    assert service.expected_calls == ()
