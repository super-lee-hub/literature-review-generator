# PR #14 Remediation Audit

Audit date: 2026-08-02 (Asia/Shanghai)

Repository: `super-lee-hub/literature-review-generator`

Branch: `codex/platform-hardening-outline-v3`

Base: `origin/main` at `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a`

This is the pre-remediation source audit required by PR #14. It records the
call graph observed on the branch before the clean-cut implementation. The
inventory was produced with `git grep` over tracked source and configuration;
file names alone were not treated as evidence of a production caller.

## 1. Current production entry points

### Public and compatibility entries

| Entry | Observed implementation | Current role | Remediation |
| --- | --- | --- | --- |
| `python -m reviewctl` | `reviewctl.py` | Thin JSON control-plane shell | Keep as the only public CLI and remove handler injection |
| `python -m runtime.cli` | `runtime/cli.py` | Second runtime CLI; imports `main` and accepts injected handlers | Remove as a public entry |
| `python main.py` | `main.py:8450-8638` | Large compatibility CLI and monolith dispatcher | Reduce to service-owned compatibility surface and remove from documented production use |
| GUI | `gui/app.py:1119-1515` | Configures jobs and calls `services.job_runner.JobRunner`/`QueueRunner` | Route through the same control-plane runner |
| Queue | `services/queue_service.py` plus `gui/app.py` | Persistent queue and worker execution | Remove private state writes and use formal QueueService transitions |

The currently intended durable path is:

```text
reviewctl / GUI / Queue
  -> ReviewControlPlane or shared service facade
  -> AgentRuntimeRunner / canonical stage services
  -> JobWorkspace + ArtifactRegistry
  -> job outcome, stage terminals, validation, export projections
```

The observed implementation is not yet this path: `runtime.cli` imports
`main` as `legacy_main`; `runtime.control_plane` constructs a runner with
the same dependency; and `services.job_runner` still dispatches multiple
actions through `main` handlers.

## 2. All CLI surfaces and public commands

`reviewctl.py` currently exposes `doctor`, `plan`, `run`, `status`, `inspect`,
`next-action`, `resume`, `retry-node`, `reconcile`, `repair-plan`,
`repair-apply`, `validate`, `cancel`, `adopt`, `export`, and `attest`. The
`run` and `resume` parsers still accept `--stage-handler` and
`--validator-module`.

`runtime/cli.py` exposes `run`, `resume`, `status`, `reconcile`, and a
workspace migration command. Its `run`/`resume` path dynamically imports
the requested stage handler, validator module, and `main` module.

`main.py` exposes historical workflow commands through `build_parser()` and
`dispatch_command()`, including run-all, stage-one, outline, review, retry,
merge, cleanup, and outline adoption modes. These are implementation callers
from GUI/job services rather than a second documented control plane, but the
remaining dispatch logic must be moved behind the shared service layer before
the file can become a thin entry point.

## 3. Outline paths and current callers

The current outline production path is `main.py` -> `outline.pipeline.V2Pipeline`
at `main.py:7094`, with its implementation importing:

* `outline.literature_map.build_literature_map`;
* `outline.synthesis_flow.build_synthesis_flow`;
* `outline.candidates` candidate generation;
* `outline.critique_v2` critique generation;
* `outline.arbitration_v2` arbitration and final-outline construction;
* `outline.coverage_audit.run_coverage_audit`;
* `outline.adoption` and `outline.adoption_transaction`;
* `outline.v2_config` and `outline.v2_models`.

The branch already contains deterministic v3 foundations in
`outline/v3_evidence.py`, `outline/v3_models.py`, and `outline/v3_relations.py`,
plus durable node and replay primitives in `runtime/outline_v3_dag.py` and
`runtime/outline_v3_replay.py`. No source file named `OutlineV3Executor`
currently owns the complete DAG from evidence views through explicit adoption;
the v3 foundation is therefore not yet a production execution path.

Other outline callers include `outline/__init__.py`,
`outline/runtime_resolver.py`, `outline/adoption.py`,
`outline/adoption_transaction.py`, tests under `tests/test_outline_*`, and
the legacy `main.py` dispatch methods. These callers must be migrated to one
current v3 namespace before deleting the v2-only modules.

## 4. Configuration truth sources

The current configuration read/write path is split across:

* `config_loader.py`, which imports `services.config_compat`;
* `config_validator.py`, which validates API, performance, outline, retry,
  validation, preprocessing, and GUI fields;
* `services/configuration_service.py`, which owns defaults, normalization,
  section creation, and save round-trips;
* `setup_wizard.py`, which still reads and writes `[Performance]` and two
  validation sections;
* `gui/app.py`, which imports compatibility validation helpers and has its
  own config-save surface;
* `config.ini.example`, which still emits the version-selection outline key
  and legacy performance/token fields.

`services/config_compat.py` currently defines `CompatConfigView`, validation
compatibility settings, outline v2 settings, legacy citation policy handling,
and legacy rate-limit key handling. This is a compatibility truth layer and
must be replaced with typed current settings before removal.

The desired single truth layer is a typed settings model with explicit
`ApplicationSettings`, `ProviderSettings`, `RuntimeSettings`,
`ValidationSettings`, `OutlineSettings`, `PreprocessSettings`, retry policy,
and rendering settings. Current validation switches must live only in
`[Validation]`; runtime concurrency and retry limits must live only in
`[Runtime]`.

## 5. Compatibility adapters and old schema readers/writers

Static caller search found the following compatibility surfaces:

* `services/config_compat.py` and its imports from loader, GUI, main, control
  plane, lifecycle, and tests;
