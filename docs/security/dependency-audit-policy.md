# Dependency Audit Policy

The release dependency audit is `pip-audit --strict` against the generated
production lock. A green audit is evidence for the exact lock being tested;
it is not a claim that an unpinned source requirement or a later regeneration
is safe.

An advisory may be excepted only when the project owner records the advisory
ID, affected package and version, impact assessment, compensating control,
owner, and a review/expiry date in the release report. No silent ignore list
or blanket `--ignore-vuln` is permitted in CI.

The release workflow also installs from hash-pinned locks and pins its critical
GitHub Actions to immutable commit SHAs. Live provider, browser, OCR, and
corpus acceptance remain separate gates and are never inferred from this
dependency check.

Optional ChromaDB local-RAG dependencies are excluded from the release locks
while the audit feed reports unresolved advisories without a fix version. The
feature remains disabled by default and must be enabled only in a separately
reviewed environment; this is an explicit boundary, not a suppressed audit.
