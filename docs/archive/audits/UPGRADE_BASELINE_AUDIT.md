# Upgrade Baseline Audit

Audit date: 2026-08-02 (Asia/Shanghai)

Repository: `super-lee-hub/literature-review-generator`

Workspace: `D:\auto-generate`

Upgrade branch: `codex/platform-hardening-outline-v3`

This audit was written before production logic changes for the Outline Intelligence v3 upgrade. It records the repository state observed from Git, the current canonical execution path, the existing foundations that must be preserved, and the gates for the staged migration.

## 1. Repository reality

### Git state

The audit started on `main` after fetching `origin/main`:

| Check | Observed result |
|---|---|
| Baseline commit | `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a` (`Merge reliability upgrade into main`) |
| `origin/main` | Exactly the same commit as the local baseline |
| Baseline divergence | `0` ahead / `0` behind |
| Remote `main` verification | `git ls-remote origin refs/heads/main` returned `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a` |
| Integration branch | Created from the verified `origin/main` as `codex/platform-hardening-outline-v3` |
| Tracked-file worktree | Clean at the time of branch creation |
| Untracked files | `PPH_五份综述_20260731.zip` and `PPH_完整资料包_20260731.zip`; both are user-owned archives and were preserved, not staged or modified |

The historical branch `codex/pipeline-validation-update` still exists. Its current local tip is `76906f6`, its remote tip is `3ab953e`, and its local history is 12 commits ahead of `origin/main` and 0 commits behind. The local branch is one commit ahead of its configured remote. It is not a release baseline and is not being merged wholesale.

The branch contains 76 changed files relative to `origin/main`, approximately 26,563 added and 2,432 deleted lines. The change set includes large PPH-specific scripts and temporary repair utilities as well as changes to runtime and outline code. These facts make selective migration mandatory.

### Baseline checks

The following checks were run before the integration branch was created:

| Command | Result |
|---|---|
| `python -m pytest --collect-only -q` | 1,137 tests collected in 4.54 seconds |
| `python -m compileall -q ai_interface.py config_loader.py config_validator.py main.py setup_wizard.py gui outline preprocess runtime services validation validator.py zotero_parser.py` | Passed |
| `python -m pyright` | Passed: 0 errors, 0 warnings, 0 informations |
| `python -m pytest -q --strict-markers` | Did not complete within the 180-second bounded run; no completion result was returned |
| `python -m pytest -q --strict-markers --ignore=tests/test_gui_playwright.py` | Did not complete within the 300-second bounded run; the timeout is not explained by Playwright alone |

The full-suite result is therefore recorded as a baseline timeout, not as a pass or failure claim. Subsequent gates must use bounded, targeted suites and must report any remaining full-suite limitation explicitly.

## 2. Existing foundations to preserve

The current canonical contracts and implementation already provide the following foundations and must not be redesigned without a demonstrated defect:

- `JobWorkspace` stores real artifacts under `output/<project>__<job_id>/`; `output/<project>/` is a compatibility directory.
- `JobRunner` is the primary local execution boundary. `dispatch_command()` remains a compatibility/parameter entry point.
- `ArtifactRegistry` records artifact identity, dependencies, status, and content hashes; registry operations already have transactional and fail-closed tests.
- `job_outcome_v1.json`, append-only attempt snapshots, and `runtime_stage_terminals/` provide durable lifecycle and stage-terminal evidence.
- `summary_schema.py`, `FIELD_OWNER_REGISTRY`, canonical `*_summaries.json`, paper artifacts, and source identity/reuse services define the Stage 1 truth layer.
- Review persistence already uses registered review drafts and citation manifests. Validation has a canonical `ValidationRunResultV1`, dependency checks, evidence resolution, summary recheck, and repair-plan/apply foundations.
- Stage 1 has a controlled visual-artifact/input-builder path; visual evidence remains supplementary to text and must stay bounded and traceable.
- The existing Outline v2 chain has registered literature map, synthesis flow, candidates, critiques, arbitration, final outline, coverage audit, stage health, and explicit adoption modules.

The upgrade therefore closes and hardens the existing path instead of replacing Workspace, Registry, JobRunner, Stage 1, or the established truth-source model.

## 3. Current main path

The durable path observed in the repository is:

```text
input/spec
  -> JobRunner / JobWorkspace
  -> source_inventory_v1.json and source_bundle.json
  -> Stage 1 canonical summaries and registered paper artifacts
  -> registered Outline v2 artifacts and explicit adoption
  -> review_draft_v2 + citation_manifest_v3
  -> ValidationRunResultV1 and exact Registry dependency closure
  -> job_outcome_v1.json / runtime stage terminals
  -> DOCX, TXT, and other projections
```

The AI-native public surface is currently:

```text
runtime.cli (auto-generate-runtime)
  -> AgentRuntimeRunner
  -> AgentRuntimeBridge
  -> JobRunner / ArtifactRegistry / canonical validators
```

`runtime.cli` currently exposes `run`, `resume`, `status`, `reconcile`, and `migrate-legacy`. It does not yet provide the requested single `reviewctl` control plane with `doctor`, `plan`, `next-action`, node retry, repair, validation, adoption, and export commands.

## 4. Confirmed open gaps

The following gaps are confirmed by the baseline search and current source surface:

