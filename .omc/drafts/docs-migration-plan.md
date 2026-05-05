[SUPERSEDED by `.omc/plans/docs-migration.md` (Plan Version 2) as of 2026-05-05]

This draft represents the original Option A (full migration, 48 pages, big-bang approach). It has been replaced by Plan Version 2, which adopts Option B (phased by audience, 4 phases, ~33 pages), removes docs/user/ subdirectories, adds bilingual maintenance strategy, and incorporates Architect + Critic review feedback. Consult the formal plan at `.omc/plans/docs-migration.md` for the authoritative specification.

---

# Documentation Architecture Migration Plan (SUPERSEDED)

**Plan saved to:** `.omc/plans/docs-migration.md` (historical reference only)
**Date:** 2026-05-05 (superseded same day)
**Scope:** 10 source files (9 root MDs) migrated into ~40 target docs/ pages across CN/EN mirrors, with root stubs retained.

---

## RALPLAN-DR Summary

### Principles (5)

1. **Single authoritative source per topic.** Each piece of information has ONE canonical location. Root MDs serve users and routing; `docs/` serves developers, AI agents, and runtime reference. Content must not be duplicated across both.

2. **Bilingual parity (CN/EN full mirrors).** Every page under `docs/zh-CN/` must have an equivalent page under `docs/en/`. Content depth and structure are identical; only language differs. No language-specific content gaps allowed.

3. **Root is for users, not internals.** Root MD files are user-facing entry points. Developer, AI, runtime, and reference content belongs under `docs/`. User-facing README files (zh-CN, en) remain complete guides at root.

4. **Thin stubs, never deletion.** Root convention files (AGENTS.md, TRUTH_SOURCES.md, FEATURE_MATRIX.md, etc.) are NOT deleted. They become thin stubs (title + audience note + link to `docs/` authoritative page). This preserves git history and prevents broken links from external references.

5. **Content ownership is explicit.** Every `docs/` subdirectory (`user/`, `developer/`, `ai/`, `runtime/`, `reference/`) has a documented content scope. Contributors must know exactly where to place new content without ambiguity.

### Decision Drivers (Top 3)

1. **Onboarding speed** -- New users find project advantage and pick GUI/CLI within ~5 minutes. AI agents and developers enter from root, find architecture and runtime truth under `docs/` within 3-5 minutes.

2. **Maintainability** -- Single-authoritative-source-per-topic eliminates drift between overlapping root files. Clear content ownership prevents contributors from guessing where documentation goes.

3. **Non-breaking migration** -- Root stubs with links preserve all existing URLs and external references. No file is deleted; no existing link breaks.

### Viable Options

#### Option A: Full migration + root stubs (RECOMMENDED)

Extract all internal content from root MDs into `docs/` in a single migration. Root MDs become thin stubs. README.md is finalized from its current draft.

**Pros:**
- Cleanest final state with zero content ambiguity
- Single migration means no intermediate confusing states
- Each page has clear ownership; no contributor confusion

**Cons:**
- Larger single effort (~40 pages across two languages)
- Requires careful content extraction to avoid losing nuance
- Needs thorough verification pass

**Why Option B is less suitable:**
Incremental extraction creates a multi-phase intermediate state where some content lives in root MDs and some in `docs/`, with cross-references that break between phases. Given the project already has a documented target architecture and well-understood content boundaries, staged publishing adds coordination cost without reducing risk -- the extraction work is the same total effort spread over more phases.

---

## Implementation Plan

### Phase 1: Create docs/ skeleton and index pages

**Goal:** Establish the directory structure and navigation framework so subsequent phases have a target to write into.

**Steps:**

1. Create directory tree:
   ```
   docs/
     zh-CN/
       index.md
       user/
       developer/
       ai/
       runtime/
       reference/
     en/
       index.md
       user/
       developer/
       ai/
       runtime/
       reference/
   ```