* `outline/legacy_adapter.py`;
* `outline/runtime_resolver.py` and its v2 config resolution;
* `outline/v2_config.py`, `outline/v2_models.py`, and `outline/pipeline.py`;
* `runtime/cli.py` workspace projection and migration methods;
* `runtime/reconcile.py` legacy workspace outcome projection;
* `services/review_draft.py` v1/v2 builders;
* `services/citation_manifest.py` migration and legacy-policy branches;
* citation resolution helpers in `services/citation_ref_catalog.py` and
  `validation/review_validator.py`;
* `services/source_normalizer.py` legacy paper projection;
* `main.py` legacy paper, draft, context, and outline fallbacks.

Artifact schema versions that are current rather than compatibility paths
must remain versioned. In particular, the existing canonical citation
manifest version is not removed merely because older migration readers are
deleted. If a clean-cut field removal changes that schema, its formal version
must be advanced and all readers/writers/tests updated together.

## 6. Production fallback inventory

`git grep` found these fallback families in production or production-facing
code:

* client token-bucket and adaptive rate-limit remnants in `ai_interface.py`,
  config defaults, validator, wizard, and documentation;
* outline v2 feature flags and `V2Pipeline` dispatch;
* dynamic external `--stage-handler` and validator imports;
* `main` injection into runtime runner/orchestrator/job runner;
* legacy workspace status/reconcile/migration projection;
* 950000 context limit, head/tail truncation, and outline/synthesis context
  optimizers in `context_manager.py` and `main.py`;
* compatibility review-draft and citation-manifest readers/writers;
* legacy citation guessing and warning paths;
* report-only repair paths that can materialize empty or unchanged patches;
* queue worker writes to private queue internals;
* export status determined before all closure bytes are read.

The clean-cut rule is to remove these paths from production rather than keep
them behind warnings or a version switch. A one-time migration tool is not
planned because this audit found no repository fixture proving a current
user need for conversion of an old workspace.

## 7. Provider call and completion entry points

Provider calls enter through `ai_interface.py`, principally
`_call_ai_api_detailed_uninstrumented`, `_call_ai_api_detailed`,
`get_summary_from_ai_detailed`, and `get_summary_from_ai_with_fallback`.
Provider capability and route policy also live in
`services/model_capabilities.py` and `services/model_selection.py`.
`runtime/provider_runtime.py` now contains receipt/budget primitives, but the
current path still needs one bound runtime per job/attempt/stage/node and
receipt-backed completion for every real call.

Current completion decisions are distributed across
`services/job_outcome.py`, `runtime/runner.py`, `runtime/reconcile.py`,
`outline/stage_health.py`, `outline/adoption.py`, and validation result
contracts. `runtime/completion_evaluator.py` exists, but the runner still
has independent readiness logic and the control plane projects several
states separately. The remediation must make the evaluator the only
completion decision source.

## 8. Queue, GUI, export, adopt, and repair writes

* Queue state is written by `services/queue_service.py`, with GUI/worker
  paths using `QueueRunner`; private runtime maps and direct save calls must
  be replaced by formal state-transition methods.
* GUI config and job actions are implemented in `gui/app.py`; status text and
  progress projections must read canonical evaluator/artifact facts.
* Outline adoption is written by `outline/adoption.py` and
  `outline/adoption_transaction.py`; it currently consumes v2 models and
  must consume v3 final outline, coverage, stability, health, receipts, and
  dependency closure.
* Validation writes canonical results through `validation/run_result.py`,
  closure services, and runtime adapters; validation execution must not be
  reduced to reading an existing closure.
* Repair writes are implemented in `services/repair_integration.py` and
  `validation/repair_transaction.py`. Dependency hashes and action type must
  be complete; unchanged/report-only findings must remain issue/action records
  instead of fake patch proposals.
* Export and attestation write through `runtime/export_bundle.py`. The bundle
  service must resolve and verify closure, read bytes, checksum the exact
  payload, validate the temporary ZIP, atomically rename it, and only then
  register the bundle.

## 9. Removal and replacement plan

| Stage | Remove or rewrite | Replacement/current owner | Gate |
| --- | --- | --- | --- |
| 1 | compatibility config, duplicate validation/performance truth, token-pool fields | typed current settings + strict validator | config tests + Pyright |
| 2 | second CLI, handler injection, `main` runtime injection, workspace projection | `reviewctl` + built-in stage registry | control-plane/runtime tests |
| 3 | route-local budget/receipt gaps | bound ProviderRuntime + context profiles + completion evaluator | provider tests |
| 4 | 950000/head-tail truncation | explicit budget failure, stable sharding, hierarchical merge | context/shard tests |
| 5-6 | incomplete v3 foundation and v2 production path | `OutlineV3Executor` and current v3 models | deterministic E2E + resume tests |
| 7 | duplicate completion predicates and hand-written health | evaluator-derived health/outcome | completion tests |
| 8 | old draft/citation migration and fake repair patches | current schemas + semantic validation/repair closure | validation/repair tests |
| 9 | queue private writes and cancellation race | formal QueueService transitions | cancellation/GUI tests |
| 10 | premature export state and broad registry export | closure allowlist + atomic export/attestation | export tests |
| 11-14 | stale docs and missing architecture guard | current docs + full regression evidence | final commands + CI |

## 10. Audit conclusion

The current branch contains meaningful provider, registry, validation,
Outline v3 foundation, replay, completion, queue, and export building blocks,
but the production path still has the split architecture identified above.
This document is the separate audit commit required before implementation.
The following commits must be small, stage-scoped, and each must include its
focused gate; the two user-owned PPH ZIP archives remain untracked and out of
scope.
