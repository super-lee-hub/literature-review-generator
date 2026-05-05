# Plan: Documentation Architecture Migration

**Plan Version 2** -- supersedes draft of 2026-05-05
**Plan saved to:** `.omc/plans/docs-migration.md`
**Date:** 2026-05-05 (revised)
**Mode:** Consensus (RALPLAN-DR), Deliberate (high-risk: multi-file cross-reference migration)

---

## 1. RALPLAN-DR Summary

### Principles

1. **Audience ownership determines location.** User-facing content stays at root (README family). Developer, AI-agent, and runtime-truth content moves to `docs/` with thin root stubs. Each piece of content has exactly one authoritative home.
2. **Root stays thin.** Only entry-point READMEs and brief navigational stubs remain at root. No deep technical content at the top level. No developer/AI/runtime truth mixed into user README bodies beyond short nav links.
3. **Bilingual parity.** Every `docs/zh-CN/` page has a full-content mirror in `docs/en/`, never a summary. Chinese and English docs are full mirrors of each other.
4. **Stubs over silence.** Root convention files that AI agents or tooling expect (AGENTS.md, TRUTH_SOURCES.md, etc.) become thin stubs linking to `docs/`, never deleted outright.
5. **Phased and reversible.** Each phase produces a coherent, usable intermediate state. Rollback is per-phase `git revert` -- no phase leaves the repository in a broken state.

### Decision Drivers

1. **Discoverability** -- AI agents entering via `AGENTS.md` must still find the authoritative handoff in `docs/`; no convention file disappears. Root README nav links must point to correct `docs/` paths. The AGENTS.md stub must preserve the full mental model in a single file (fat stub, ~2-3KB) so a fresh AI agent still gets oriented in 3-5 minutes without multiple tool calls.
2. **Content non-duplication** -- Zero copy-paste between root stubs and `docs/`. Each fact lives in exactly one file. Stubs contain only a brief description and a link.
3. **Reviewability** -- A migration touching 9+ source files and creating ~30 new files demands reviewable increments. A single 40-file commit is hard to audit for link correctness and content integrity.

### Viable Options

| | Option A: Big-Bang | Option B: Phased by Audience (RECOMMENDED) |
|---|---|---|
| **Approach** | Create entire `docs/` tree and all stubs in one commit | Scaffold first, then migrate by content category, verify each phase |
| **Files per step** | ~40 in one commit | ~8-14 per phase, 4 phases |
| **Reviewability** | Low -- fatigue on large diff | High -- each phase is independently reviewable |
| **Intermediate coherence** | Risky -- links may break during creation | Safe -- each phase is a coherent state |
| **Rollback blast radius** | Full migration | Single phase |
| **Duration** | 1 session | 3-4 sessions |

**Option C (Stub-First) is invalidated** because creating root stubs that point to not-yet-existing `docs/` pages would create dead links during the migration window -- worse than either alternative.

**Chosen: Option B -- Phased by Audience.** Each phase migrates one content category (developer, AI+runtime, reference) and concludes with verification before proceeding.

---

## 2. ADR (Architecture Decision Record)

- **Decision:** Migrate root-level developer/AI/runtime documentation into a structured bilingual `docs/` tree in 4 phases, organized by audience (developer, AI, runtime, reference), leaving thin navigational stubs at root for convention files. AGENTS.md stub uses a "fat stub" pattern (~2-3KB) that preserves the full narrative reading order with brief section summaries, so a fresh AI agent can still get the complete mental model from one file.
- **Drivers:** Discoverability for AI agents via convention stubs; single authoritative home per content unit; reviewability via phased commits; bilingual parity mandate; one-file AI handoff preservation.
- **Alternatives considered:**
  - **Big-bang (rejected):** Too many files in one commit for effective review; high risk of cross-reference link breakage.
  - **Stub-first (rejected):** Dead links during migration window; confusing intermediate state.
  - **Delete root files outright (rejected):** Violates principle #4 -- breaks tool discovery that expects root convention files.
  - **Single docs/en/ai/handoff.md preserving full AGENTS.md content (rejected in favor of fat stub):** While this preserves the single-document narrative, it removes AI agent discoverability from the root entirely. The fat stub approach keeps the mental model at root while putting expanded detail in docs/.
- **Why this option:** Phased migration provides reviewable increments, reversible steps, and preserves discoverability throughout. Each phase produces a coherent state. The fat stub pattern for AGENTS.md resolves the fragmentation antithesis by keeping the full reading order in one file.
- **Consequences:**
  - Root becomes a clean entry-point surface (3 README files + 5-6 navigational stubs)
  - `docs/` becomes the authoritative home for all non-user-facing documentation
  - Future documentation writers have clear guidance: audience determines file location
  - Bilingual maintenance burden increases (mirror edits), but spec mandates this
  - AGENTS.md fat stub must be updated when docs/ pages change structure
  - No `docs/user/` subdirectory exists -- user-facing content lives at root README level only
- **Follow-ups:**
  - Bilingual CI parity check is part of Phase 4 deliverables (not future)
  - Consider a `docs/CONTRIBUTING.md` with writer guidelines for the new structure
  - After 2 release cycles, evaluate whether root stubs other than AGENTS.md can be removed

---

## 3. Architect Feedback Disposition

The following table records how each recommendation from the Architecture review was resolved.

| # | Architect Recommendation | Disposition | Rationale |
|---|---|---|---|
| **#1** | Reduce granularity: draft had 48 pages across 22 developer pages; too many micro-pages | **ACCEPTED (modified form)** | Plan Version 2 targets ~33 pages (down from 48). Phase 2 developer pages consolidated to 4 (setup, architecture, architecture-baseline, plus extracted architecture.md from AGENTS.md). No micro-pages like `testing.md`, `technical-notes.md`, `constraints.md` from the draft. |
| **#2** | Re-sequence by source document instead of audience | **REJECTED** | Would break audience-based organization, which is the core structural principle. Instead, content extraction uses copy-not-move semantics: source files (AGENTS.md, TRUTH_SOURCES.md) stay intact until their rewrite phase, so no cross-phase coordination is needed. |
| **#3** | Resolve docs/user/ vs root README contradiction | **ACCEPTED (Model A)** | `docs/user/` subdirectory eliminated entirely. User-facing content lives exclusively at root (README.md, README.zh-CN.md, README.en.md). `docs/` is strictly for internal (developer/AI/runtime/reference) content. |
| **#4** | (implicit) Defend AGENTS.md fragmentation | **ACCEPTED (fat stub pattern)** | See ADR and Section 4.3. AGENTS.md root stub preserves full reading order with brief summaries at ~2-3KB, resolving the "3-5 minute single-page handoff" antithesis. |

---

## 4. Implementation Plan

### Phase 1: Scaffolding -- Create `docs/` skeleton (NO user/ subdirectory)