1. The legacy client-side TPM/RPM token pool remains in `ai_interface.py` (`RateLimiter` and its default construction). Legacy keys remain in `config.ini`, `config.ini.example`, `services/configuration_service.py`, `config_validator.py`, `setup_wizard.py`, and test fixtures.
2. A single `CanonicalCompletionEvaluator` is not present. Completion/readiness must be consolidated without weakening existing fail-closed `job_outcome`, validation, stage-health, or adoption checks.
3. The current runtime CLI is smaller than the requested Agent control plane; machine-readable `next-action` and safe forbidden-action reporting are not yet a formal public contract.
4. The current outline implementation is Outline v2. The required deterministic `OutlineEvidenceView`, global corpus ledger, multi-view matrix, global relation map, review-intent contract, section evidence packets, node DAG, replay binding, and stability audit are not yet a unified v3 path.
5. Provider capability, context-budget, tiny-output/incomplete detection, and API receipts exist in partial or route-specific forms but are not yet a single provider-neutral runtime contract from which Stage Health is derived.
6. Citation, semantic validation, summary recheck, repair, queue, GUI, export, and forensic attestation have useful foundations but require a closure audit before any claim that the v3 path is complete.

## 5. Selective migration inventory

The experimental branch is evidence and prototype material only. Candidates for selective migration, after contract and test review, are:

- provider error classification, detailed transport metadata, incomplete/tiny-output detection, and fallback provenance from the experimental `ai_interface.py` work;
- `runtime/outline_v2_replay.py`, generalized and renamed as a model-call replay store with full route/model/prompt/input/config/schema bindings;
- `runtime/reconcile.py` enhancements for schema, dependency, hash, READY immutability, Registry revision, quarantine, and formal repair/import transactions;
- stable source identity, alias crosswalks, summary reuse, source hashes, and stale-summary diagnostics;
- ReviewBatch selection/corpus binding, provider-aware concurrency, and completion-manifest behavior.

Nothing is migrated merely because it makes a test pass. Each capability must be extracted into a general service with canonical artifact ownership and a focused regression gate.

## 6. Content that must remain isolated

The following experimental content is not a product entry point and must not be merged wholesale:

- `scripts/pph_*.py`, `scripts/fix_s02_s03.py`, `scripts/fix_s03_ch10.py`, `scripts/run_stage1_direct.py`, `scripts/seed_audit.py`, and `tmp_check_snapshot.py`;
- scripts that know PPH variant names, concrete paper names, fixed job IDs, or hard-coded workspace paths;
- scripts that directly edit Registry JSON, Stage Health, canonical review/citation artifacts, READY artifacts, or final DOCX contents;
- hand-repair and direct-run utilities that cannot be expressed as an audited transaction.

If a forensic utility has retained value, it belongs under `tools/legacy_forensics/`, must declare that it is not canonical production code, must not register artifacts, and must not be called by CI or the main runtime.

## 7. Staged implementation order and gates

Each stage requires an independent commit, focused tests, migration notes, and a passing gate before the next stage:

1. Baseline audit and branch hygiene — this document, clean integration branch, preserved user archives.
2. Legacy token-pool removal and config migration — old keys may warn on read but cannot affect runtime or be emitted by new config.
3. Provider runtime receipts and error taxonomy — formal budgets, capability profiles, receipt persistence, retry/fallback provenance, and fail-closed incomplete output.
4. Agent control plane and completion evaluator — one completion predicate plus status, next-action, doctor, resume, and node-retry contracts.
5. Outline v3 deterministic corpus views — canonical joins, evidence views, complete ledger, matrix, and stable technical sharding.
6. Outline v3 relation map and candidate planning — review intent, global relations, shared evidence, section packets, coverage/stability audits, and explicit adoption.
7. Outline node persistence, replay, and resume — durable node metadata, idempotency, stale replay rejection, and affected-downstream-only recovery.
8. Registry immutability and repair transactions — leases/locks, CAS revision, hash drift quarantine, derived Stage Health, and immutable READY versions.
9. Citation, semantic validation, and repair closure — manifest-first citations, occurrence context, evidence/root-cause separation, mapping-first span-level report-first repairs.
10. Queue, GUI, and cancellation productization — serial outer queue, provider-bounded Stage 1 concurrency, cancellation checkpoints, resume, retry, and evaluator-consistent UI state.
11. Generalize branch prototypes and quarantine scripts — declarative project specs and generic services; no PPH-specific production logic.
12. Export bundle and forensic attestation — declarative bundle closure and read-only status of existing legacy review artifacts.
13. Documentation and full regression — bilingual documentation parity, compatibility notes, static safety scans, targeted fault injection, and final bounded/full-suite report.

The required stop condition for a partial delivery is a complete stage gate. A timeout, `needs_review`, stale hash, incomplete provider receipt, pending node, or missing artifact must remain a blocked/unvalidated state and must never be promoted to success or canonical readiness.

## 8. Required safety invariants

- Do not directly edit `artifact_registry.json`, Stage Health, dependency hashes, adoption state, canonical drafts/manifests, READY artifacts, or DOCX sections.
- Do not use global process termination, workspace deletion, hard-coded attachment/project/job paths, or a legacy CLI shell as a canonical runtime.
- Real artifacts remain inside the active JobWorkspace; compatibility directories are projections only.
- DOCX remains a projection of canonical review draft, citation manifest, and render policy.
- Stage Health is derived from receipts, validators, quality audits, dependency checks, and declared fallback policy.
- Existing review outputs are attested read-only before they can be reused as trusted resume/cache inputs.

## 9. Baseline gate conclusion

The repository is now on the clean integration branch `codex/platform-hardening-outline-v3`, based on the verified `origin/main` commit `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a`. The baseline audit is complete. Production implementation may proceed only through the staged gates above; the full pytest baseline remains an explicitly recorded timeout and is not a completion claim.
