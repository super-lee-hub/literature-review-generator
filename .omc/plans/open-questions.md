# Open Questions

## docs-migration - 2026-05-05

- [ ] **Bilingual translation strategy for new CN pages** — Phase 2 and Phase 3 create ~20 new `docs/zh-CN/` pages as translations of EN originals (or CN originals for AGENTS.md-extracted content). Should translation be done by: (a) the executor producing both languages in the same pass, (b) a separate translator pass after canonical content is approved, or (c) CN pages created as "intended mirrors" with structural parity but language deferral to a native speaker? This affects per-phase workload and verification criteria.
  - *UPDATE (Plan V2):* Section 6 now documents the canonical language direction and translation workflow. Executor creates canonical first, translates to non-canonical. But the choice between (a), (b), or (c) for the actual execution pass is still a resourcing decision for the executor.

- [x] ~~**docs/*/user/ content depth** — The target architecture specifies `docs/*/user/` pages (installation, GUI, CLI, PDF mode, Zotero mode, config, outputs, validation, troubleshooting). However, acceptance criterion #2 says "README.zh-CN.md and README.en.md remain complete user guides" at root. Should `docs/*/user/` in this migration: (a) only have an index.md linking to root READMEs (defer deep-dives to future), or (b) extract deep-dive pages from README material (e.g., preprocess details, validation internals) that power users would reference separately?~~
  - *RESOLVED (Plan V2):* docs/user/ eliminated entirely (Model A, Architect #3 ACCEPTED). User-facing content lives exclusively at root. docs/ is for internal (developer/AI/runtime/reference) content only.

- [x] ~~**AGENTS.md §9 (config system) — keep in AI handoff or move to reference?** — The config system section in AGENTS.md (~25 lines) is currently mixed: it lists config sections useful to developers AND AI agents configuring API keys. It overlaps with README.zh-CN.md §11 and README.en.md §9. Decision needed: does it stay in `docs/*/ai/index.md` (since AI agents need it to configure the project) or move to `docs/*/reference/` (since it's a reference table)?~~
  - *RESOLVED (Plan V2):* §9 content stays brief in ai/handoff.md for AI agent orientation; detailed config reference belongs in runtime/truth-sources.md since config drives data contracts.

- [ ] **ARCHITECTURE_BASELINE.md — keep root stub or remove entirely?** — This file is explicitly marked as "migration-era baseline, not current runtime truth." It is referenced only historically within AGENTS.md. Unlike AGENTS.md or TRUTH_SOURCES.md, it is not a convention file that tooling or AI agents expect at root. Should it get a root stub or be removed from root entirely (content preserved in `docs/`)?
  - *UPDATE (Plan V2):* Current plan keeps it as a thin stub. Still open for debate.

## docs-migration (Plan V2) - 2026-05-05

- [ ] **AGENTS.md fat stub reading order maintenance** — The fat stub preserves the 18-item reading order with summaries. When docs/ pages are reorganized in the future, the fat stub must be updated alongside them. Is the maintainer willing to accept this as an ongoing maintenance cost, or should the reading order in the stub be simplified to just the top 5-8 most critical items?

- [ ] **CI parity check: hard-fail vs warning on section count mismatch** — The `check-docs-parity.sh` script currently hard-fails on section count mismatch (exit 1). Section counts can vary slightly due to translation naturalness (e.g., CN uses more sub-headings). Should the section count check be a warning instead of a hard fail, with the hard fail reserved for missing files only?

- [ ] **AGENTS.md fat stub brevity enforcement** — The fat stub spec targets 2-3KB. During execution, the executor may produce a 4-5KB stub that drifts toward "full content" rather than the intended "essential mental model." Should there be a hard byte limit enforced, or is "contains the reading order with summaries, regardless of byte count" the real criterion?