2. Write `docs/zh-CN/index.md` and `docs/en/index.md` as navigation hubs. Each index must:
   - State audience and purpose of the docs/ directory
   - List all five subdirectories with one-line descriptions
   - Link back to root README.md for the product homepage
   - Link back to README.zh-CN.md / README.en.md for the user guide

3. Write subdirectory index pages (`user/index.md`, `developer/index.md`, `ai/index.md`, `runtime/index.md`, `reference/index.md`) in both CN and EN as full mirrors. Each must:
   - State the content scope of that subdirectory
   - List planned pages (as placeholder links or TBD markers)
   - Include a note that pages are being populated in subsequent phases

**Acceptance criteria:**
- All directories exist as specified
- All 12 index files (6 CN + 6 EN) are complete bilingual mirrors
- Each index clearly states its audience and content scope
- All indexes link back to root README.md

**Dependencies:** None (Phase 1 can start immediately).

---

### Phase 2: Migrate developer and AI content

**Goal:** Extract all developer-facing and AI-agent-facing content from root MDs into `docs/zh-CN/developer/`, `docs/en/developer/`, `docs/zh-CN/ai/`, and `docs/en/ai/`.

**Steps:**

1. Create `developer/` pages (CN+EN mirrors) by extracting from AGENTS.md, DEVELOPMENT.md, and ARCHITECTURE_BASELINE.md:

   | New docs/ page | Source root MD | Source sections |
   |---|---|---|
   | `developer/architecture.md` | AGENTS.md | Section 4 (architecture overview tree), Section 8 (GUI/CLI relationship) |
   | `developer/pipeline.md` | AGENTS.md | Section 5 (current main chain: stages 1-3, validation/repair) |
   | `developer/module-map.md` | AGENTS.md | Section 12 (where to look when modifying), Section 3 (recommended reading order) |
   | `developer/data-contracts.md` | AGENTS.md | Section 6 (data contracts & truth sources) |
   | `developer/setup.md` | DEVELOPMENT.md | Entire file (environment, deps, dev commands, what not to commit) |
   | `developer/testing.md` | TRUTH_SOURCES.md | Testing and Validation section |
   | `developer/technical-notes.md` | AGENTS.md | Section 11 (technical debt), Section 10 (capabilities list) |
   | `developer/constraints.md` | ARCHITECTURE_BASELINE.md | Hard Constraints, Compatibility Projection, Pointer Atomicity Contract |

2. Create `ai/` pages (CN+EN mirrors) by extracting from AGENTS.md and TRUTH_SOURCES.md:

   | New docs/ page | Source root MD | Source sections |
   |---|---|---|
   | `ai/handoff.md` | AGENTS.md | Section 1 (doc division for AI), Section 2 (one-line summary), Section 14 (conclusion for AI) |
   | `ai/codex-skill.md` | TRUTH_SOURCES.md | Section 7 (AI-native Runtime Bridge) |
   | `ai/operating-rules.md` | AGENTS.md | Section 13 (recommended startup for AI), Section 3 (recommended reading order for AI) |

3. For each page, write in the appropriate language for that directory:
   - `docs/zh-CN/*` pages are written in Chinese (translate where source is English)
   - `docs/en/*` pages are written in English (translate where source is Chinese)
   - Each page pair must be a FULL mirror -- same sections, same depth, same structure

**Acceptance criteria:**
- All 11 developer pages (per language = 22 total) exist with full CN/EN mirroring
- All 3 AI pages (per language = 6 total) exist with full CN/EN mirroring
- Every page has a clear title, audience statement, and navigation links
- Content extracted from AGENTS.md is faithfully preserved (no information loss)
- DEVELOPMENT.md content is fully migrated
- ARCHITECTURE_BASELINE.md developer-relevant content is migrated

**Dependencies:** Phase 1 (index pages must exist first).

---

### Phase 3: Migrate runtime and reference content

**Goal:** Extract all runtime truth, compatibility, reference, and historical content from root MDs into `docs/zh-CN/runtime/`, `docs/en/runtime/`, `docs/zh-CN/reference/`, and `docs/en/reference/`.

**Steps:**

