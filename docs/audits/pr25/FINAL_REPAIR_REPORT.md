# R1 final repair implementation report

This branch implements the repair plan against PR #24's hardening head
`bf00852fb627ac0519d96287d2d27b36f17a99ea`.

The current executable code commit is `2627f1ca97a445f8a46d7996bb8665b67a7adcdd`.
The remote branch is `codex/r1-outline-final-repair`; PR #25 is open and
unmerged. PR #24 remains open and unmerged.

## Implemented

- Restored the PR baseline's aggregate budgets, external-host admission,
  source binding, Registry/current-set protections, durable receipt handling,
  and locked dependency surface before applying the R1 changes.
- Added executable `global_navigation`, `topic_synthesis`,
  `cross_group_comparison`, and `global_synthesis` DAG nodes. Production
  external routes execute bounded provider-backed topic/cross/global
  synthesis with immutable response artifacts and receipt closure; internal
  fixture/offline routes retain an explicit deterministic projection and do
  not claim model synthesis.
- Candidate requests now use complete evidence views and content-layer
  references. Navigation cards remain routing data. A single effective input
  cap is enforced before transport: 32,000 tokens, the configured cap, and
  the route capacity, whichever is smaller.
- Relation adjudication now keeps selected, deferred, insufficient-evidence,
  not-comparable, rejected, and confirmed states separate. Year/template
  overlap alone cannot create a substantive contradiction.
- Added a durable selected-candidate revision node with parent/revised hashes,
  bounded revision count, and per-paper boundary replacement for accepted
  aggregate-gap recommendations. Format repair now rejects section count,
  order, duplicate-ID, and identity changes; positional rebinding was removed.
- Unified transport retries with expected receipt `max_attempts`, rejected
  duplicate expected call identities, merged staging receipts by identity,
  and made the stable receipt ledger the replay authority. Older snapshots no
  longer resurrect a receipt missing from the stable ledger.
- Added fail-closed pause state handling and propagated the pause path through
  the runtime provider admission context, including Stage 1/Writer/Validator
  provider runtimes.
- Fixed the hidden timeout hostname override, MinerU page-index JSON
  classification, and retained the PR baseline's binary/ZIP size limits,
  NULL-parent Zotero handling, and terminal MinerU error propagation.
- Added the read-only `reviewctl chunk-plan` command and a real `reviewctl
  pause` control-plane operation.
- Preserved complete author-gap and study-level evidence IDs, made missing
  dossiers fail closed, retained relation decision reasons, and prevented
  shard merges or alias maps from deleting/reindexing paper identities.
- Critique inputs are lossless for semantic text and split only at complete
  section boundaries. Selected-candidate recommendations accept typed issue
  transactions (title, goal, claim and aggregate-claim operations), record
  parent/revised hashes, and run targeted structural/evidence revalidation.
- `chunk-plan` now accepts typed Stage 1 summary manifests, refuses to write
  over any input, and returns a non-zero exit for blocked plans. Coverage
  gates use the selected scope while retaining full/local coverage as
  diagnostics.
- Typed `reuse_summary_files` now bypass source-bundle resolution and PDF
  preprocessing. The imported manifest is verified against its source summary,
  visual evidence, provider closure, and ledger before the zero-transport
  Stage 1 path is accepted.
- Production topic, relation, candidate, and critique requests now use
  explicit Registry-bound evidence projections. Complete semantic fields stay
  in content-layer artifacts; provider inputs carry the relevant complete
  field values plus stable evidence-unit hashes and never silently truncate a
  value. Topic units split only at paper boundaries when the effective input
  cap requires it. Candidate and critique output caps are enforced at both
  planning and transport.
- Semantic provider topic requests now materialize complete dossier units,
  including study units, claim modality, evidence IDs, source locators,
  qualifiers and null findings. A genuinely oversized unit blocks admission
  before paid transport instead of being replaced with a hash-only reference.
- Cross/group/global/candidate cache reuse rehydrates the persisted provider
  outputs and cross-group artifact before constructing downstream requests;
  summary-order stability and route-only replay now use canonical paper order
  and equivalent provider-visible projections.
- Selected-candidate repairs now record typed per-target outcomes
  (`changed`, `already_satisfied`, `unresolved`, or `failed`) and reject a
  recommendation when any required target remains unresolved. The revised
  candidate hash is persisted with a post-revision verification record.
- Semantic provider outputs now reject unknown paper/study/evidence identities
  before persistence, with source claim IDs and synthesized claim IDs kept in
  separate namespaces.
- `reviewctl config-migrate` now removes obsolete GPT route sections, migrates
  their `OutlineModels` references to `Backup_Reader_API`, and removes the
  unsupported Writer fallback key. The user's config was migrated with an
  automatic backup and the ordinary preflight now passes using `.env` fields.

## Verification

Fresh local checks on this code commit:

