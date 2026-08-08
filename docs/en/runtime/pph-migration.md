# Prototype and PPH Migration Boundary

The current `codex/platform-hardening-outline-v3` tree contains no `pph_*.py` production scripts. The PPH ZIP archives that may exist as untracked workspace files are user-owned inputs and are intentionally not staged, imported, or executed by the runtime.

Future reusable capabilities belong in generic services such as `ValidationClosureService`, `ExportBundleService`, declarative review-batch services, or corpus patch transactions. Any historical or project-specific forensic utility must live under an explicitly isolated `tools/legacy_forensics/` boundary, remain outside the CLI/GUI/runtime path, and carry its own read-only or quarantine contract.

Do not copy a prototype branch wholesale into production. Before migration, identify the generic contract, add focused tests, register outputs through `ArtifactRegistry`, and document the compatibility projection. A script that edits Registry, Stage Health, canonical drafts, manifests, or DOCX files in place is not an approved migration.
