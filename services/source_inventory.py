from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, ClassVar, Iterable, Literal, Mapping, Sequence

from services.paper_identity import build_canonical_paper_key


SourceMode = Literal["direct", "zotero", "summary_only"]
SourceFileType = Literal[
    "zotero_report",
    "pdf",
    "external_summary",
    "classification_file",
]
SourceFileStatus = Literal["ready", "missing", "not_file", "unreadable"]
SourceRootStatus = Literal["ready", "missing", "not_directory"]
DiagnosticSeverity = Literal["info", "warning", "error"]
PathInput = str | os.PathLike[str]


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_path(value: PathInput | None) -> str:
    text = os.fspath(value).strip() if value is not None else ""
    if not text:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(text)))


def _path_identity(value: str) -> str:
    return os.path.normcase(os.path.normpath(value)) if value else ""


def _relative_path(path: str, source_root: str) -> str:
    if not path or not source_root:
        return ""
    try:
        if os.path.commonpath([path, source_root]) != source_root:
            return ""
        return os.path.relpath(path, source_root)
    except ValueError:
        return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return payload
    raise TypeError("source_bundle must be a mapping or expose to_dict()")


@dataclass(frozen=True)
class SourceInventoryDiagnosticV1:
    code: str
    severity: DiagnosticSeverity
    message: str
    source_type: str = ""
    path: str = ""

    def validate(self) -> None:
        if not self.code:
            raise ValueError("SourceInventoryDiagnosticV1.code is required")
        if self.severity not in {"info", "warning", "error"}:
            raise ValueError(f"unsupported diagnostic severity: {self.severity}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source_type": self.source_type,
            "path": self.path,
        }

    def fingerprint_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "source_type": self.source_type,
            "path": _path_identity(self.path),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceInventoryDiagnosticV1":
        diagnostic = cls(
            code=str(payload.get("code") or ""),
            severity=str(payload.get("severity") or "warning"),  # type: ignore[arg-type]
            message=str(payload.get("message") or ""),
            source_type=str(payload.get("source_type") or ""),
            path=_normalize_path(str(payload.get("path") or "")),
        )
        diagnostic.validate()
        return diagnostic


