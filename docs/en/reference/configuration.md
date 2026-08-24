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

- `mode = vision_first`
- `send_extracted_text = true`
- `send_selected_visuals = true`
- `send_original_pdf = never`
- `image_transport = base64`
- `single_call_max_pages = 12`
- `visual_scan_batch_size = 10`
- `final_image_refs_max = 8`
- `require_complete_visual_coverage = true`
- `max_request_image_bytes = 36000000` raw image-byte budget, leaving headroom for
  base64 expansion below DeepSeek's official 48 MiB inline request limit.
- `max_single_image_bytes = 24000000` raw-byte budget, leaving headroom below
  the official 32 MiB single base64/URL image limit.

Current Stage 1 boolean and enum values are strict. Accepted boolean spellings
are `true/false`, `1/0`, `yes/no`, and `on/off`; current enum values are
`mode=vision_first`, `image_transport=base64`, and
`send_original_pdf=never|auto|always`. `crop_padding_ratio` is a finite value
from `0` through `0.25` inclusive. Unknown spellings, unsupported enum values,
non-finite floats, and out-of-range padding fail configuration validation and
are not silently replaced with defaults.

The runtime estimates base64-expanded bytes for both per-image and per-request
budgets. A visual scan records planned, sent, omitted, and observed visual IDs;
an observation batch is valid only when it contains exactly one strict-schema
observation for every image actually sent. Long papers scan all sendable
nonblank pages first, then select the final raw image references from those
observations. Backup reader transport is text-only and is recorded as such.

## Stage 1 ownership and migration keys

`Stage1_Input` owns provider-facing text/PDF/image transport, page-scan batch
and final-reference budgets, and the `require_complete_visual_coverage` reuse
policy. `mode=vision_first`, `image_transport=base64`, and
`Stage1_Visual.render_all_nonblank_pages=true` are invariants. The historical
`pdf_required_for_formal_precision`, `formal_precision_text_only_policy`, and
`pdf_verifier_api` keys are accepted only for migration normalization and are
removed from the typed current settings.

`Stage1_Visual` owns rendering and crop shape: page/crop dimensions, pixel and
artifact-byte limits, formats, JPEG quality, padding, and table/formula crop
switches. The current transport budgets remain in `Stage1_Input`. The old
`max_visual_refs_per_paper`, `visual_artifact_dir`, and the duplicate
`Stage1_Visual.max_request_image_bytes` / `max_single_image_bytes` keys are
removed and rejected; they are not silently accepted as current controls.

Default values in `config.ini.example` are checked against
`default_config_sections()` (apart from secret placeholders) so GUI defaults,
the example file, and the runtime owner map cannot drift independently.

## Stage 1 Visual Rendering

`[Stage1_Visual]` defaults render every nonblank page at a target long edge
of 2200 px. Page snapshots use JPEG quality 92; figure, table, and formula
crops use PNG with about 4% padding. Pixel and byte limits are enforced before
publication. Each visual manifest records dimensions, scale, estimated DPI,
format, byte count, and SHA-256.

## Migration and Evidence

Changing the model, capability, prompt identity/hash, schema, preprocess
evidence, visual manifest, or visual coverage invalidates Stage 1 reuse. Missing
or failed page rendering is recorded in `stage1_visual_coverage/v1`; with
`require_complete_visual_coverage=true`, the typed
`visual_evidence_qualification` must verify complete evidence before exact
reuse. `quality_audit.needs_manual_review=true` records degraded or incomplete
evidence; it does not authorize reuse. An explicit
`require_complete_visual_coverage=false` policy may reuse a verified degraded
binding with that status preserved. This switch relaxes only the final raw
reinspection completeness gate: rendering, page scanning, observation
integrity, and final-transport omissions remain fail-closed semantic gates.
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