1. Create `runtime/` pages (CN+EN mirrors) by extracting from TRUTH_SOURCES.md, AGENTS.md, and MIGRATION_NOTES.md:

   | New docs/ page | Source root MD | Source sections |
   |---|---|---|
   | `runtime/truth-sources.md` | TRUTH_SOURCES.md | Main Truth Sources (Stages 1-7) |
   | `runtime/workspace-layout.md` | AGENTS.md | Section 7 (Job workspace, output, cache) |
   | `runtime/artifact-lifecycle.md` | TRUTH_SOURCES.md | Compatibility Projections (field, API, input/output) + Key Implementation Notes |
   | `runtime/deprecated-paths.md` | TRUTH_SOURCES.md | Deprecated Paths (Stages 1-4) |
   | `runtime/removal-timeline.md` | TRUTH_SOURCES.md | Removal Timeline (Phases 1-3) |

2. Create `reference/` pages (CN+EN mirrors) by extracting from FEATURE_MATRIX.md, MIGRATION_NOTES.md, ARCHITECTURE_BASELINE.md, and README content:

   | New docs/ page | Source root MD | Source sections |
   |---|---|---|
   | `reference/cli-reference.md` | README.zh-CN.md | Section 9 (command reference table) |
   | `reference/config-reference.md` | AGENTS.md | Section 9 (config system: sections + env vars) |
   | `reference/artifact-glossary.md` | TRUTH_SOURCES.md | Stage 1-7 Key Artifacts lists |
   | `reference/feature-matrix.md` | FEATURE_MATRIX.md | Entire file (feature table + details + roadmap) |
   | `reference/migration-history.md` | MIGRATION_NOTES.md | Entire file (all migration notes) |
   | `reference/architecture-history.md` | ARCHITECTURE_BASELINE.md | Baseline info, Source-of-Truth Matrix, Write-Stop Timeline |

3. Create lightweight `user/` pages (CN+EN mirrors):
   | New docs/ page | Content |
   |---|---|
   | `user/index.md` | Links to README.zh-CN.md (CN) / README.en.md (EN) as the authoritative user guide; lists topic anchors within the README |

**Acceptance criteria:**
- All 5 runtime pages (per language = 10 total) exist with full CN/EN mirroring
- All 6 reference pages (per language = 12 total) exist with full CN/EN mirroring
- 2 user index pages exist
- TRUTH_SOURCES.md content is fully migrated
- FEATURE_MATRIX.md content is fully migrated
- MIGRATION_NOTES.md content is fully migrated
- ARCHITECTURE_BASELINE.md historical content is migrated
- CLI flags and config fields are documented in reference pages, not buried in user guides

**Dependencies:** Phase 2 (developer/ai content provides context for runtime/reference pages).

---

### Phase 4: Create root stubs and finalize README.md

**Goal:** Convert all internal root MDs to thin stubs, finalize README.md as the product homepage, and ensure all navigation links are correct.

**Steps:**

1. Finalize `README.md` (product homepage):
   - Keep the bilingual hero section (title + badge + tagline)
   - Keep the "Why This Project Exists" section (bilingual, condensed)
   - Keep the "Pick Your Path" routing table (update links if needed, add `docs/` references)
   - Condense "What It Does" to a 5-7 line pipeline diagram
   - Move detailed "Quick Start" commands to a 2-line install-and-run summary; point to README.zh-CN.md / README.en.md for full instructions
   - Update "Documentation Boundaries" to reflect the new `docs/` structure:
     ```
     - docs/zh-CN/ & docs/en/: authoritative developer, AI, runtime, and reference documentation
     - AGENTS.md: thin stub -> docs/en/ai/handoff.md
     - TRUTH_SOURCES.md: thin stub -> docs/en/runtime/truth-sources.md
     - FEATURE_MATRIX.md: thin stub -> docs/en/reference/feature-matrix.md
     ```
   - Remove content that duplicates what now lives in `docs/`

