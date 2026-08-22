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
`require_complete_visual_coverage=true`, the summary remains eligible only with
`quality_audit.needs_manual_review=true` or a fresh successful complete run.