**Scope:** Establish the directory structure and index pages. Zero content migration in this phase -- only landing/index pages that describe what will live in each section. No `docs/*/user/` directories are created; user-facing content is exclusively at root.

**File operations (create only, ~10 new files):**

| Action | File | Description |
|--------|------|-------------|
| CREATE | `docs/zh-CN/index.md` | Docs site landing page (CN). Explains the 4 sections (developer, AI, runtime, reference), links to root README.md as the primary user entry point. |
| CREATE | `docs/en/index.md` | Docs site landing page (EN). Full mirror of CN version. |
| CREATE | `docs/zh-CN/developer/index.md` | Developer docs landing. Lists architecture, setup, testing, contribution pages (links to future Phase 2 content). |
| CREATE | `docs/en/developer/index.md` | Developer docs landing (EN). Mirror. |
| CREATE | `docs/zh-CN/ai/index.md` | AI agent docs landing. Lists handoff, skill, runtime bridge pages (links to future Phase 3 content). |
| CREATE | `docs/en/ai/index.md` | AI agent docs landing (EN). Mirror. |
| CREATE | `docs/zh-CN/runtime/index.md` | Runtime truth docs landing. Lists truth sources, artifact lifecycle, workspace layout (links to future Phase 3 content). |
| CREATE | `docs/en/runtime/index.md` | Runtime truth docs landing (EN). Mirror. |
| CREATE | `docs/zh-CN/reference/index.md` | Reference docs landing. Lists CLI flags, config fields, feature matrix, migration history (links to future Phase 2/4 content). |
| CREATE | `docs/en/reference/index.md` | Reference docs landing (EN). Mirror. |

Also create empty placeholder directories:
- `docs/zh-CN/developer/`, `docs/zh-CN/ai/`, `docs/zh-CN/runtime/`, `docs/zh-CN/reference/`
- `docs/en/developer/`, `docs/en/ai/`, `docs/en/runtime/`, `docs/en/reference/`

**Explicitly NOT created:** `docs/zh-CN/user/` and `docs/en/user/`. User-facing content lives at root (README.md, README.zh-CN.md, README.en.md). `docs/` is for internal content only.