2. Create thin stubs for internal root MDs:

   **AGENTS.md** stub (replaces current 14KB file):
   ```markdown
   # AGENTS.md
   > AI agent and developer handoff. The authoritative documentation lives in `docs/`.
   
   - For AI agent onboarding: [docs/en/ai/handoff.md](./docs/en/ai/handoff.md) | [docs/zh-CN/ai/handoff.md](./docs/zh-CN/ai/handoff.md)
   - For architecture: [docs/en/developer/architecture.md](./docs/en/developer/architecture.md)
   - For module map: [docs/en/developer/module-map.md](./docs/en/developer/module-map.md)
   - For runtime truth: [docs/en/runtime/truth-sources.md](./docs/en/runtime/truth-sources.md)
   - Historical content preserved in: [docs/en/reference/migration-history.md](./docs/en/reference/migration-history.md)
   ```

   **TRUTH_SOURCES.md** stub:
   ```markdown
   # TRUTH_SOURCES.md
   > Runtime truth and compatibility documentation has moved to `docs/`.
   
   - Runtime truth sources by stage: [docs/en/runtime/truth-sources.md](./docs/en/runtime/truth-sources.md)
   - Compatibility paths: [docs/en/runtime/compatibility.md](./docs/en/runtime/compatibility.md) -- (if separate page) or reference the truth-sources page
   - Deprecated paths and removal timeline: [docs/en/runtime/deprecated-paths.md](./docs/en/runtime/deprecated-paths.md)
   - Artifact glossary: [docs/en/reference/artifact-glossary.md](./docs/en/reference/artifact-glossary.md)
   ```

   **FEATURE_MATRIX.md** stub:
   ```markdown
   # FEATURE_MATRIX.md
   > Feature status documentation has moved to `docs/`.
   
   - Feature matrix and status: [docs/en/reference/feature-matrix.md](./docs/en/reference/feature-matrix.md)
   - Roadmap: included in the feature-matrix page
   ```

   **DEVELOPMENT.md** stub:
   ```markdown
   # DEVELOPMENT.md
   > Development setup and contribution guide has moved to `docs/`.
   
   - Setup and environment: [docs/en/developer/setup.md](./docs/en/developer/setup.md)
   - Testing: [docs/en/developer/testing.md](./docs/en/developer/testing.md)
   ```

   **ARCHITECTURE_BASELINE.md** stub:
   ```markdown
   # ARCHITECTURE_BASELINE.md
   > Historical architecture baseline. This file is preserved as a migration-era snapshot.
   > Current architecture documentation lives in `docs/`.
   
   - Current architecture: [docs/en/developer/architecture.md](./docs/en/developer/architecture.md)
   - Historical baseline and SOT matrix: [docs/en/reference/architecture-history.md](./docs/en/reference/architecture-history.md)
   - Hard constraints: [docs/en/developer/constraints.md](./docs/en/developer/constraints.md)
   ```

   **MIGRATION_NOTES.md** stub:
   ```markdown
   # MIGRATION_NOTES.md
   > Migration history has moved to `docs/`.
   
   - Full migration history: [docs/en/reference/migration-history.md](./docs/en/reference/migration-history.md)
   ```

3. **Do NOT modify:**
   - `README.zh-CN.md` -- stays as complete Chinese user guide
   - `README.en.md` -- stays as complete English user guide

4. Update cross-references in README.zh-CN.md Section 17 (Documentation Division) and README.en.md Section 1 (Document Split) to reference the new `docs/` structure alongside existing root files.

**Acceptance criteria:**
- README.md is a concise product homepage (under ~60 lines versus current ~104)
- All 7 internal root MDs (AGENTS, TRUTH_SOURCES, FEATURE_MATRIX, DEVELOPMENT, ARCHITECTURE_BASELINE, MIGRATION_NOTES) are thin stubs under 15 lines each
- All stubs have clear "Content moved to docs/" messaging with specific links
- README.zh-CN.md and README.en.md are unchanged in content, only documentation-division sections updated
- No root MD is deleted

**Dependencies:** Phases 2-3 (docs/ must be populated before stubs are valid).

