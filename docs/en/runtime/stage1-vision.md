# Stage 1 Selective Visual Evidence Pipeline

Stage 1 is a full-text-first academic paper reader. MinerU normalized text,
structured page metadata, captions, blocks, page index, and OCR are the primary
evidence. Vision is a deterministic, bounded supplement for evidence whose
meaning depends on layout, object boundaries, or visual notation.

The normal path is:

```text
PDF / Zotero attachment
  -> canonical attachment resolution
  -> MinerU / preprocess
  -> normalized full text + structured page metadata + OCR/captions/blocks
  -> deterministic selective visual gate
  -> selected figure/table/formula/page units (only when required)
  -> one paper-level Stage 1 synthesis
  -> summary_v2_lite
```

## Normal, escalation, and exceptional paths

For a modern digital PDF with reliable text, Stage 1 makes one synthesis
provider call. It may include no images, or a small set of deterministic visual
objects. A 30-page paper does not create 30 visual calls merely because it has
30 pages.

The selector runs before any visual provider call. It uses persisted preprocess
evidence such as `image_count`, figure/table/formula captions, framework/model/
mechanism/workflow terms, nearby text, block geometry, OCR use/conflicts, text
layer quality, and scanned-page indicators. It never calls Vision to decide
which page should be sent to Vision.

If selected objects exceed the request image budget, the runtime creates
`stage1_visual_extract:<paper>:<batch>` calls and then exactly one synthesis.
These are selected-visual batches, not page-coverage batches.

Only scan-heavy or OCR-poor inputs may use the exceptional adaptive page path.
Typical deterministic reasons are `scanned_pdf`, `scanned_page_ratio`,
`low_text_coverage`, `low_text_layer_coverage`, or materially degraded OCR.
Set `Stage1_Visual.selection_mode=adaptive_page_scan` for an explicit test or
operator escalation. `render_all_nonblank_pages=true` remains a readable
compatibility switch for that exception; it is not the production default.
Adaptive page calls use the retained page-attribution contract and record the
reason for escalation in the coverage and receipt artifacts.

## Selective visual gate and budgets

The shipped defaults are:

```ini
[Stage1_Visual]
selection_mode = selective
render_all_nonblank_pages = false
page_snapshot_soft_max = 4
figure_crop_soft_max = 6
table_crop_soft_max = 6
formula_crop_soft_max = 4
selected_visual_soft_total = 10
selected_visual_hard_total = 16
```

Page snapshots, figure crops, table crops, and formula crops are separate
evidence units. A page snapshot and a crop for the same object are not both
sent by default; the selector prefers the crop unless page-level layout,
scanning quality, attribution uncertainty, or crop failure makes the full page
the safer representation.

Soft budgets guide optional selection. Required units are never silently
dropped when a soft budget is reached. If required units do not fit in one
request, selected-object extraction batches are used. Image byte budgets,
base64 expansion estimates, single-image limits, request limits, frozen local
bytes, atomic groups, and Registry hashes remain enforced at transport time.

## Completeness and provenance

The current selective coverage artifact is `stage1_visual_coverage/v2`. Its
authority is based on required visual units, not all nonblank pages:

- `visual_selection_status`
- `required_visual_unit_count`
- `required_visual_unit_ids`
- `optional_visual_unit_ids`
- `selected_visual_unit_ids`
- `inspected_visual_unit_ids`
- `unresolved_visual_unit_ids`
- `visual_extraction_strategy`
- `evidence_coverage_status`

Normal text-only papers have no required visual units and use
`evidence_coverage_status=not_required`. A selected unit is complete only when
it is directly sent in the synthesis or successfully represented by a selected
visual extraction observation. A missing required unit is `incomplete`; it is
never silently reduced to complete.

Selected-object observations use `stage1_visual_evidence/v3` and the active
`stage1.visual_extract.system.v1` prompt. The legacy page-attribution reader
`stage1_visual_observations/v2` and its prompt remain available for the
adaptive page exception and for auditing older runs. Legacy all-page artifacts
cannot masquerade as a selective authority.

Every provider-generated summary still binds source PDF bytes, preprocess
artifacts, prompt/schema identities, selected visual IDs with page/bbox and
image hashes, Registry dependencies, expected-call graph, provider receipts,
receipt closure, and typed Stage 1 reuse authority. Changing the selection
identity invalidates exact summary reuse. Source PDFs, MinerU output, OCR,
page index, structured JSON, and preprocess artifacts may be reused only after
their hashes and Registry dependency closure verify.

## Output budgets and retry taxonomy

Visual extraction and paper synthesis have independent Stage 1 output budgets:
`stage1_visual_scan_max_output_tokens` and
`stage1_synthesis_max_output_tokens`. The synthesis default has enough
headroom for `summary_v2_lite`; the effective sequence is clamped to the
configured or known provider maximum and is shown by `reviewctl doctor`.

Retries are classified, not shared:

- transient network/429/502/503/504 failures use bounded transport retry;
- `finish_reason=length` advances once through a larger same-route budget and
  then fails closed as `STAGE1_SYNTHESIS_OUTPUT_BUDGET_EXHAUSTED` (no same-budget
  retry and no backup burn after exhaustion);
- simple JSON envelope defects may be recovered locally;
- schema-semantic failures have at most a bounded schema-aware recovery;
- prompt/schema drift, invalid enums, deterministic parameter/authentication
  errors, and other non-retryable 4xx failures stop deterministically.

The `evidence_kinds` enum is defined once in
`services/stage1_visual_schema.py`. Both the page prompt/validator and the
selected-object prompt/validator consume that authority, so visual IDs cannot
be misused as evidence kinds.

## MinerU result URL safety

MinerU result downloads require HTTPS and an exact hostname. The safe defaults
include the currently observed official hosts
`mineru.oss-cn-shanghai.aliyuncs.com` and `cdn-mineru.openxlab.org.cn`.
`MINERU_ALLOWED_URL_HOSTS` can add exact hosts; schemes, paths, arbitrary URLs,
and wildcards are rejected. `reviewctl doctor` reports the effective allowlist
and warns about invalid entries without making a network call.

## Transport and validation boundaries

Vision requests use provider-specific multimodal capability checks and the
existing base64/image byte hardening. Raw image paths are frozen before a
synthesis transport; receipts record the request hash and actual image
membership. Validation remains text/evidence-only and consumes structured
visual observations and provenance, not fresh Vision calls.

The optional live DeepSeek smoke is not implied by offline or mocked tests. It
is `NOT_RUN` unless the explicit live flag and a DeepSeek-scoped credential are
provided.

## Architecture comparison

Historical default:

```text
all nonblank pages -> page visual scan -> page observations
                   -> selected raw reinspection -> paper synthesis
```

Current default:

```text
MinerU full text -> deterministic selective visual gate
                 -> optional selected-visual extraction batches
                 -> one paper synthesis
```

All-page inspection remains an evidence-based exceptional fallback. This
restores the original product meaning—an academic reader whose main input is
the normalized paper—while retaining provenance, Registry, receipts,
fail-closed validation, transport safety, and resumable reuse.