- Python compileall: PASS.
- Changed-file Ruff `E9,F`: PASS.
- Changed-file Pyright: 0 errors, 0 warnings, 0 informations.
- Semantic execution and full-stability suite: 33 passed.
- Outline replay/invalidation suite: 9 passed.
- Semantic chunking, relations, receipt closure, reviewctl: 31 passed.
- Preprocess service: 35 passed.
- Preprocess platform safety, Zotero attachment resolver, export: 26 passed.
- Runtime/CLI/validation/review generation group: 15 passed.
- Additional focused provider/outline tests: 52 passed.
- Provider routing/replay and current production-chain regressions: 14 passed
  in the latest local run after semantic response replay/closure fixes.
- Typed reuse admission regression: 1 passed; Stage 1 reuse regression group:
  2 passed; changed-file Pyright/Ruff remained clean after the final compact
  projection changes.
- Windows CI failures were reproduced locally and repaired: the invalidation
  and routed replay files now pass locally (`18 passed` combined), and the
  complete dossier request regression passes.
- Config migration regression: `26 passed`; local HTTP 524 evidence regression:
  `1 passed`; positive technical targets 0/24k/32k/50k: `4 passed`.

The authorized R1 execution evidence has two distinct boundaries. The reuse-only
run reused all 63 typed manifests with `STAGE1_AUTHORITY_READY=true` and made
zero Stage 1/MinerU calls. Under the earlier compact projection, the Outline
preflight reached `estimated_provider_calls=21` and a real topic request
reached the configured gateway, which returned Cloudflare HTTP 524. That
attempt is retained as historical external evidence. On the current
executable, complete dossier/study/claim/locator materialization is restored
and large papers split at study/claim boundaries without dropping claim IDs.
The complete R1 plan now estimates 220 semantic topic calls and 240 total
provider calls before the downstream cross/global/review work, exceeding the
authorized 24-call limit, and stops before transport with
`max_provider_calls_exceeded`. This is the correct current internal boundary:
no paid request is made with incomplete evidence, but R1 is not READY until a
valid within-budget execution design or an authorized budget change exists.

The environment-wide `pip check` is now clean after installing `pypdf 6.19.0`
for the active Python 3.13 environment. PDF/DOCX/export focused tests passed
20/20 after installation.

## Remaining acceptance boundaries

- Current R1 is blocked internally by the complete-evidence 32k/24-call
  admission gate. The earlier gateway timeout remains recorded separately.
  Writer, Validator/repair, citation verification, DOCX export, and GUI parity
  therefore remain `NOT_VERIFIED`; no artifact is promoted to
  `canonical_ready`.
- The historical R1 spec was machine-checked read-only: 63 typed
  `stage1_reusable_summary_manifest/v1` files exist and decode with 63 unique
  paper keys. The original `D:\\auto-generate\\config.ini` was migrated by
  the supported `reviewctl config-migrate` command with a timestamped backup;
  ordinary `reviewctl preflight` now passes. Credentials were resolved from
  the authorized `D:\\auto-generate\\.env`; values were not printed or
  committed. A fresh external-host acknowledgement was used for the attempt.
- F1 remains an independent regression corpus and is `UNACCEPTED`; no R1
  evidence is promoted to F1.
- Full-repository Ruff still reports pre-existing unused-import and duplicate
  literal findings in unrelated modules. The changed-file lint surface is
  clean.
- Hosted Windows CI run `36095307505` passed on the same executable SHA
  `2627f1c`; the current branch head after that run contains documentation-only
  matrix updates. The older failure was the typed-manifest reuse path and was
  repaired and reproduced locally before the green run.
- The initial commit emitted a Git maintenance warning about pruned reflog
  objects while committing. The commit and remote read-back succeeded; the
  repository object-health warning should be repaired separately without
  rewriting user history.

## Current defect disposition

| Finding | Current disposition | Evidence boundary |
|---|---|---|
| F01 version identity split | Fixed on this branch | PR24 baseline and PR25 executable SHA are recorded; PRs remain unmerged. |
| F02 planned-only semantic nodes | Partially fixed | Real semantic DAG paths exist; R1 complete-evidence admission blocks before paid execution. |
| F03 plan/request budget mismatch | Fixed fail-closed | Complete request builder is estimated before transport; R1 records a machine-readable budget rejection. |
| F04 truncated navigation used as facts | Partially fixed | Topic requests materialize complete units; later canonical adoption remains unverified for R1. |
| F05 template-polluted grouping | Unverified | No full hostile R1 grouping adjudication was completed. |
| F06 relation proposition mismatch | Fixed in regression scope | Relation comparability tests pass; R1 substantive relation adjudication was not executed. |
| F07 deferred marked rejected | Fixed in regression scope | Decision-state tests pass; R1 live relation results are absent. |
| F08 retry/closure conflict | Fixed in regression scope | Receipt/replay/closure suites pass. |
| F09 repair identity/adoption | Fixed in regression scope | Revision and identity tests pass; no R1 adopted candidate exists. |

The historical R01-R14 findings were not independently re-audited one by one
on the final executable; they remain `UNVERIFIED` unless covered by the fresh
tests above. This prevents historical PASS labels from being reused as current
acceptance evidence.
