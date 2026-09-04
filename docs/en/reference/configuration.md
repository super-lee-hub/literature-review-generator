# Configuration Reference

The tracked template is [`config.ini.example`](../../../config.ini.example).
`config.ini` and `.env` are local secrets/configuration and must not be
committed. Configuration schema version 4 is loaded by `config_loader.py` and
validated by `services.settings`.

## Reader and Validator Defaults

- `Primary_Reader_API.model = deepseek-v4-flash-vision-exp`: experimental
  vision-first Stage 1 reader.
- `Backup_Reader_API.model = deepseek-v4-flash`: text-only Stage 1 fallback.
- `Validator_API.model = deepseek-v4-flash`: validation remains text-only.
- The primary reader may carry image content; Validator requests never carry
  `local_image_path`, `image_url`, `input_image`, or PDF file content.
- Existing custom reader models are preserved. The migration changes only the
  shipped legacy DeepSeek default or an explicitly enabled `vision_first` mode.
- `[Multimodal]` is read once for migration warning compatibility and is no
  longer written or used as a second API-key authority.

## Stage 1 Input

`[Stage1_Input]` controls the evidence path:

- `mode = text_first`
  (`vision_first` / `text_only` are compatibility inputs normalized to `text_first`)
- `send_extracted_text = true`
- `send_selected_visuals = true`
- `send_original_pdf = never`
- `image_transport = base64`
- `single_call_max_pages = 12`
- `visual_scan_batch_size = 8`; this applies only to selected-object extraction
  or explicit scan-heavy escalation, never to normal page count
- `stage1_visual_scan_max_output_tokens = 16000`
- `stage1_synthesis_max_output_tokens = 64000`
- `stage1_length_retry_max_attempts = 1`; `finish_reason=length` gets at most
  one larger same-route recovery and does not fall through to backup after
  budget exhaustion
- `stage1_length_retry_ceiling_tokens = 128000`, clamped to the provider cap
- `stage1_request_timeout_seconds = 300`; this long Stage 1 timeout is
  independent from the output-token budget
- `stage1_semantic_retry_max_attempts = 1`; a JSON response that fails the v2
  visual schema may retry on the same primary route only within this finite bound
- `final_image_refs_max = 8`
- `require_complete_visual_coverage = true`
- `max_request_image_bytes = 36000000` raw image-byte budget, leaving headroom for
  base64 expansion below DeepSeek's official 48 MiB inline request limit.
- `max_single_image_bytes = 24000000` raw-byte budget, leaving headroom below
  the official 32 MiB single base64/URL image limit.

Current Stage 1 boolean and enum values are strict. Accepted boolean spellings
are `true/false`, `1/0`, `yes/no`, and `on/off`; the current canonical mode is
`mode=text_first` (compatibility inputs `vision_first` / `text_only` are normalized),
with `image_transport=base64` and
`send_original_pdf=never|auto|always`. `crop_padding_ratio` is a finite value
from `0` through `0.25` inclusive. Unknown spellings, unsupported enum values,
non-finite floats, and out-of-range padding fail configuration validation and
are not silently replaced with defaults.

The runtime estimates base64-expanded bytes for both per-image and per-request
budgets. The selective gate records required, optional, selected, inspected, and
unresolved visual unit IDs. A visual extraction batch is valid only when it
contains exactly one strict-schema observation for every image actually sent.
Backup reader transport is text-only and is recorded as such.

## Stage 1 ownership and migration keys

`Stage1_Input` owns provider-facing text/PDF/image transport, selected-object
batch and final-reference budgets, and the `require_complete_visual_coverage`
reuse policy. `mode=text_first` and `image_transport=base64` are current invariants;
legacy inputs `vision_first` / `text_only` normalize to `text_first`. `Stage1_Visual.selection_mode=selective` is the production default;
`adaptive_page_scan` and `render_all_nonblank_pages=true` are explicit
exception controls. The historical
`pdf_required_for_formal_precision`, `formal_precision_text_only_policy`, and
`pdf_verifier_api` keys are accepted only for migration normalization and are
removed from the typed current settings.

`Stage1_Visual` owns deterministic selection and rendering: selection mode,
soft/hard visual budgets, page/crop dimensions, pixel and artifact-byte limits,
formats, JPEG quality, padding, and table/formula crop switches. The current
transport byte budgets remain in `Stage1_Input`. The old
`max_visual_refs_per_paper`, `visual_artifact_dir`, and the duplicate
`Stage1_Visual.max_request_image_bytes` / `max_single_image_bytes` keys are
removed and rejected; they are not silently accepted as current controls.

Default values in `config.ini.example` are checked against
`default_config_sections()` (apart from secret placeholders) so GUI defaults,
the example file, and the runtime owner map cannot drift independently.

## Stage 1 Visual Rendering

`[Stage1_Visual]` defaults to `selection_mode=selective` and
`render_all_nonblank_pages=false`. It uses soft caps of 4 page snapshots, 6
figure crops, 6 table crops, 4 formula crops, 10 selected visuals total, and a
16-unit hard total. Page snapshots use a 2200 px target long edge and JPEG
quality 92; figure, table, and formula crops use PNG with about 4% padding.
Pixel and byte limits are enforced before publication. Each visual manifest
records dimensions, scale, estimated DPI, format, byte count, and SHA-256.

## Outline Role Routing

Each semantic role in `[OutlineModels]` resolves to its own API section via
`OutlineProviderRouter` in `outline/provider_router.py`:

