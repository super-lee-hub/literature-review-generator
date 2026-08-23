# Stage 1 Vision-First Pipeline

Stage 1 keeps MinerU or other normalized full text as a primary evidence
source and adds page-level visual evidence. The pipeline does not guess which
figure is important before the model has seen the page.

## Coverage

Every nonblank PDF page is rendered as a traceable `page_snapshot`. Blank pages
may be skipped only with `skipped_blank`; render failures remain explicit
`render_failed`. The durable `stage1_visual_coverage/v1` report records total
pages, nonblank pages, rendered/scanned/skipped/failed counts, page status,
crop references, batches, coverage status, and omissions.

## Request paths

- Short papers (at most `single_call_max_pages`) use one synthesis request with
  MinerU full text, page metadata, page-specific labels/OCR, page images, and
  bounded figure/table/formula crops.
- Longer papers first use deterministic visual scan batches of at most
  `visual_scan_batch_size` page objects. Each image is immediately preceded by
  a text label containing `visual_id`, page, bbox, artifact type, caption, and
  nearby OCR/text. The scan output is a durable
  `stage1_visual_observations/v2` artifact.
- Final synthesis receives the full normalized text, all scan observations,
  coverage report, and at most `final_image_refs_max` high-value crops. Crop
  selection is a second-stage evidence decision; it does not decide whether a
  page was visible to the visual model.

## Page-to-crop attribution and reuse qualification

The first pass is page-only: a `page_snapshot` is the unit that is sent and
observed for long-paper coverage. The v2 prompt receives bounded metadata for
same-page `figure_crop`, `table_crop`, and `formula_crop` candidates, but those
metadata entries are candidates rather than confirmed observations. Each page
observation must declare `resolved`, `ambiguous`, or `no_matching_candidate`.
Only an explicit, validated `raw_reinspection_candidates` association can
select a child; a child does not inherit quantitative or relationship evidence
merely because it shares a page. The selected child carries
`source_page_visual_id`, `source_observation_visual_id`, `object_attribution_*`,
`post_scan_score`, and score components. If an ambiguous set does not fit the
raw-image budget, the reducer retains the page snapshot as the safe fallback.
Ambiguous attribution is atomic: the reducer admits the complete candidate set
or one page snapshot, never a partial child set. This atomic decision covers
reference count, encoded request bytes, per-image bytes, missing/unreadable
children, overlap, dedupe groups, and transport constraints. The final coverage
and provider transport metadata retain the group id, candidate ids, resolution,
selected ids, fallback reason, transport status, and child-completion flag.

The achieved reducer is recorded in the typed
`visual_evidence_qualification` binding and distinguishes four independent
facts:

- `scan_coverage_status`: `complete`, `partial`, `failed`, or `not_required`;
- `final_synthesis_modality`: `multimodal`, `text_only`, or `pdf_plus_text`;
- `final_raw_visual_recheck_status`: `complete`, `partial`,
  `not_run_fallback`, or `not_required`;
- `evidence_coverage_status`: `complete`, `degraded`, or `incomplete`.

These fields replace the old overloaded interpretation of `coverage_status`
(which remains only as a compatibility alias). A backup response after a
complete page scan keeps `scan_coverage_status=complete`, but records
`final_synthesis_modality=text_only`,
`final_raw_visual_recheck_status=not_run_fallback`, and
`evidence_coverage_status=degraded`; it is marked for manual review.

With `require_complete_visual_coverage=true`, exact reuse verifies the Registry
records, content hashes, JSON type/version, the v2 observation prompt/schema
identity, and observation/coverage bytes. A prior v1 observation contract is
therefore invalidated rather than silently reinterpreted. The
`validate_legacy_visual_observations_v1` API is a legacy reader only; current
Stage 1 and Registry paths call `validate_current_visual_observations_v2` and
reject v1 provider responses.
Partial or failed scans, omitted required pages, and missing, deleted, or
tampered coverage/observation artifacts block reuse and require new provider
work. Setting the option to `false` is an explicit degraded-reuse policy: it
does not erase the status or manual-review flag, and the referenced artifacts
must still verify.

## Provider and fallback

DeepSeek Vision uses the official OpenAI-compatible Chat Completions format:
`text` blocks and `image_url` blocks containing base64 data URLs. Responses
uses `input_text` and `input_image`. The experimental model is explicitly
capability-gated; ordinary `deepseek-v4-flash` is text-only. The configured
`deepseek-v4-flash-vision-exp` model is publicly documented/reported as live;
generic model-list endpoints may lag that availability. Therefore a missing
key means that live availability was not independently verified, not that the
model was declared unavailable.

If the vision call fails, the reader falls back to ordinary Flash with MinerU
full text and any successful visual observations rendered as text. A fallback
is recorded as fallback evidence and is never labeled multimodal success.

Validation stays on ordinary `deepseek-v4-flash` and receives source chunks,
OCR, captions, and observation text only. Original PDFs are not sent as file
attachments by default.

## Strict configuration values

Current Stage 1 booleans, enums, and `crop_padding_ratio` are parsed by one
shared strict parser during validation, settings normalization, and runtime
input construction. Unknown boolean spellings, unsupported enum values, and
non-finite or out-of-range padding values fail closed; they do not silently
become a default. Accepted boolean spellings are `true/false`, `1/0`,
`yes/no`, and `on/off`. The current enums are `mode=vision_first`,
`image_transport=base64`, and `send_original_pdf=never|auto|always`.

## Official limits

The implementation follows the [DeepSeek Vision guide](https://api-docs.deepseek.com/zh-cn/guides/vision): 48 MiB inline request body, 32 MiB per base64/URL image, 384 image-token upper bound, and 4096 px single-edge limit when a request contains 15 or more images.

## Explicit live smoke

The opt-in smoke test creates a private synthetic one-page PDF containing a
small table and a framework diagram, then checks the exact experimental model
ID, image payload, JSON response, usage, and provider receipt. Run it only when
the external credential and network call are authorized:

```powershell
$env:AUTO_GENERATE_RUN_LIVE_API = "1"
python -m pytest -q tests/live/test_deepseek_vision_smoke.py -m live_api
```

The smoke test accepts only `DEEPSEEK_API_KEY` or
`AUTO_GENERATE_DEEPSEEK_API_KEY`; a generic live key or `OPENAI_API_KEY` is
never sent to the DeepSeek endpoint. Without explicit opt-in, the result is
`LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_LIVE_API_NOT_ENABLED`; without a
DeepSeek-specific key, it is
`LIVE_DEEPSEEK_VISION_SMOKE=NOT_RUN_NO_DEEPSEEK_KEY`. Offline and mocked tests
do not substitute for live evidence. The smoke test never writes the key,
response body, or synthetic PDF into the repository.
