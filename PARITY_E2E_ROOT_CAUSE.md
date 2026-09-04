# Parity E2E Root Cause

## Evidence boundary

- `BASELINE_SHA`: `4a5d56e83bf00a7eea529115798c772e0e1f15d6`
- Reproduction: `tests/test_validation_entrypoint_parity_e2e.py::test_direct_cli_gui_and_queue_normalize_to_one_job_contract`
- Environment: Windows, strict offline flags, all `MINERU_*` process credentials cleared

## Failed run

```text
NETWORK_ATTEMPTED = NO
MINERU_TOKEN_PRESENT = NO
PREPROCESS_ROUTE = hybrid requested MinerU, but no token was present; local fallback continued
LOCAL_PARSER_ROUTE = fitz
OCR_ROUTE = unavailable/not used (tesseract unavailable)
NORMALIZED_TEXT_BYTES = 202
PAGE_COUNT = 1
PREPROCESS_QUALITY_STATUS = PASS
STAGE1_PROVIDER_CALLED = NO
STAGE1_OUTPUT_CREATED = NO
FIRST_AUTHORITY_FAILURE = injected Stage1 reader callback rejected the production
                         stage1_input_settings keyword argument
```

The durable attempt snapshot recorded:

```text
built-in stage executor failed for analyze:
_patch_reader.<locals>.configured_reader() got an unexpected keyword argument
'stage1_input_settings'
```

The reported `summary_schema_ready=False`, `stage1_authority_ready=False`, and
`stage1_reuse_eligible=False` were downstream effects of that callback
`TypeError`; the preprocess manifest independently recorded
`stage1_quality_level=PASS` and `mineru_attempted=false`.

## Fix and verification

- `PARITY_FIX_TYPE = test-fixture contract correction`
- The callback now accepts the production `stage1_input_settings` argument; no
  Stage1 output, completion flag, or authority state was fabricated.
- The normal local preprocess and Stage1 artifact publication path remains
  active.
- `tests/test_validation_entrypoint_parity_e2e.py`: `4 passed`