**Acceptance criteria:**
- [ ] `docs/` directory exists with complete zh-CN/ and en/ subtrees (4 sections each, no user/)
- [ ] Every section has an `index.md` landing page in both languages
- [ ] All index pages have correct relative links to sibling pages (even if target pages don't exist yet -- links will resolve in later phases)
- [ ] CN and EN index pages are full mirrors (not summaries)
- [ ] Root files are untouched (no regression)

**Dependencies:** None (Phase 1 is self-contained)

---

### Phase 2: Migrate Developer + Reference Content

**Scope:** Move `DEVELOPMENT.md`, `ARCHITECTURE_BASELINE.md`, `FEATURE_MATRIX.md`, and `MIGRATION_NOTES.md` into `docs/`. Extract architecture overview from `AGENTS.md` (this is a copy operation -- AGENTS.md remains intact at root until Phase 3). Create root stubs for migrated files.

**File operations (~18 files):**

| Action | Source | Destination | Notes |
|--------|--------|-------------|-------|
| CREATE | (new) | `docs/zh-CN/developer/setup.md` | CN translation of DEVELOPMENT.md content |
| CREATE | (new) | `docs/en/developer/setup.md` | EN: migrate full content from `DEVELOPMENT.md` |
| CREATE | (from AGENTS.md §4,§8,§11,§12) | `docs/zh-CN/developer/architecture.md` | CN: architecture overview + GUI/CLI relationship + tech debt + module map, extracted from AGENTS.md. Source language for AGENTS.md content is Chinese. |
| CREATE | (from AGENTS.md §4,§8,§11,§12) | `docs/en/developer/architecture.md` | EN translation of architecture.md. Secondary direction: AGENTS.md source is Chinese, EN is translation. |
| CREATE | (new) | `docs/zh-CN/developer/architecture-baseline.md` | CN translation of ARCHITECTURE_BASELINE.md content. Source: EN. |
| CREATE | (new) | `docs/en/developer/architecture-baseline.md` | EN: migrate full content from `ARCHITECTURE_BASELINE.md`. Canonical source is EN. |
| CREATE | (new) | `docs/zh-CN/reference/feature-matrix.md` | CN translation of FEATURE_MATRIX.md content. Source: EN. |
| CREATE | (new) | `docs/en/reference/feature-matrix.md` | EN: migrate full content from `FEATURE_MATRIX.md`. Canonical source is EN. |
| CREATE | (new) | `docs/zh-CN/reference/migration-history.md` | CN translation of MIGRATION_NOTES.md content. Source: EN. |
| CREATE | (new) | `docs/en/reference/migration-history.md` | EN: migrate full content from `MIGRATION_NOTES.md`. Canonical source is EN. |
| REWRITE | `DEVELOPMENT.md` | (root stub) | Thin stub: audience, 1-sentence description, link to `docs/en/developer/setup.md` |
| REWRITE | `ARCHITECTURE_BASELINE.md` | (root stub) | Thin stub: note it's historical, link to `docs/en/developer/architecture-baseline.md` |
| REWRITE | `FEATURE_MATRIX.md` | (root stub) | Thin stub: audience, description, link to `docs/en/reference/feature-matrix.md` |
| REWRITE | `MIGRATION_NOTES.md` | (root stub) | Thin stub: description, link to `docs/en/reference/migration-history.md` |
| UPDATE | `docs/zh-CN/developer/index.md` | (update links) | Add real links to setup.md, architecture.md, architecture-baseline.md |
| UPDATE | `docs/en/developer/index.md` | (update links) | Mirror update |
| UPDATE | `docs/zh-CN/reference/index.md` | (update links) | Add real links to feature-matrix.md, migration-history.md |
| UPDATE | `docs/en/reference/index.md` | (update links) | Mirror update |

**Content mapping detail for architecture.md (extracted from AGENTS.md):**
- From AGENTS.md §4 (architecture overview diagram) -- full extraction
- From AGENTS.md §8 (GUI/CLI relationship) -- full extraction
- From AGENTS.md §11 (tech debt) -- full extraction
- From AGENTS.md §12 (module map: what to edit for what task) -- full extraction
- From AGENTS.md §5 (current truth chain) -- partial: stage pipeline description
- AGENTS.md §3 (reading order) -- kept in AGENTS.md (it's AI handoff, not developer reference)

**Acceptance criteria:**
- [ ] `docs/zh-CN/developer/` has: index.md, setup.md, architecture.md, architecture-baseline.md
- [ ] `docs/en/developer/` has full mirrors of all above
- [ ] `docs/zh-CN/reference/` has: index.md, feature-matrix.md, migration-history.md
- [ ] `docs/en/reference/` has full mirrors of all above
- [ ] Root stubs (DEVELOPMENT.md, ARCHITECTURE_BASELINE.md, FEATURE_MATRIX.md, MIGRATION_NOTES.md) are brief (under 500 bytes each), state audience, describe what the file was, and link to the authoritative `docs/` page
- [ ] AGENTS.md still contains its full original content (extraction was a copy, not a move -- it gets rewritten in Phase 3)
- [ ] CN and EN developer/reference pages are full mirrors
- [ ] No links broken in the new docs/ pages

**Dependencies:** Phase 1 complete (directories and index pages exist)

---

### Phase 3: Migrate AI Handoff + Runtime Truth Content

**Scope:** Split `AGENTS.md` into AI handoff content (extracted to `docs/*/ai/`) and runtime truth content (extracted to `docs/*/runtime/`). Migrate `TRUTH_SOURCES.md` into `docs/*/runtime/`. Create final AGENTS.md root stub as a **fat stub (~2-3KB)** that preserves the full narrative reading order with brief section summaries. Create TRUTH_SOURCES.md root stub.

**The fat stub rationale:** The Architect's steelman antithesis identified that splitting AGENTS.md into 5+ pages destroys its "3-5 minute single-page handoff" value. The fat stub pattern resolves this: the AGENTS.md root stub preserves the complete reading order (§3) with a 1-2 sentence summary per item plus links to the full docs/ pages. A fresh AI agent reading this single file still gets the complete mental model -- the project identity, the data-to-document pipeline, the module map, and where to look for what -- without needing to follow links. The docs/ pages provide expanded detail for deep dives.

**Intra-phase ordering (CRITICAL — AGENTS.md split is the highest-risk operation):**
1. Create all `docs/` content pages first (extract from AGENTS.md, preserving original — AGENTS.md at root stays intact)
2. Create `docs/` runtime pages (from TRUTH_SOURCES.md + AGENTS.md extracts)
3. Verify all extracted content (diff-check against originals) before rewriting any root file
4. Rewrite AGENTS.md as fat stub
5. Rewrite TRUTH_SOURCES.md as thin stub
6. Update index pages with resolved links

**File operations (~18 files):**

| Action | Source | Destination | Notes |
|--------|--------|-------------|-------|
| CREATE | (from AGENTS.md §1-3,§9,§10,§14) | `docs/zh-CN/ai/handoff.md` | CN: AI handoff context -- doc split, project summary, reading order, config system, capabilities, conclusion. Rewritten to remove developer/runtime content already extracted in Phase 2. Source language: CN (AGENTS.md is Chinese-primary). |
| CREATE | (from AGENTS.md §1-3,§9,§10,§14) | `docs/en/ai/handoff.md` | EN translation of ai/handoff.md |
| CREATE | (from AGENTS.md + SKILL.md) | `docs/zh-CN/ai/skill.md` | CN: Codex/OMX skill documentation. Content adapted from `.codex/skills/auto-generate-orchestrator/SKILL.md` for readability. |
| CREATE | (from AGENTS.md + SKILL.md) | `docs/en/ai/skill.md` | EN mirror |
| CREATE | (from AGENTS.md §5.5,§6.4,§7) | `docs/zh-CN/ai/runtime-bridge.md` | CN: AI-native runtime bridge docs -- source_bundle, stage_trace, workspace integration |
| CREATE | (from AGENTS.md §5.5,§6.4,§7) | `docs/en/ai/runtime-bridge.md` | EN mirror |
| CREATE | (from TRUTH_SOURCES.md + AGENTS.md §5-7) | `docs/zh-CN/runtime/truth-sources.md` | CN: Merged runtime truth -- canonical artifacts per stage, compatibility projections, data contracts. Source for TRUTH_SOURCES.md content: EN. |
| CREATE | (from TRUTH_SOURCES.md + AGENTS.md §5-7) | `docs/en/runtime/truth-sources.md` | EN: canonical source. |
| CREATE | (from TRUTH_SOURCES.md §deprecated) | `docs/zh-CN/runtime/compatibility.md` | CN: Compatibility paths, deprecated APIs, removal timeline |
| CREATE | (from TRUTH_SOURCES.md §deprecated) | `docs/en/runtime/compatibility.md` | EN: canonical source |
| CREATE | (from AGENTS.md §7+TRUTH_SOURCES.md) | `docs/zh-CN/runtime/workspace-layout.md` | CN: Job workspace structure, output directories, artifact registry |
| CREATE | (from AGENTS.md §7+TRUTH_SOURCES.md) | `docs/en/runtime/workspace-layout.md` | EN mirror |
| REWRITE | `AGENTS.md` | (root fat stub ~2-3KB) | Fat stub preserving the §3 reading order with brief summaries. Structure: (1) project identity statement, (2) the 18-item reading order with 1-2 sentence description per item + link to expanded docs/ page, (3) pointer to docs/en/ai/handoff.md as full handoff document. |
| REWRITE | `TRUTH_SOURCES.md` | (root stub ~500B) | Thin stub: states audience, links to `docs/en/runtime/truth-sources.md` |
| UPDATE | `docs/zh-CN/ai/index.md` | (update links) | Ensure links to handoff.md, skill.md, runtime-bridge.md resolve |
| UPDATE | `docs/en/ai/index.md` | (update mirror links) | Mirror |
| UPDATE | `docs/zh-CN/runtime/index.md` | (update links) | Ensure links to truth-sources.md, compatibility.md, workspace-layout.md resolve |
| UPDATE | `docs/en/runtime/index.md` | (update mirror links) | Mirror |

**AGENTS.md fat stub specification:**

The fat stub preserves the following structure in a single file (~2-3KB):

```markdown
# AGENTS.md

> AI agent and developer handoff. This file preserves the essential mental model.
> For expanded detail on any topic, follow the link to docs/en/ai/ or docs/zh-CN/ai/.

## Project Identity
[One-paragraph summary of what this project is and does -- from AGENTS.md §2]

## Recommended Reading Order
[A condensed version of AGENTS.md §3, each item with 1-2 sentence summary and link:]
1. AGENTS.md (this file -- concise handoff)
2. TRUTH_SOURCES.md -> docs/en/runtime/truth-sources.md: Runtime truth ...
3. FEATURE_MATRIX.md -> docs/en/reference/feature-matrix.md: Feature status ...
[continue for all 18 items, each with a docs/ link where content migrated]

## Where to find everything
- AI agent handoff: docs/en/ai/handoff.md | docs/zh-CN/ai/handoff.md
- Architecture & module map: docs/en/developer/architecture.md
- Runtime truth: docs/en/runtime/truth-sources.md
- Capabilities list & conclusion: docs/en/ai/handoff.md
```

**Content split detail for AGENTS.md:**
- **Stays in AGENTS.md fat stub:** Document split overview (§1 condensed), project summary (§2), recommended reading order (§3 with summaries), capabilities highlights (§10 condensed), conclusion pointer (§14 reference)
- **Moved to docs/ai/handoff.md:** Full §1, §2, §3, §10, §14 content as stand-alone handoff document
- **Already extracted in Phase 2:** Architecture diagram (§4), GUI/CLI relationship (§8), tech debt (§11), module map (§12) -- now in `docs/*/developer/architecture.md`
- **Moved to runtime in Phase 3:** Current truth chain (§5), data contracts (§6), job workspace/output (§7)
- **Kept in ai/handoff.md:** Config system (§9 -- AI agents need API key and provider configuration context at handoff; it does not fit the truth-sources/workspace-layout/compatibility categories in runtime/)
- **Removed (redundant with READMEs):** Quick start CLI commands (§13)

**Acceptance criteria:**
- [ ] `docs/zh-CN/ai/` has: index.md, handoff.md, skill.md, runtime-bridge.md
- [ ] `docs/en/ai/` has full mirrors of all above
- [ ] `docs/zh-CN/runtime/` has: index.md, truth-sources.md, compatibility.md, workspace-layout.md
- [ ] `docs/en/runtime/` has full mirrors of all above
- [ ] AGENTS.md root fat stub is ~2-3KB, preserves the full §3 reading order with brief summaries and docs/ links
- [ ] A fresh AI agent reading AGENTS.md stub alone can answer "what is this project and where do I start?" within 3-5 minutes
- [ ] TRUTH_SOURCES.md root stub is concise (< 500B), links to `docs/en/runtime/truth-sources.md`
- [ ] All content from original AGENTS.md and TRUTH_SOURCES.md is preserved somewhere in `docs/` (no information loss)
- [ ] CN and EN pages are full mirrors
- [ ] SKILL.md at `.codex/skills/auto-generate-orchestrator/` is NOT modified (it's a functional skill file, not documentation to migrate)

**Dependencies:** Phase 2 complete (developer content extracted from AGENTS.md)

---

### Phase 4: Finalize Root READMEs + CI + Verification

**Scope:** Polish `README.md` into the final concise bilingual product homepage with navigation links aligned to the new `docs/` structure. Update `README.zh-CN.md` and `README.en.md` link references. Add bilingual CI parity check script. Perform full verification pass including cross-reference tracing and expanded test plan.

**File operations (~10 files):**

| Action | File | Description |
|--------|------|-------------|
| REWRITE | `README.md` | Polish to final form. Keep existing bilingual structure. Align all nav links in the "Pick Your Path" table and "Documentation Boundaries" section to point to correct `docs/` paths (instead of root files that are now stubs). Ensure tone is "concise product homepage, not long internal manual." |
| UPDATE | `README.zh-CN.md` | Update §16 (排障快捷表 / troubleshooting) and §17 (文档分工 / doc split) links: point developer/runtime references to `docs/zh-CN/` instead of root stubs. The user-facing content (sections 1-15) stays unchanged. |
| UPDATE | `README.en.md` | Update §1 (doc split) and §10 (troubleshooting) links: point developer/runtime references to `docs/en/` instead of root stubs. The user-facing content (sections 2-9, 11-12) stays unchanged. |
| UPDATE | `docs/zh-CN/index.md` | Ensure all cross-reference links resolve correctly after Phase 2-3 migrations. |
| UPDATE | `docs/en/index.md` | Mirror update. |
| CREATE | `scripts/check-docs-parity.sh` | CI parity check script (see Bilingual Maintenance Strategy, Section 6). |
| VERIFY | (all files) | Full link-check pass: every relative link in every file must resolve to an existing file. |
| VERIFY | (all files) | Content ownership pass: no developer/AI/runtime content beyond short nav links in root README files. |
| VERIFY | (all files) | Bilingual parity pass: every `docs/zh-CN/` file has a matching `docs/en/` counterpart with equivalent content depth. |
| VERIFY | (cross-ref) | AGENTS.md §3 reading order cross-reference trace (see Section 9.5). |

**README.md polish specification:**
- Keep the current bilingual structure (Chinese + English interleaved)
- The "Why This Project Exists" section stays (it's the product pitch)
- The "Pick Your Path" table gets updated links:
  - AI agent / maintainer row links to `./AGENTS.md` (which is now a fat stub with reading order and docs/ links)
  - Debugging row links to `./TRUTH_SOURCES.md` (stub linking to `docs/en/runtime/truth-sources.md`)
  - Feature status row links to `./FEATURE_MATRIX.md` (stub linking to `docs/en/reference/feature-matrix.md`)
  - Optionally add a row: "Browsing full documentation" linking to `./docs/en/index.md`
- The "Documentation Boundaries" section gets updated to reflect the new split (root vs docs/)
- Remove any content that duplicates `docs/` material (keep only the minimum needed for a homepage)
- Target length: similar to current (~5KB), not longer

**Acceptance criteria:**
- [ ] README.md is a concise bilingual product homepage (not a long internal manual)
- [ ] All navigation links in README.md resolve correctly
- [ ] README.zh-CN.md and README.en.md remain complete user guides with updated doc-split links
- [ ] No developer/AI/runtime truth content remains in root README bodies beyond short nav links
- [ ] All root convention stubs point clearly to authoritative `docs/` pages
- [ ] Every `docs/zh-CN/` file has a full-content mirror in `docs/en/`
- [ ] Zero broken internal links across all files (manual walkthrough + grep for `](./` patterns)
- [ ] `scripts/check-docs-parity.sh` exists and runs successfully
- [ ] Cross-reference trace for AGENTS.md §3 reading order passes (see Section 9.5)
- [ ] New user can understand project advantage and pick GUI/CLI from README.md within ~5 min

**Dependencies:** Phases 1-3 complete (all content migrated, all stubs created)

---

## 5. Complete File Mapping

For each current root MD file, exactly what happens:

### README.md
- **Disposition:** STAYS AT ROOT. Polished in Phase 4.
- **Content changes:** Navigation links updated to point to `docs/` paths. "Documentation Boundaries" section rewritten to reflect new split. Minor polish for conciseness.
- **Content preserved:** "Why This Project Exists", "Pick Your Path" table structure, "What It Does", "Quick Start", pipeline diagram.

### README.zh-CN.md
- **Disposition:** STAYS AT ROOT. Minor link updates in Phase 4 only.
- **Content changes:** §16 (排障快捷表 / troubleshooting) links updated. §17 (文档分工 / doc split) references updated.
- **Content preserved:** All 18 sections remain. User-facing content (sections 1-15) untouched.

### README.en.md
- **Disposition:** STAYS AT ROOT. Minor link updates in Phase 4 only.
- **Content changes:** §1 (doc split) links updated. §10 (troubleshooting) references updated.
- **Content preserved:** All 12 sections remain. User-facing content (sections 2-9, 11-12) untouched.

### AGENTS.md
- **Disposition:** REWRITTEN as fat root stub (~2-3KB) in Phase 3.
- **Content migration:**
  - §1-3, §10, §14 (AI handoff) → `docs/zh-CN/ai/handoff.md` + `docs/en/ai/handoff.md`
  - §4, §8, §11, §12 (architecture, tech debt, module map) → `docs/zh-CN/developer/architecture.md` + `docs/en/developer/architecture.md` (Phase 2)
  - §5-7 (truth chain, data contracts, workspace) → merged into `docs/*/runtime/truth-sources.md` and `docs/*/runtime/workspace-layout.md` (Phase 3)
  - §9 (config system) → kept in `docs/zh-CN/ai/handoff.md` + `docs/en/ai/handoff.md` (Phase 3)
  - §13 (CLI quick start) → removed (redundant with README files)
- **Root fat stub content:** Project identity statement, full §3 reading order preserved with 1-2 sentence summaries per item and links to expanded docs/ pages, plus centralized "Where to find everything" directory.

### TRUTH_SOURCES.md
- **Disposition:** REWRITTEN as thin root stub (~500B) in Phase 3.
- **Content migration:**
  - Stage truth sources (§1-7) → `docs/zh-CN/runtime/truth-sources.md` + `docs/en/runtime/truth-sources.md`
  - Compatibility projections → `docs/zh-CN/runtime/compatibility.md` + `docs/en/runtime/compatibility.md`
  - Deprecated paths + removal timeline → merged into compatibility.md
  - Testing and validation section → split: runtime parts stay in runtime/, developer parts referenced from developer/index.md
- **Root stub content:** Audience, description, link to `docs/en/runtime/truth-sources.md`.

### ARCHITECTURE_BASELINE.md
- **Disposition:** REWRITTEN as thin root stub (~400B) in Phase 2.
- **Content migration:** Full content → `docs/zh-CN/developer/architecture-baseline.md` + `docs/en/developer/architecture-baseline.md`
- **Root stub content:** States this is a historical baseline document. Links to `docs/en/developer/architecture-baseline.md`. Notes that `AGENTS.md` and `docs/en/developer/architecture.md` are the current architecture truth.

### DEVELOPMENT.md
- **Disposition:** REWRITTEN as thin root stub (~400B) in Phase 2.
- **Content migration:** Full content → `docs/zh-CN/developer/setup.md` + `docs/en/developer/setup.md`
- **Root stub content:** Audience (contributors), description, link to `docs/en/developer/setup.md`.

### FEATURE_MATRIX.md
- **Disposition:** REWRITTEN as thin root stub (~400B) in Phase 2.
- **Content migration:** Full content → `docs/zh-CN/reference/feature-matrix.md` + `docs/en/reference/feature-matrix.md`
- **Root stub content:** Audience (maintainers, AI agents), description, link to `docs/en/reference/feature-matrix.md`.

### MIGRATION_NOTES.md
- **Disposition:** REWRITTEN as thin root stub (~400B) in Phase 2.
- **Content migration:** Full content → `docs/zh-CN/reference/migration-history.md` + `docs/en/reference/migration-history.md`
- **Root stub content:** Description, link to `docs/en/reference/migration-history.md`.

### prompts/README.md
- **Disposition:** OUT OF SCOPE. This file (~2KB) contains runtime prompt templates used by the pipeline, not documentation to migrate. No changes needed.
- **Content preserved:** Entire file as-is. It is a functional runtime asset, not end-user or developer documentation.

### .codex/skills/auto-generate-orchestrator/SKILL.md
- **Disposition:** NOT MODIFIED. This is a functional Codex skill file loaded by the orchestrator runtime. Its content is adapted into `docs/*/ai/skill.md` for readability, but the source file is untouched.

---

## 6. Bilingual Maintenance Strategy

### Canonical Language Direction

Each source document has a designated canonical language. Translations are derived from the canonical.

| Source Document | Canonical Language | Direction |
|-----------------|-------------------|-----------|
| **AGENTS.md** | Chinese (zh-CN) | CN pages in docs/ are authoritative for AGENTS.md-extracted content. EN pages are translations. |
| **TRUTH_SOURCES.md** | English (en) | EN pages in docs/ are authoritative for TRUTH_SOURCES.md-extracted content. CN pages are translations. |
| **FEATURE_MATRIX.md** | English (en) | EN is canonical. CN is translation. |
| **DEVELOPMENT.md** | English (en) | EN is canonical. CN is translation. |
| **ARCHITECTURE_BASELINE.md** | English (en) | EN is canonical. CN is translation. |
| **MIGRATION_NOTES.md** | English (en) | EN is canonical. CN is translation. |
| **README.md / README.zh-CN.md / README.en.md** | Mixed | README.md is bilingual. README.zh-CN.md is CN canonical. README.en.md is EN canonical. Neither is a translation of the other. |

### Translation Workflow

1. **During migration (Phases 1-3):** Executor creates the canonical-language page first, then translates to the non-canonical language. Translation quality bar: technical accuracy (correct terminology, equivalent section structure, equivalent content coverage). Not marketing-grade literary quality.
2. **Post-migration updates:** When updating a docs/ page, update the canonical-language page first and mark the mirror page with `<!-- TODO: sync with [canonical-page] after [date] -->` if the translation cannot be updated immediately. This prevents silent drift.
3. **Quality bar:** CN/EN pages must have identical section structure (same heading hierarchy, same section count). Line count may vary due to language differences, but content coverage must be equivalent. A page is NOT a valid mirror if it omits sections present in its counterpart.

### CI Parity Check Script

Added as a Phase 4 deliverable: `scripts/check-docs-parity.sh`

The script enforces:
1. **Page count parity:** `docs/zh-CN/` and `docs/en/` must contain exactly the same set of `.md` files (modulo the `zh-CN/` vs `en/` prefix).
2. **Section structure parity:** Each CN/EN page pair must have the same number of markdown headings (h2+h3) and the same heading text (translated).
3. **Staleness detection:** Scans for `<!-- TODO: sync with` markers to flag out-of-date translations.
4. **Exit code:** Non-zero if any check fails, suitable for CI gating.

```bash
#!/usr/bin/env bash
# check-docs-parity.sh — CI check for CN/EN docs/ mirror parity
# Exit 0 = parity OK, Exit 1 = drift detected

set -euo pipefail

DRIFT=0

# 1. File count parity
CN_FILES=$(find docs/zh-CN -name '*.md' | sed 's|docs/zh-CN/||' | sort)
EN_FILES=$(find docs/en -name '*.md' | sed 's|docs/en/||' | sort)

if ! diff -q <(echo "$CN_FILES") <(echo "$EN_FILES") > /dev/null; then
  echo "FAIL: CN/EN file count mismatch"
  diff <(echo "$CN_FILES") <(echo "$EN_FILES") || true
  DRIFT=1
fi

# 2. Section count parity (per file pair)
for cn_file in $CN_FILES; do
  cn_headings=$(grep -c '^##' "docs/zh-CN/$cn_file" 2>/dev/null || echo 0)
  en_headings=$(grep -c '^##' "docs/en/$cn_file" 2>/dev/null || echo 0)
  if [ "$cn_headings" != "$en_headings" ]; then
    echo "FAIL: Section count mismatch in $cn_file (CN:$cn_headings EN:$en_headings)"
    DRIFT=1
  fi
done

# 3. Staleness markers
STALE=$(grep -rl '<!-- TODO: sync with' docs/ 2>/dev/null || true)
if [ -n "$STALE" ]; then
  echo "WARN: Stale translation markers found:"
  echo "$STALE"
  # Warning only, not a hard fail
fi

exit $DRIFT
```

---

## 7. Risk Assessment

### Risk 1: Broken internal links
- **Likelihood:** Medium. ~33 files with cross-references being rewritten.
- **Impact:** High. AI agents and users following broken links lose trust.
- **Mitigation:**
  - Each phase includes a link-verification step before proceeding.
  - Phase 4 includes a comprehensive `grep` for all `](./` patterns and resolution check.
  - Root stubs use absolute-relative paths (`./docs/en/...`) that are easy to verify.
  - Stubs never link to content that hasn't been created yet in the current phase.

### Risk 2: Information loss during AGENTS.md split
- **Likelihood:** Low-Medium. AGENTS.md is 14KB with mixed content categories.
- **Impact:** High. Developer/AI context lost reduces onboarding speed.
- **Mitigation:**
  - The AGENTS.md split is a copy-first-then-stub operation. Original content is preserved in git history.
  - Content mapping table (Section 5 above) specifies exactly which AGENTS.md sections go where.
  - Verification step: diff original AGENTS.md sections against their docs/ destinations to confirm no loss.

### Risk 3: Bilingual drift (CN/EN mirrors diverge)
- **Likelihood:** Medium. Manual translation step for new CN pages from EN originals (and vice versa).
- **Impact:** Medium. Acceptance criterion #4 fails.
- **Mitigation:**
  - CN pages are created as translations following canonical language direction (Section 6).
  - Phase 4 CI parity check enforces page count and section structure parity.
  - `<!-- TODO: sync with -->` markers make staleness visible.
  - Section 6 documents the canonical direction table and translation workflow.

### Risk 4: Tooling that hardcodes root file paths
- **Likelihood:** Low. No known tooling hardcodes these paths beyond convention-based discovery.
- **Impact:** Low-Medium. If some CI or agent script expects content in root AGENTS.md, it gets a fat stub (still informative).
- **Mitigation:**
  - The AGENTS.md fat stub preserves enough context that tooling receiving stub content still gets useful information.
  - Stubs preserve the file name and a clear pointer to the authoritative docs/ page.
  - AI agents reading stubs get explicit instructions where to find full content.

### Risk 5: README.md finalization scope creep
- **Likelihood:** Medium. "Polish to concise product homepage" is subjective.
- **Impact:** Low. Can iterate in follow-up PRs.
- **Mitigation:**
  - Phase 4 has explicit polish specification (keep existing sections, update links, remove duplicates, target ~5KB).
  - The deep-interview draft is the starting point, not a from-scratch rewrite.
  - Acceptance criterion is functional: new user understands project and picks entry within 5 min.

### Risk 6: Link rot at scale (NEW)
- **Likelihood:** Medium. 33 pages with cross-references means ~60-80 internal links to maintain.
- **Impact:** Medium. Broken links erode trust faster than missing content.
- **Mitigation:**
  - Phase 4 includes deterministic link verification: extract all `](./` patterns, resolve each against the file tree.
  - CI parity script can be extended to validate internal links on each commit.
  - Index pages act as link hubs; broken links there have the highest blast radius and are checked first.

### Risk 7: Searchability regression (NEW)
- **Likelihood:** High. No more single `grep *.md` at root to find content across all documentation.
- **Impact:** Medium. Maintainers accustomed to single-grep discovery will need to search `docs/` + root.
- **Mitigation:**
  - Document the new grep pattern: `grep -r "pattern" docs/ *.md` for root + docs search.
  - Consider adding a `docs/CONTRIBUTING.md` that explains search patterns for the new structure.
  - GitHub's built-in search spans the whole repo; `grep` at the repo root still works.

### Risk 8: Git history fragmentation (NEW)
- **Likelihood:** Certain. Content moves from files with long histories (AGENTS.md: many commits) to new files with no history.
- **Impact:** Low. Git blame on docs/ pages won't show pre-migration authorship.
- **Mitigation:**
  - Root stubs preserve the original file path for `git log --follow` queries.
  - Migration commit messages cite source file names so `git log --grep` can trace provenance.
  - This is an inherent cost of file reorganization, not unique to this plan.

### Risk 9: Thin stub degradation over time (NEW)
- **Likelihood:** Medium. Over time, stubs may accumulate stale links if docs/ pages are reorganized.
- **Impact:** Low-Medium. Stale stubs are worse than no stub (actively misleading).
- **Mitigation:**
  - CI parity script can validate stub links as a future enhancement.
  - AGENTS.md fat stub's reading order links are less likely to go stale because they point to section-level pages (architecture, truth-sources) rather than micro-pages.
  - Fat stub content (project identity, reading order) changes very slowly.

### Risk 10: External reference impact (NEW)
- **Likelihood:** Low-Medium. DeepWiki, blog posts, bookmarks may reference specific root MD section anchors.
- **Impact:** Low. Root stubs preserve the file path. Section anchors within AGENTS.md sections that moved to docs/ will 404.
- **Mitigation:**
  - The fat stub includes section references where possible.
  - AGENTS.md stub explicitly states "Expanded content moved to docs/" so any external visitor understands the redirect.
  - If specific external references are discovered post-migration, anchor redirects can be added to stubs.

---

## 8. Pre-Mortem: Three Failure Scenarios

### Scenario A: "The links all work but the content is hollow"
The migration mechanically moves text but loses the narrative coherence. AGENTS.md split creates disjunct pages that don't flow. AI agents reading `docs/en/ai/handoff.md` get fragmented bullet points instead of the original cohesive handoff document.
- **Prevention:** When splitting AGENTS.md, preserve surrounding context sentences, not just the extracted sections. Each target page should read as a self-contained document with its own introduction and conclusion. The fat stub at root preserves the full reading order so narrative coherence is never lost at the entry point.
- **Detection:** Read each new docs/ page end-to-end before signing off Phase 3. Also read the AGENTS.md fat stub as a fresh AI agent to verify the mental model is complete.

### Scenario B: "The Chinese mirrors are summaries in practice"
To save time, the executor creates CN pages that are shorter, less detailed versions of the EN pages, violating the "full mirror" requirement.
- **Prevention:** Explicit acceptance criterion: CN pages must match EN pages in section count and substantive content. Phase 4 verification explicitly checks this. CI parity script (Section 6) provides ongoing enforcement.
- **Detection:** Compare heading counts and section structures between CN/EN pairs using `scripts/check-docs-parity.sh`. Any significant discrepancy flags a mirror gap.

### Scenario C: "The AGENTS.md fat stub is still too thin and breaks AI onboarding"
AGENTS.md fat stub reduces the reading order to one-line links with no context. An AI agent gets section titles but no understanding of WHY each document matters or HOW the pieces fit together.
- **Prevention:** The fat stub specification requires 1-2 sentence summaries per reading order item, not just link lists. Each summary must convey what the target document provides and why it matters in the mental model.
- **Detection:** Read the AGENTS.md stub as if you are a fresh AI agent. Can you answer "what is this project, what pipeline does it run, and where do I look to understand X?" from the stub alone, without following any links? If not, the summaries are too thin.

---

## 9. Verification Checklist

### 9.1 Per-Phase Verification

**Phase 1:**
- [ ] All 10 index.md files exist and are valid markdown
- [ ] All directory paths exist: `docs/zh-CN/{developer,ai,runtime,reference}/` and `docs/en/{developer,ai,runtime,reference}/`
- [ ] No `docs/*/user/` directories exist
- [ ] CN and EN index.md pairs have matching section structures

**Phase 2:**
- [ ] All 10 new docs/ content files exist and are valid markdown
- [ ] All 4 root stubs are under 500 bytes each
- [ ] Root stubs contain: audience, description, link to authoritative docs/ page
- [ ] AGENTS.md is untouched (still has full original content)
- [ ] No broken links in new docs/ pages

**Phase 3:**
- [ ] All 12 new docs/ content files exist and are valid markdown
- [ ] AGENTS.md root fat stub is 2-3KB, preserves full §3 reading order with summaries and links
- [ ] AGENTS.md fat stub: a fresh reader can answer "what is this project and where do I start?" from the stub alone
- [ ] TRUTH_SOURCES.md root stub is under 500B
- [ ] Original AGENTS.md content is fully accounted for in docs/ (no sections dropped)
- [ ] Original TRUTH_SOURCES.md content is fully accounted for in docs/
- [ ] SKILL.md at `.codex/skills/auto-generate-orchestrator/` is unmodified

**Phase 4:**
- [ ] README.md is concise (< 6KB), bilingual, has correct nav links
- [ ] README.zh-CN.md §16-17 links updated, user content unchanged
- [ ] README.en.md §1, §10 links updated, user content unchanged
- [ ] `scripts/check-docs-parity.sh` exists, is executable, and runs with exit code 0

### 9.2 Final Acceptance Criteria (mapped to spec)

| # | Criterion | How to verify |
|---|-----------|---------------|
| 1 | New user understands project and picks GUI/CLI within ~5 min | Read README.md as a fresh user. Can you answer "what does this do, why should I use it, and how do I start?" from the homepage alone? |
| 2 | Power user finds complete coverage of all features | Trace each feature from README.zh-CN.md section headers: input modes, commands, config, artifacts, validation, repair, recovery. All present. |
| 3 | AI agent/developer finds architecture, truth, module maps under docs/ | Open AGENTS.md fat stub, read the reading order. Within the stub itself, understand enough to navigate. Follow links: within 3 clicks, reach architecture overview, module map, truth sources, workspace layout. |
| 4 | CN/EN docs are full mirrors | For each `docs/zh-CN/*.md`, verify `docs/en/*.md` exists with equivalent section count and content depth. Run `scripts/check-docs-parity.sh`. |
| 5 | Root README.md is a concise product homepage | README.md is under 6KB. Contains no developer-only content (module maps, data contracts, tech debt). Contains no AI-agent-only content (Codex skill internals, stage traces). |
| 6 | README.zh-CN.md and README.en.md remain complete user guides | Diff against pre-migration versions. Only §16 (troubleshooting) and §17 (doc split) in CN, and §1 (doc split) and §10 (troubleshooting) in EN should differ. All user-facing instructional content intact. |
| 7 | Root convention stubs point clearly to docs/ | For each stub (AGENTS.md, TRUTH_SOURCES.md, FEATURE_MATRIX.md, DEVELOPMENT.md, ARCHITECTURE_BASELINE.md, MIGRATION_NOTES.md): file exists, contains a link to a valid docs/ target. AGENTS.md is the exception at 2-3KB (fat stub); all others under 1KB. |
| 8 | Final structure is maintainable with clear content ownership | Each docs/ section has an index.md describing what belongs there. Writer can answer "where does new content X go?" by reading the section index. No docs/user/ ambiguity. |

### 9.3 Automated Checks

```bash
# Verify all internal links resolve
grep -rohP '\]\(\./[^)]+\)' docs/ README*.md AGENTS.md TRUTH_SOURCES.md FEATURE_MATRIX.md DEVELOPMENT.md ARCHITECTURE_BASELINE.md MIGRATION_NOTES.md | sort | uniq

# Verify CN/EN file parity
diff <(find docs/zh-CN -name '*.md' | sed 's|docs/zh-CN/||' | sort) <(find docs/en -name '*.md' | sed 's|docs/en/||' | sort)

# Verify root stubs are thin (AGENTS.md exception: 2-3KB; others: under 1000 bytes)
for f in AGENTS.md TRUTH_SOURCES.md FEATURE_MATRIX.md DEVELOPMENT.md ARCHITECTURE_BASELINE.md MIGRATION_NOTES.md; do wc -c < "$f"; done

# Run bilingual parity CI script
bash scripts/check-docs-parity.sh
```

### 9.4 Expanded Test Plan (Deliberate Mode)

#### Unit-Level: Per-File Content Integrity

For each new docs/ page created in Phases 2-3:
1. **Section count check:** Page has at least 2 sections (introduction + body). No page is a single continuous block without headings.
2. **Link validity:** Every `[text](./path)` link in the page resolves to an existing file (relative to the page's location).
3. **Frontmatter/metadata:** Page has a clear h1 title matching its `index.md` reference.
4. **Source traceability:** Each page cites its source document and sections (e.g., "Source: AGENTS.md §4, §8, §11-12") so content provenance is never lost.

#### Integration-Level: Cross-File Narrative Coherence After AGENTS.md Split

1. **Reading order follow-through:** Starting from each item in the AGENTS.md fat stub reading order, follow the docs/ link. Verify the linked page delivers what the summary promised.
2. **No orphan sections:** Every AGENTS.md section number (§1-§14) is accounted for in either: (a) the AGENTS.md fat stub, (b) a docs/developer/ page, (c) a docs/ai/ page, (d) a docs/runtime/ page, or (e) explicitly removed with rationale (§13).
3. **TRUTH_SOURCES.md coverage:** Every truth source table from the original TRUTH_SOURCES.md appears in `docs/*/runtime/truth-sources.md`. Compatibility projections appear in `docs/*/runtime/compatibility.md`.
4. **No dead-end indices:** Every link listed in a `docs/*/developer/index.md` or similar index page resolves to an existing page within that section.

#### E2E: Fresh-AI-Agent Simulation

Simulate a fresh AI agent entering the repository for the first time:
1. **Start:** Open AGENTS.md (fat stub). Time how long it takes to understand:
   - What the project does (project identity)
   - The data-to-document pipeline structure
   - Where to look for architecture, truth sources, and module maps
   - Target: complete mental model within 3-5 minutes from this single file
2. **Navigate:** From the fat stub links, reach:
   - `docs/en/developer/architecture.md` (module map + architecture overview)
   - `docs/en/runtime/truth-sources.md` (stage artifacts and data contracts)
   - `docs/en/ai/handoff.md` (full handoff document)
   - Target: reach all three within 3 additional clicks/minutes
3. **Work task:** As the AI agent, answer: "I need to modify the stage-2 outline generation. Which file do I edit?" Verify the module map (in architecture.md) provides this information.

#### Observability: Ongoing Drift Detection

1. **Bilingual mirror drift:** `scripts/check-docs-parity.sh` is the primary observable. Run it. Exit code 0 means parity holds. Non-zero means drift. This is designed to run in CI on every PR that touches `docs/`.
2. **Stale stubs detection:** Periodically (recommended: quarterly or after major docs/ reorganization), grep for all links in root stubs and verify targets still exist. This can be added as a future CI enhancement.
3. **Content ownership boundary drift:** Check root README files for leaked internal content. Automated via: `grep -c '##.*(Stage|模块|Module|Pipeline|Config|Config 系统)' README*.md` -- count should not increase after migration.

### 9.5 Cross-Reference Verification (AGENTS.md §3 Reading Order Trace)

For each of the 18 items in AGENTS.md §3 (recommended reading order), trace the post-migration path:

| # | Original Reference | Post-Migration Path | Expected Content |
|---|---|---|---|
| 1 | AGENTS.md | AGENTS.md (fat stub at root) | Project identity + reading order with summaries |
| 2 | TRUTH_SOURCES.md | TRUTH_SOURCES.md stub -> docs/en/runtime/truth-sources.md | Runtime truth by stage |
| 3 | FEATURE_MATRIX.md | FEATURE_MATRIX.md stub -> docs/en/reference/feature-matrix.md | Feature status matrix |
| 4 | summary_schema.py | Unchanged (source file) | Stage-1 canonical summary schema |
| 5 | services/job_runner.py | Unchanged (source file) | Job workspace / resume / artifact coordinator |
| 6 | main.py | Unchanged (source file) | Compat entry + main orchestration |
| 7 | gui/app.py | Unchanged (source file) | GUI entry point |
| 8 | .codex/skills/.../SKILL.md | Unchanged (functional skill file) + docs/en/ai/skill.md | Codex skill internals |
| 9 | runtime/orchestrator.py | Unchanged (source file) | Runtime orchestration |
| 10 | preprocess/service.py | Unchanged (source file) | Preprocessing service |
| 11-18 | (remaining test/source files) | Unchanged (source files) | Test and service files |

**Verification action:** Confirm each of the 18 items in the post-migration reading order is reachable. For items that now point to docs/ pages (#2, #3, #8), verify the docs/ page exists and contains equivalent content to the original file. For source files (#4-7, #9-18), confirm they are unchanged and the AGENTS.md stub still references them.

---

## 10. Estimated Effort

| Phase | Files Created | Files Modified | Files Rewritten | Content Volume | Estimated Sessions |
|-------|---------------|----------------|-----------------|----------------|--------------------|
| Phase 1: Scaffolding | 10 | 0 | 0 | ~2KB (index pages only, mostly navigation) | 1 (light) |
| Phase 2: Dev + Reference | 10 | 4 | 4 (root stubs) | ~18KB migrated, ~2KB new stubs | 1-2 (medium) |
| Phase 3: AI + Runtime | 12 | 2 | 2 (root stubs) | ~20KB split/migrated, ~3KB AGENTS.md fat stub | 2 (heavy -- AGENTS.md split is the hardest part) |
| Phase 4: Polish + CI + Verify | 1 (CI script) | 5 | 1 (README.md) | ~3KB rewritten, ~1KB CI script, rest is verification | 1-2 (medium) |
| **Total** | **33** | **11** | **7** | **~43KB content, ~5KB stubs + CI** | **5-7 sessions** |

### New docs/ pages summary

| Subdirectory | Page Count (per language) | Source Files |
|---|---|---|
| `{lang}/index.md` | 1 | New content |
| `{lang}/developer/` | 4 pages (index + setup + architecture + architecture-baseline) | AGENTS.md, DEVELOPMENT.md, ARCHITECTURE_BASELINE.md |
| `{lang}/ai/` | 4 pages (index + handoff + skill + runtime-bridge) | AGENTS.md, SKILL.md |
| `{lang}/runtime/` | 4 pages (index + truth-sources + compatibility + workspace-layout) | TRUTH_SOURCES.md, AGENTS.md |
| `{lang}/reference/` | 3 pages (index + feature-matrix + migration-history) | FEATURE_MATRIX.md, MIGRATION_NOTES.md |
| **Total per language** | **16 pages** | |
| **Total (CN+EN)** | **32 pages** | |

Note: Page count reduced from draft's 48 to 32 by eliminating user/ subdirectory (2 pages), consolidating micro-pages in developer/ (was 8, now 4 per language), reducing runtime/ (was 5, now 3 per language), and reducing reference/ (was 6, now 3 per language). AI/ gained one page (handoff.md extracted from index.md for clarity), going from 3 to 4.

Phase 3 is the hardest: splitting AGENTS.md (14KB, mixed audiences) into 3-4 target files while preserving narrative coherence, creating full CN mirrors, and writing the fat stub that preserves the complete mental model -- all require careful editorial judgment.

---

## 11. Rollback Notes

Each phase is an independent git commit. Rollback for any phase:
```bash
git revert <phase-commit-hash>
```

The repository is always in a coherent state at phase boundaries:
- After Phase 1: `docs/` exists with index pages only (4 sections, no user/). Root files untouched. Safe to pause indefinitely.
- After Phase 2: Developer/reference content lives in `docs/`. AGENTS.md still has full content. Root stubs exist for migrated files. Safe.
- After Phase 3: AI/runtime content lives in `docs/`. AGENTS.md is a fat stub (2-3KB). TRUTH_SOURCES.md is a thin stub. Full content preserved in `docs/`. Safe.
- After Phase 4: Migration complete. CI parity script in place. All acceptance criteria met.
