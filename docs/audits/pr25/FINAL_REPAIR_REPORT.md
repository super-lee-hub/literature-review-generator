# R1 final repair implementation report

This branch implements the repair plan against PR #24's hardening head
`bf00852fb627ac0519d96287d2d27b36f17a99ea`.

The current executable code commit is `f9fcf63f3c5f157e0856aadfbfe2618052f8c48a`.
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

## Verification

Fresh local checks on this code commit:

- Python compileall: PASS.
- Changed-file Ruff `E9,F`: PASS.
- Changed-file Pyright: 0 errors, 0 warnings, 0 informations.
- Semantic execution suite: 31 passed.
- Outline replay/invalidation suite: 9 passed.
- Semantic chunking, relations, receipt closure, reviewctl: 31 passed.
- Preprocess service: 35 passed.
- Preprocess platform safety, Zotero attachment resolver, export: 26 passed.
- Runtime/CLI/validation/review generation group: 15 passed.
- Additional focused provider/outline tests: 52 passed.
- Provider routing/replay and current production-chain regressions: 14 passed
  in the latest local run after semantic response replay/closure fixes.

The environment-wide `pip check` is now clean after installing `pypdf 6.19.0`
for the active Python 3.13 environment. PDF/DOCX/export focused tests passed
20/20 after installation.

## Remaining acceptance boundaries

- R1's fixed 63-paper live provider run, Writer, Validator/repair, citation
  verification, DOCX export, and GUI parity were not invoked in this coding
  turn. They require owner-authorized credentials, budget, and the frozen R1
  manifest. The machine-readable matrix marks them `BLOCKED_EXTERNAL` or
  `NOT_VERIFIED`, never PASS.
- The historical R1 spec was machine-checked read-only: 63 typed
  `stage1_reusable_summary_manifest/v1` files exist and decode with 63 unique
  paper keys. The user-authorized `D:\\auto-generate\\config.ini` is present,
  but the current loader rejects its legacy `Outline_GPT_*` sections and all
  nine API keys are classified as template credentials. Its historical
  external-host acknowledgement expired on 2026-09-21, so a live run still
  requires real credentials and a fresh acknowledgement.
- F1 remains an independent regression corpus and is `UNACCEPTED`; no R1
  evidence is promoted to F1.
- Full-repository Ruff still reports pre-existing unused-import and duplicate
  literal findings in unrelated modules. The changed-file lint surface is
  clean.
- Hosted CI is being rerun against the current SHA; the prior run's failures
  were type-check/strict-offline consequences of the semantic response
  closure changes and are not treated as acceptance evidence.
- The initial commit emitted a Git maintenance warning about pruned reflog
  objects while committing. The commit and remote read-back succeeded; the
  repository object-health warning should be repaired separately without
  rewriting user history.
