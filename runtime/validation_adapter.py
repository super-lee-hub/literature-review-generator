from __future__ import annotations

from typing import Any, Dict, List


class RuntimeValidationAdapter:
    """Narrow generator-shaped adapter for validator.run_review_validation()."""

    def __init__(self, generator: Any) -> None:
        self._generator = generator

    @property
    def logger(self) -> Any:
        return self._generator.logger

    @property
    def config(self) -> Any:
        return self._generator.config

    @property
    def artifact_registry(self) -> Any:
        return self._generator.artifact_registry

    @property
    def job_workspace(self) -> Any:
        return self._generator.job_workspace

    @property
    def summaries(self) -> List[Dict[str, Any]]:
        return self._generator.summaries

    @summaries.setter
    def summaries(self, value: List[Dict[str, Any]]) -> None:
        self._generator.summaries = value

    def _stage2_validation_enabled(self) -> bool:
        return bool(self._generator._stage2_validation_enabled())

    def _review_draft_v2_path(self) -> str:
        return str(self._generator._review_draft_v2_path())

    def _citation_manifest_path(self) -> str:
        return str(self._generator._citation_manifest_path())

    def _get_review_word_file_path(self) -> str:
        return str(self._generator._get_review_word_file_path())

    def _persist_citation_manifest(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._generator._persist_citation_manifest(*args, **kwargs))

    def save_summaries(self) -> bool:
        return bool(self._generator.save_summaries())

    def _persist_paper_artifact(self, result: Dict[str, Any]) -> bool:
        return bool(self._generator._persist_paper_artifact(result))

    def get_paper_key(self, paper: Dict[str, Any]) -> str:
        return str(self._generator.get_paper_key(paper))