| Role | Key | Note |
| --- | --- | --- |
| Relation adjudication | `relation_adjudicator_model` | Prefer a model distinct from generation |
| Candidate generation | `outline_model` | Strong reasoning model |
| Structure critique | `structure_critic_model` | Should differ from generation |
| Coverage critique | `coverage_critic_model` | Should differ from generation |
| Evidence critique | `evidence_critic_model` | Should differ from generation |
| Arbitration | `arbitrator_model` | Normally the same model as generation |

Generation and arbitration sharing one model is **intentional**: the arbitrator
must absorb peer critiques using the same reasoning model that produced the
candidates. Consequently only a *critique* collapsing onto the generator's
provider counts as self-review, and the executor reports that diagnostic
explicitly instead of silently degrading to single-model self-review.

A role that cannot be resolved is never quietly remapped onto `Outline_API`. It
is recorded as a diagnostic, and resolving that node fails closed.

The shipped role mapping is:

| Outline role | API section | Model | Wire endpoint / network ownership |
| --- | --- | --- | --- |
| Relation adjudication | `Free_Mode_API` | DeepSeek V4 Pro | DeepSeek Chat Completions, DeepSeek official API |
| Candidate generation | `Outline_API` | Claude Opus 5 | Native Anthropic Messages through `chat.178266.xyz`, third-party gateway |
| Structure critique | `Writer_API` | GPT-5.6-sol | OpenAI Responses-compatible transport through `ai.saigou.work`, third-party gateway |
| Coverage critique | `Free_Mode_API` | DeepSeek V4 Pro | DeepSeek Chat Completions, DeepSeek official API |
| Evidence critique | `Writer_API` | GPT-5.6-sol | OpenAI Responses-compatible transport through `ai.saigou.work`, third-party gateway |
| Arbitration | `Outline_API` | Claude Opus 5 | Native Anthropic Messages through `chat.178266.xyz`, third-party gateway |

The endpoint/protocol and the model brand are separate facts. A request sent to
`chat.178266.xyz` or `ai.saigou.work` is a request to a third-party gateway; the
project does not claim that the gateway is an official Anthropic or OpenAI
connection merely because it serves a Claude or GPT model name. The runtime
persists the gateway host and a secret-free route fingerprint in bindings and
receipts, while credentials remain in `.env` or the local credential store.

## Anthropic Messages Transport

`endpoint_type` supports `chat_completions`, `responses`, and `anthropic`. With
`anthropic`, the native Anthropic Messages protocol is used:

* requests go to `<api_base>/<anthropic_path>`, default `v1/messages`,
  overridable with `anthropic_path`;
* authentication uses `x-api-key` and `anthropic-version` headers rather than a
  Bearer token; the default version is `2023-06-01`, overridable with
  `anthropic_version`;
* the system prompt is the top-level `system` field, not a system message inside
  `messages`;
* the token limit is `max_tokens`, and with extended thinking `max_tokens` must
  exceed `thinking_budget_tokens` — the builder raises it automatically;
* the protocol has no `response_format` parameter, so a JSON request appends an
  instruction to the system prompt instead;
* the response `content` is a block list, and only blocks with `type == "text"`
  count as answer content.

For Claude Opus 5, the current request policy is adaptive thinking with
`output_config.effort`; the legacy `enabled` plus `budget_tokens` form is not
sent. `thinking_budget_tokens` is retained only for manually configured legacy
Claude generations that still require it.

A Claude model name alone does **not** trigger protocol inference: the same model
id may be served by an Anthropic endpoint or by an OpenAI-compatible gateway, and
guessing from the name would select the wrong wire format.

## Stage 3 Review and Writer

“Stage 3 Review” and “Writer” are two layers of one stage, not two independent
pipeline stages. Stage 3 is the review-generation workflow; `Writer_API` is the
provider/model it calls once per adopted Outline v3 section. The Writer produces
structured section blocks and citation tokens. The runtime then publishes the
canonical `review_draft/v3`, `citation_manifest/v3`, and DOCX artifacts and
closes the Stage 3 provider receipts. The separate `Validator_API` is used only
when the validation stage is requested.

## Explicit configuration migration

The loader is fail-closed and does not silently rewrite old files. Migrate an
older config explicitly, with an atomic same-directory replace and a backup:

```bash
python -m reviewctl config-migrate --config config.ini
```

Use `--dry-run` to inspect the report. Ambiguous legacy `[API_Parameters]` keys
are preserved in a marked comment block by default; `--drop-unknown-legacy` is
the explicit opt-in to discard them.

## Migration and Evidence

Changing the model, capability, prompt identity/hash, schema, preprocess
evidence, selection identity, visual manifest, or visual coverage invalidates
Stage 1 reuse. The current coverage artifact is
`stage1_visual_coverage/v2`; it binds required visual unit IDs rather than all
nonblank pages. Selected-object evidence is `stage1_visual_evidence/v3`.
`require_complete_visual_coverage=true` requires the typed
`visual_evidence_qualification` to verify all required units before exact reuse.
`quality_audit.needs_manual_review=true` records degraded or incomplete
evidence; it does not authorize reuse. An explicit
`require_complete_visual_coverage=false` policy may reuse a verified degraded
binding with that status preserved. Rendering, page scanning, selected-object
extraction integrity, and final-transport omissions remain fail-closed semantic
gates.
An unresolved raw unit must remain represented as degraded evidence with a
partial or fallback final raw-recheck status; it is never rewritten as
complete. Persisted current qualification JSON is parsed with exact types, so
malformed boolean, integer, array, or omission fields block reuse before any
permissive projection. Absence of `visual_evidence_qualification` is a legacy
compatibility path only when the binding is genuinely pre-current and has none
of the current visual markers; removing or emptying that qualification from a
current authority is a validation failure and must never downgrade to legacy.
Prompt files are Registry-authorized by SHA-256; malformed JSON node policies,
hash drift, and missing prompt placeholders fail closed.