---

### Phase 5: Verification

**Goal:** Validate all 8 acceptance criteria from the project specification.

**Steps:**

1. **Criteria 1 (User onboarding ~5 min):** Open README.md. Verify it loads as a product homepage with clear advantage statement and entry routing. The "Pick Your Path" table must route each audience to the correct document. No internal runtime or developer detail should appear in README.md body.

2. **Criteria 2 (Power user completeness):** Open README.zh-CN.md and README.en.md. Verify each contains all sections: install, GUI workflow, CLI workflow, PDF mode, Zotero mode, config, outputs, artifacts, validation, repair, recovery, troubleshooting. No information loss from pre-migration state.

3. **Criteria 3 (AI/developer entry path):** Start from root AGENTS.md stub. Follow links to `docs/en/ai/handoff.md` -> `docs/en/developer/architecture.md` -> `docs/en/runtime/truth-sources.md`. Verify the path provides: architecture overview, runtime truth, module maps, maintenance rules. No dead links.

4. **Criteria 4 (CN/EN full mirrors):** For each page in `docs/zh-CN/`, verify a corresponding page exists in `docs/en/` with identical structure (same headings, same section count, same content depth). Spot-check 3 page pairs for parity.

5. **Criteria 5 (Root README = concise homepage):** Verify README.md is under 80 lines, contains no internal manual content, no technical debt lists, no config reference tables, no stage-by-stage implementation notes. All internal content must be reachable only via links.

6. **Criteria 6 (User guides preserved):** Diff pre-migration and post-migration README.zh-CN.md and README.en.md. Only documentation-division sections should differ. No user-facing content removed.

7. **Criteria 7 (Root stubs point correctly):** For each of the 7 root stub files, follow every link and verify the target page exists and contains the relevant content. No 404-equivalent.

8. **Criteria 8 (Maintainability):** Check that each `docs/` subdirectory has an `index.md` that clearly states:
   - What content belongs in this subdirectory
   - What content does NOT belong (with pointers to where it goes)
   - This ensures future contributors know exactly where to place new documentation.

**Acceptance criteria for Phase 5:**
- All 8 criteria pass
- Zero broken links in root stubs and docs/ indexes
- CN/EN mirror parity confirmed for all pages

**Dependencies:** Phases 1-4 must be complete.

---

## File Mapping Table

### Complete content migration map