@dataclass(frozen=True)
class SourceRootV1:
    root_type: str
    path: str
    status: SourceRootStatus
    diagnostic_codes: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.root_type:
            raise ValueError("SourceRootV1.root_type is required")
        if not self.path:
            raise ValueError("SourceRootV1.path is required")
        if self.status not in {"ready", "missing", "not_directory"}:
            raise ValueError(f"unsupported source root status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_type": self.root_type,
            "path": self.path,
            "status": self.status,
            "diagnostic_codes": list(self.diagnostic_codes),
        }

    def fingerprint_dict(self) -> dict[str, Any]:
        return {
            "root_type": self.root_type,
            "path": _path_identity(self.path),
            "status": self.status,
            "diagnostic_codes": sorted(set(self.diagnostic_codes)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceRootV1":
        root = cls(
            root_type=str(payload.get("root_type") or ""),
            path=_normalize_path(str(payload.get("path") or "")),
            status=str(payload.get("status") or "missing"),  # type: ignore[arg-type]
            diagnostic_codes=tuple(
                sorted({str(item) for item in payload.get("diagnostic_codes", []) if str(item)})
            ),
        )
        root.validate()
        return root


@dataclass(frozen=True)
class SourceFileRecordV1:
    source_type: SourceFileType
    path: str
    source_root: str
    relative_path: str
    content_hash: str
    size_bytes: int
    status: SourceFileStatus
    canonical_paper_key: str = ""
    diagnostic_codes: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.source_type not in {
            "zotero_report",
            "pdf",
            "external_summary",
            "classification_file",
        }:
            raise ValueError(f"unsupported source file type: {self.source_type}")
        if not self.path:
            raise ValueError("SourceFileRecordV1.path is required")
        if self.status not in {"ready", "missing", "not_file", "unreadable"}:
            raise ValueError(f"unsupported source file status: {self.status}")
        if self.status == "ready" and (not self.content_hash or self.size_bytes < 0):
            raise ValueError("ready source files require a content hash and non-negative size")
        if self.status != "ready" and self.content_hash:
            raise ValueError("non-ready source files cannot carry a content hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "path": self.path,
            "source_root": self.source_root,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "canonical_paper_key": self.canonical_paper_key,
            "diagnostic_codes": list(self.diagnostic_codes),
        }

    def fingerprint_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "path": _path_identity(self.path),
            "source_root": _path_identity(self.source_root),
            "relative_path": os.path.normcase(self.relative_path),
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "canonical_paper_key": self.canonical_paper_key,
            "diagnostic_codes": sorted(set(self.diagnostic_codes)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceFileRecordV1":
        record = cls(
            source_type=str(payload.get("source_type") or ""),  # type: ignore[arg-type]
            path=_normalize_path(str(payload.get("path") or "")),
            source_root=_normalize_path(str(payload.get("source_root") or "")),
            relative_path=str(payload.get("relative_path") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            status=str(payload.get("status") or "missing"),  # type: ignore[arg-type]
            canonical_paper_key=str(payload.get("canonical_paper_key") or ""),
            diagnostic_codes=tuple(
                sorted({str(item) for item in payload.get("diagnostic_codes", []) if str(item)})
            ),
        )
        record.validate()
        return record


@dataclass(frozen=True)
class SourceInventoryV1:
    source_mode: SourceMode
    project_name: str
    source_roots: tuple[SourceRootV1, ...]
    files: tuple[SourceFileRecordV1, ...]
    diagnostics: tuple[SourceInventoryDiagnosticV1, ...] = ()

    ARTIFACT_TYPE: ClassVar[str] = "source_inventory"
    ARTIFACT_VERSION: ClassVar[str] = "v1"
    SCHEMA_VERSION: ClassVar[str] = "1.0"

    def validate(self) -> None:
        if self.source_mode not in {"direct", "zotero", "summary_only"}:
            raise ValueError(f"unsupported source mode: {self.source_mode}")
        seen_roots: set[tuple[str, str]] = set()
        for root in self.source_roots:
            root.validate()
            key = (root.root_type, _path_identity(root.path))
            if key in seen_roots:
                raise ValueError(f"duplicate source root: {root.root_type}:{root.path}")
            seen_roots.add(key)

        seen_files: set[tuple[str, str, str]] = set()
        for record in self.files:
            record.validate()
            key = (
                record.source_type,
                _path_identity(record.path),
                record.canonical_paper_key,
            )
            if key in seen_files:
                raise ValueError(
                    "duplicate source file record: "
                    f"{record.source_type}:{record.path}:{record.canonical_paper_key}"
                )
            seen_files.add(key)
        for diagnostic in self.diagnostics:
            diagnostic.validate()

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "artifact_type": self.ARTIFACT_TYPE,
            "artifact_version": self.ARTIFACT_VERSION,
            "schema_version": self.SCHEMA_VERSION,
            "source_mode": self.source_mode,
            "source_roots": sorted(
                (root.fingerprint_dict() for root in self.source_roots),
                key=_stable_json,
            ),
            "files": sorted(
                (record.fingerprint_dict() for record in self.files),
                key=_stable_json,
            ),
            "diagnostics": sorted(
                (diagnostic.fingerprint_dict() for diagnostic in self.diagnostics),
                key=_stable_json,
            ),
        }

    def fingerprint(self) -> str:
        return _sha256_text(_stable_json(self.fingerprint_payload()))

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "artifact_type": self.ARTIFACT_TYPE,
            "artifact_version": self.ARTIFACT_VERSION,
            "schema_version": self.SCHEMA_VERSION,
            "project_name": self.project_name,
            "source_mode": self.source_mode,
            "source_roots": [root.to_dict() for root in self.source_roots],
            "files": [record.to_dict() for record in self.files],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
            "inventory_hash": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceInventoryV1":
        artifact_type = str(payload.get("artifact_type") or "")
        artifact_version = str(payload.get("artifact_version") or "")
        schema_version = str(payload.get("schema_version") or "")
        if artifact_type != cls.ARTIFACT_TYPE:
            raise ValueError(f"unsupported source inventory artifact type: {artifact_type}")
        if artifact_version != cls.ARTIFACT_VERSION or schema_version != cls.SCHEMA_VERSION:
            raise ValueError(
                "unsupported source inventory version: "
                f"artifact_version={artifact_version}, schema_version={schema_version}"
            )

        inventory = cls(
            source_mode=str(payload.get("source_mode") or ""),  # type: ignore[arg-type]
            project_name=str(payload.get("project_name") or ""),
            source_roots=tuple(
                SourceRootV1.from_dict(item)
                for item in payload.get("source_roots", [])
                if isinstance(item, Mapping)
            ),
            files=tuple(
                SourceFileRecordV1.from_dict(item)
                for item in payload.get("files", [])
                if isinstance(item, Mapping)
            ),
            diagnostics=tuple(
                SourceInventoryDiagnosticV1.from_dict(item)
                for item in payload.get("diagnostics", [])
                if isinstance(item, Mapping)
            ),
        )
        inventory.validate()
        claimed_hash = str(payload.get("inventory_hash") or "")
        if claimed_hash and claimed_hash != inventory.fingerprint():
            raise ValueError("source inventory hash does not match its fingerprint payload")
        return inventory


def _root_record(root_type: str, path: str) -> tuple[SourceRootV1, SourceInventoryDiagnosticV1 | None]:
    if os.path.isdir(path):
        return SourceRootV1(root_type=root_type, path=path, status="ready"), None
    if os.path.exists(path):
        code = "source_root_not_directory"
        return (
            SourceRootV1(
                root_type=root_type,
                path=path,
                status="not_directory",
                diagnostic_codes=(code,),
            ),
            SourceInventoryDiagnosticV1(
                code=code,
                severity="error",
                message=f"Configured {root_type} source root is not a directory",
                source_type=root_type,
                path=path,
            ),
        )
    code = "source_root_missing"
    return (
        SourceRootV1(
            root_type=root_type,
            path=path,
            status="missing",
            diagnostic_codes=(code,),
        ),
        SourceInventoryDiagnosticV1(
            code=code,
            severity="error",
            message=f"Configured {root_type} source root does not exist",
            source_type=root_type,
            path=path,
        ),
    )


def _inspect_file(
    *,
    source_type: SourceFileType,
    path: str,
    source_root: str,
    canonical_paper_key: str,
) -> tuple[SourceFileRecordV1, SourceInventoryDiagnosticV1 | None]:
    relative_path = _relative_path(path, source_root)
    diagnostic_codes: list[str] = []
    if source_root and not relative_path:
        diagnostic_codes.append("source_outside_root")

    if not os.path.exists(path):
        diagnostic_codes.append("source_file_missing")
        return (
            SourceFileRecordV1(
                source_type=source_type,
                path=path,
                source_root=source_root,
                relative_path=relative_path,
                content_hash="",
                size_bytes=0,
                status="missing",
                canonical_paper_key=canonical_paper_key,
                diagnostic_codes=tuple(sorted(set(diagnostic_codes))),
            ),
            SourceInventoryDiagnosticV1(
                code="source_file_missing",
                severity="error",
                message=f"{source_type} source file does not exist",
                source_type=source_type,
                path=path,
            ),
        )
    if not os.path.isfile(path):
        diagnostic_codes.append("source_path_not_file")
        return (
            SourceFileRecordV1(
                source_type=source_type,
                path=path,
                source_root=source_root,
                relative_path=relative_path,
                content_hash="",
                size_bytes=0,
                status="not_file",
                canonical_paper_key=canonical_paper_key,
                diagnostic_codes=tuple(sorted(set(diagnostic_codes))),
            ),
            SourceInventoryDiagnosticV1(
                code="source_path_not_file",
                severity="error",
                message=f"{source_type} source path is not a file",
                source_type=source_type,
                path=path,
            ),
        )

    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
    except OSError as exc:
        diagnostic_codes.append("source_file_unreadable")
        return (
            SourceFileRecordV1(
                source_type=source_type,
                path=path,
                source_root=source_root,
                relative_path=relative_path,
                content_hash="",
                size_bytes=0,
                status="unreadable",
                canonical_paper_key=canonical_paper_key,
                diagnostic_codes=tuple(sorted(set(diagnostic_codes))),
            ),
            SourceInventoryDiagnosticV1(
                code="source_file_unreadable",
                severity="error",
                message=f"Could not read {source_type} source file: {exc.__class__.__name__}",
                source_type=source_type,
                path=path,
            ),
        )

    record = SourceFileRecordV1(
        source_type=source_type,
        path=path,
        source_root=source_root,
        relative_path=relative_path,
        content_hash=digest.hexdigest(),
        size_bytes=size_bytes,
        status="ready",
        canonical_paper_key=canonical_paper_key,
        diagnostic_codes=tuple(sorted(set(diagnostic_codes))),
    )
    record.validate()
    if "source_outside_root" in diagnostic_codes:
        return (
            record,
            SourceInventoryDiagnosticV1(
                code="source_outside_root",
                severity="warning",
                message=f"{source_type} source file is outside its declared source root",
                source_type=source_type,
                path=path,
            ),
        )
    return record, None


def _coerce_diagnostic(value: SourceInventoryDiagnosticV1 | Mapping[str, Any]) -> SourceInventoryDiagnosticV1:
    if isinstance(value, SourceInventoryDiagnosticV1):
        return value
    return SourceInventoryDiagnosticV1.from_dict(value)


def _sort_and_dedupe_roots(roots: Iterable[SourceRootV1]) -> tuple[SourceRootV1, ...]:
    by_key: dict[tuple[str, str], SourceRootV1] = {}
    for root in roots:
        by_key[(root.root_type, _path_identity(root.path))] = root
    return tuple(sorted(by_key.values(), key=lambda item: (item.root_type, _path_identity(item.path))))


def _sort_and_dedupe_files(files: Iterable[SourceFileRecordV1]) -> tuple[SourceFileRecordV1, ...]:
    by_key: dict[tuple[str, str, str], SourceFileRecordV1] = {}
    for record in files:
        key = (record.source_type, _path_identity(record.path), record.canonical_paper_key)
        by_key[key] = record
    return tuple(
        sorted(
            by_key.values(),
            key=lambda item: (item.source_type, _path_identity(item.path), item.canonical_paper_key),
        )
    )


def _sort_and_dedupe_diagnostics(
    diagnostics: Iterable[SourceInventoryDiagnosticV1],
) -> tuple[SourceInventoryDiagnosticV1, ...]:
    by_key: dict[tuple[str, str, str, str], SourceInventoryDiagnosticV1] = {}
    for diagnostic in diagnostics:
        key = (
            diagnostic.code,
            diagnostic.severity,
            diagnostic.source_type,
            _path_identity(diagnostic.path),
        )
        by_key[key] = diagnostic
    return tuple(
        sorted(
            by_key.values(),
            key=lambda item: (item.code, item.severity, item.source_type, _path_identity(item.path)),
        )
    )


def build_source_inventory(
    *,
    source_mode: SourceMode,
    project_name: str = "",
    source_bundle: Mapping[str, Any] | Any | None = None,
    pdf_paths: Iterable[PathInput] = (),
    pdf_root: PathInput | None = None,
    zotero_report: PathInput | None = None,
    zotero_root: PathInput | None = None,
    external_summary_paths: Iterable[PathInput] = (),
    classification_paths: Iterable[PathInput] = (),
    diagnostics: Sequence[SourceInventoryDiagnosticV1 | Mapping[str, Any]] = (),
) -> SourceInventoryV1:
    """Build an inventory from explicit source paths without directory discovery.

    The caller should pass the post-intake ``SourceBundle`` for direct/Zotero
    jobs.  Summary-only jobs can omit a bundle and provide only explicit
    summary files.  Empty strings are ignored before path normalization, so an
    empty source can never turn into an implicit scan or record of the CWD.
    """

    if source_mode not in {"direct", "zotero", "summary_only"}:
        raise ValueError(f"unsupported source mode: {source_mode}")

    bundle_payload: Mapping[str, Any] = {}
    source_snapshot: Mapping[str, Any] = {}
    bundle_work_items: Sequence[Any] = ()
    if source_bundle is not None:
        bundle_payload = _mapping(source_bundle)
        bundle_mode = str(bundle_payload.get("source_mode") or "")
        if bundle_mode and bundle_mode != source_mode:
            raise ValueError(
                f"source inventory mode {source_mode!r} does not match SourceBundle mode {bundle_mode!r}"
            )
        if not project_name:
            project_name = str(bundle_payload.get("project_name") or "")
        raw_snapshot = bundle_payload.get("source_snapshot")
        if isinstance(raw_snapshot, Mapping):
            source_snapshot = raw_snapshot
        raw_items = bundle_payload.get("paper_work_items")
        if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)):
            bundle_work_items = raw_items

    explicit_pdf_root = _normalize_path(pdf_root)
    explicit_zotero_root = _normalize_path(zotero_root)
    if not explicit_pdf_root and source_mode == "direct":
        explicit_pdf_root = _normalize_path(str(source_snapshot.get("pdf_folder") or ""))
    if not explicit_zotero_root and source_mode == "zotero":
        explicit_zotero_root = _normalize_path(str(source_snapshot.get("library_path") or ""))
    explicit_report = _normalize_path(zotero_report)
    if not explicit_report and source_mode == "zotero":
        explicit_report = _normalize_path(str(source_snapshot.get("zotero_report") or ""))

    roots: list[SourceRootV1] = []
    records: list[SourceFileRecordV1] = []
    all_diagnostics = [_coerce_diagnostic(item) for item in diagnostics]

    def add_root(root_type: str, raw_path: PathInput | None) -> str:
        path = _normalize_path(raw_path)
        if not path:
            return ""
        root, diagnostic = _root_record(root_type, path)
        roots.append(root)
        if diagnostic:
            all_diagnostics.append(diagnostic)
        return path

    def add_file(
        source_type: SourceFileType,
        raw_path: PathInput | None,
        *,
        source_root: str,
        canonical_paper_key: str = "",
    ) -> None:
        path = _normalize_path(raw_path)
        if not path:
            return
        record, diagnostic = _inspect_file(
            source_type=source_type,
            path=path,
            source_root=source_root,
            canonical_paper_key=canonical_paper_key,
        )
        records.append(record)
        if diagnostic:
            all_diagnostics.append(diagnostic)

    resolved_pdf_root = ""
    if source_mode == "direct":
        resolved_pdf_root = add_root("pdf", explicit_pdf_root)
    elif source_mode == "zotero":
        resolved_pdf_root = add_root("zotero_library", explicit_zotero_root)
        if explicit_report:
            report_root = add_root("zotero_report", os.path.dirname(explicit_report))
            add_file("zotero_report", explicit_report, source_root=report_root)

    for item in bundle_work_items:
        try:
            item_payload = _mapping(item)
        except TypeError:
            continue
        paper_info = item_payload.get("paper_info")
        paper_mapping = paper_info if isinstance(paper_info, Mapping) else {}
        source_pdf = str(
            item_payload.get("source_pdf")
            or paper_mapping.get("source_pdf")
            or paper_mapping.get("pdf_path")
            or ""
        )
        canonical_key = str(item_payload.get("canonical_paper_key") or "").strip()
        if not canonical_key and paper_mapping:
            canonical_key = build_canonical_paper_key(paper_mapping)
        add_file(
            "pdf",
            source_pdf,
            source_root=resolved_pdf_root or _normalize_path(os.path.dirname(_normalize_path(source_pdf))),
            canonical_paper_key=canonical_key,
        )

    inventoried_pdf_paths = {
        _path_identity(record.path)
        for record in records
        if record.source_type == "pdf"
    }
    raw_resolutions = source_snapshot.get("pdf_resolutions")
    if isinstance(raw_resolutions, Sequence) and not isinstance(raw_resolutions, (str, bytes)):
        for raw_resolution in raw_resolutions:
            if not isinstance(raw_resolution, Mapping):
                continue
            selected_path = _normalize_path(str(raw_resolution.get("selected_path") or ""))
            if not selected_path or _path_identity(selected_path) in inventoried_pdf_paths:
                continue
            raw_identity = raw_resolution.get("identity")
            identity = raw_identity if isinstance(raw_identity, Mapping) else {}
            expected = identity.get("expected")
            expected_mapping = expected if isinstance(expected, Mapping) else {}
            canonical_key = build_canonical_paper_key(
                expected_mapping
                or {
                    "title": str(raw_resolution.get("title") or Path(selected_path).stem),
                }
            )
            add_file(
                "pdf",
                selected_path,
                source_root=resolved_pdf_root or _normalize_path(os.path.dirname(selected_path)),
                canonical_paper_key=canonical_key,
            )
            inventoried_pdf_paths.add(_path_identity(selected_path))

    for raw_path in pdf_paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        canonical_key = build_canonical_paper_key({"title": Path(path).stem})
        add_file(
            "pdf",
            path,
            source_root=resolved_pdf_root or _normalize_path(os.path.dirname(path)),
            canonical_paper_key=canonical_key,
        )

    for raw_path in external_summary_paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        summary_root = add_root("external_summary", os.path.dirname(path))
        add_file("external_summary", path, source_root=summary_root)

    for raw_path in classification_paths:
        path = _normalize_path(raw_path)
        if not path:
            continue
        classification_root = add_root("classification", os.path.dirname(path))
        add_file("classification_file", path, source_root=classification_root)

    if source_mode in {"direct", "zotero"} and not any(record.source_type == "pdf" for record in records):
        all_diagnostics.append(
            SourceInventoryDiagnosticV1(
                code="no_pdf_sources",
                severity="warning",
                message="No explicit PDF source files were provided by source intake",
                source_type="pdf",
            )
        )
    if source_mode == "zotero" and not any(record.source_type == "zotero_report" for record in records):
        all_diagnostics.append(
            SourceInventoryDiagnosticV1(
                code="missing_zotero_report_source",
                severity="error",
                message="Zotero source mode requires an explicit report file",
                source_type="zotero_report",
            )
        )
    if source_mode == "summary_only" and not any(
        record.source_type == "external_summary" for record in records
    ):
        all_diagnostics.append(
            SourceInventoryDiagnosticV1(
                code="no_external_summary_sources",
                severity="warning",
                message="Summary-only source mode has no explicit summary files",
                source_type="external_summary",
            )
        )

    inventory = SourceInventoryV1(
        source_mode=source_mode,
        project_name=project_name,
        source_roots=_sort_and_dedupe_roots(roots),
        files=_sort_and_dedupe_files(records),
        diagnostics=_sort_and_dedupe_diagnostics(all_diagnostics),
    )
    inventory.validate()
    return inventory