| Current Root File | Content | Destination(s) | Root Stub Behavior |
|---|---|---|---|
| **README.md** | Product homepage (incomplete draft) | Finalized in-place at root (condense to ~60 lines) | N/A -- stays as product homepage |
| **README.zh-CN.md** | Complete Chinese user guide | Stays at root; Section 17 updated to reference docs/ | N/A -- stays complete |
| **README.en.md** | Complete English user guide | Stays at root; Section 1 updated to reference docs/ | N/A -- stays complete |
| **AGENTS.md** | AI/developer handoff (14KB) | Sections 4-14 -> docs/{lang}/developer/* + docs/{lang}/ai/* + docs/{lang}/runtime/* + docs/{lang}/reference/* | Thin stub (~10 lines) linking to docs/ |
| **TRUTH_SOURCES.md** | Runtime truth, compatibility, deprecation (10KB) | All content -> docs/{lang}/runtime/* + docs/{lang}/reference/* | Thin stub (~10 lines) linking to docs/ |
| **FEATURE_MATRIX.md** | Feature status matrix (8KB) | All content -> docs/{lang}/reference/feature-matrix.md | Thin stub (~6 lines) linking to docs/ |
| **DEVELOPMENT.md** | Dev setup/contribution (1.5KB) | All content -> docs/{lang}/developer/setup.md | Thin stub (~6 lines) linking to docs/ |
| **ARCHITECTURE_BASELINE.md** | Historical baseline (5KB) | Content -> docs/{lang}/reference/architecture-history.md + docs/{lang}/developer/constraints.md | Thin stub (~8 lines) linking to docs/ |
| **MIGRATION_NOTES.md** | Migration history (3.5KB) | All content -> docs/{lang}/reference/migration-history.md | Thin stub (~5 lines) linking to docs/ |

### New docs/ pages summary

| Subdirectory | Page Count (per language) | Source Files |
|---|---|---|
| `{lang}/index.md` | 1 | New content |
| `{lang}/user/` | 1 (index.md only) | New content (lightweight, links to README guides) |
| `{lang}/developer/` | 8 pages | AGENTS.md, DEVELOPMENT.md, ARCHITECTURE_BASELINE.md, TRUTH_SOURCES.md |
| `{lang}/ai/` | 3 pages | AGENTS.md, TRUTH_SOURCES.md |
| `{lang}/runtime/` | 5 pages | TRUTH_SOURCES.md, AGENTS.md, MIGRATION_NOTES.md |
| `{lang}/reference/` | 6 pages | FEATURE_MATRIX.md, MIGRATION_NOTES.md, ARCHITECTURE_BASELINE.md, AGENTS.md, README.zh-CN.md |
| **Total per language** | **24 pages** | |
| **Total (CN+EN)** | **48 pages** | |

---

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Content loss during extraction (important nuance dropped from source files when creating docs/ pages) | Medium | High | Review each docs/ page against its source section before publishing. Keep source root MDs in git history (thin stubs replace, not delete). Verification Phase 5 diff-checks README files. |
| CN/EN mirror drift (one language gets updated, the other doesn't) | High | Medium | Document mirror requirement in each subdirectory's index.md. Add a CI check (future) that verifies page count parity. For this migration, all pages are created in both languages in the same phase -- no gap window. |
| Broken external links (DeepWiki, blog posts, bookmarks pointing to old root MD paths) | Medium | Low | Root stubs preserve all original file paths. External links continue to resolve, just to a thinner page that redirects to docs/. |
| README.md over-condensation (loses critical information users need for first impression) | Low | Medium | README.md is finalized from the current bilingual draft, not rewritten from scratch. Only internal details are removed; user-facing value proposition stays. |
| Scope creep (docs/ pages grow beyond planned count) | Medium | Medium | Each subdirectory index explicitly states content boundaries. Phase 5 verification checks that no README content leaked into docs/ and no docs/ content leaked into README. |
| Translation quality (machine-translated CN/EN pages have errors) | Medium | Low | Developer/AI/runtime/reference pages are technical documentation, not marketing copy. Exact terminology matters more than literary quality. AGENTS.md source is already bilingual, reducing translation burden. |

---

## Verification Checklist

### Criterion 1: New user onboarding
- [ ] Open README.md -- hero section, value proposition, and routing table visible without scrolling
- [ ] "Pick Your Path" table has 6 rows covering all audiences
- [ ] No developer-internal content in README.md body (spot-check: no stage implementation details, no technical debt lists)
- [ ] User can identify their entry point (GUI/CLI/Codex) within 5 minutes of reading

### Criterion 2: Power user completeness
- [ ] README.zh-CN.md sections 1-18 all present and complete
- [ ] README.en.md sections 1-12 all present and complete
- [ ] All CLI flags from `python main.py --help` match documented commands
- [ ] Input modes (PDF folder, Zotero) fully documented in both languages
- [ ] Validation/repair flow documented
- [ ] Troubleshooting table present

### Criterion 3: AI/developer entry path
- [ ] AGENTS.md stub links to `docs/en/ai/handoff.md`
- [ ] From `docs/en/ai/handoff.md`, can reach architecture, module map, runtime truth within 3 clicks
- [ ] All AGENTS.md sections 4-14 content present somewhere in docs/
- [ ] No broken links in any stub or index file

### Criterion 4: CN/EN full mirrors
- [ ] `docs/zh-CN/` and `docs/en/` have identical subdirectory structure
- [ ] Page count identical in both language trees (24 pages each)
- [ ] Spot-check 3 page pairs: same heading count, same section depth, same content coverage
- [ ] No page exists in one language but not the other

### Criterion 5: Root README = concise homepage
- [ ] README.md under 80 lines
- [ ] No config reference tables, no CLI flag tables, no stage-by-stage implementation details
- [ ] No technical debt discussion
- [ ] All detailed content reachable only via links to other files

### Criterion 6: User guides preserved
- [ ] `diff` of README.zh-CN.md before/after shows only Section 17 changes
- [ ] `diff` of README.en.md before/after shows only Section 1 changes
- [ ] No user-facing instructions removed from either file

### Criterion 7: Root stubs all valid
- [ ] AGENTS.md stub: all links resolve to existing docs/ pages
- [ ] TRUTH_SOURCES.md stub: all links resolve
- [ ] FEATURE_MATRIX.md stub: link resolves
- [ ] DEVELOPMENT.md stub: links resolve
- [ ] ARCHITECTURE_BASELINE.md stub: links resolve
- [ ] MIGRATION_NOTES.md stub: link resolves

### Criterion 8: Maintainability
- [ ] Every `docs/` subdirectory has an `index.md` with content scope statement
- [ ] Each index states what does NOT belong in that subdirectory
- [ ] A contributor reading any index.md can determine where to place new documentation without ambiguity

---

## Estimated Effort by Phase

| Phase | Description | Pages Created | Pages Modified | Estimated Effort |
|---|---|---|---|---|
| Phase 1 | docs/ skeleton + indexes | 12 index pages | 0 | LOW (~30 min) |
| Phase 2 | Developer + AI content | 22 dev pages + 6 AI pages | 0 | HIGH (~3-4 hours) |
| Phase 3 | Runtime + reference + user | 10 runtime + 12 ref + 2 user = 24 pages | 0 | HIGH (~3-4 hours) |
| Phase 4 | Root stubs + README finalize | 0 | 8 files (README + 7 stubs) | MEDIUM (~1-2 hours) |
| Phase 5 | Verification | 0 | 0 (read-only check) | MEDIUM (~1 hour) |
| **Total** | | **64 pages** | **8 files** | **~9-12 hours** |

Note: Effort is estimated for a single executor working sequentially. CN/EN mirroring accounts for roughly half the total effort. If CN and EN pages can be drafted in parallel (by a bilingual executor or two specialists), total wall-clock time could be ~5-7 hours.

---

## ADR (Architecture Decision Record)

**Decision:** Migrate all developer, AI, runtime, and reference content from root MD files into a structured `docs/zh-CN/` and `docs/en/` mirrored directory tree. Root MD files become thin stubs. User-facing README files stay at root as complete guides.

**Drivers:**
1. Single authoritative source per topic eliminates content drift between overlapping root files
2. Clear content ownership makes future contributions predictable
3. Non-breaking migration preserves all external links via root stubs

**Alternatives considered:**
- **Do nothing:** Rejected because current root MDs have overlapping content (e.g., architecture appears in AGENTS.md, ARCHITECTURE_BASELINE.md, and TRUTH_SOURCES.md) causing maintenance burden and contributor confusion.
- **Incremental extraction (3-phase):** Rejected because intermediate states create broken cross-references between phases and the total effort is the same.

**Why chosen:** Full migration achieves the cleanest final state in a single coordinated effort. The well-defined content categories (user, developer, AI, runtime, reference) map cleanly to directory structure. Root stubs preserve all existing URLs.

**Consequences:**
- AGENTS.md shrinks from 14KB to ~10 lines
- TRUTH_SOURCES.md shrinks from 10KB to ~10 lines
- 5 other root MDs shrink to ~5-10 lines each
- New contributors must update `docs/` pages, not root MDs
- CN/EN mirror requirement adds ongoing maintenance obligation

**Follow-ups:**
1. Add a CI lint check that enforces CN/EN page count parity (prevents mirror drift)
2. Document the migration in CONTRIBUTING.md or developer/setup.md so future maintainers understand the split
3. After 2 release cycles, consider whether the root stubs can be removed entirely (once external link rot risk is assessed)
