# F1 C→D→Q acceptance report

Status: `C=PASS / D=FAIL_MODALITY / Q=BLOCKED_D_PREREQUISITE`.

This report records the current executable and the exact evidence boundary. It
does not upgrade historical F1 outputs, local fixtures, or no-call preflight
into live acceptance.

## Frozen identities

- Live evidence checkout: `ff58c4000d3350436b322d5cdd9b27223b62125c`.
- Live acceptance parent run: `f1-acceptance-20260916-r3-ff58c4000d33`.
- The stored plan intentionally leaves `final_executable_sha` blank so each authorized run binds the exact clean checkout at execution time. The live receipts below remain bound to the SHA above; later documentation-only commits do not change this runtime evidence.
- Branch: `codex/f1-validation-authority-closure`
- Acceptance plan: [F1_ACCEPTANCE_PLAN_20260915.json](F1_ACCEPTANCE_PLAN_20260915.json)
- Plan identity SHA-256: `6cf51ea5b01a4986a4489b84f16addc8cf9e7672624afad5ea754db18d218f8d`
- Plan file SHA-256: `7f9be873cc9f9c563d2f6fd176531d983bb976c09f4927ab9fccabd0867d626a`
- Acceptance budget: at most 4 Provider calls, 128,000 output tokens, 4 retry attempts, and 7,200 wall-clock seconds.
- Corpus manifest: [F1_CORPUS_MANIFEST_20260915.json](F1_CORPUS_MANIFEST_20260915.json)
- Corpus manifest file SHA-256: `f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`
- Corpus content SHA-256: `ebaf5c2a9220ed23b527e279c0fd82a6770fa70e5d4ab5d1e1e64f2150ce4319`
- Source ledger: [F1_SOURCE_GROUND_TRUTH_20260915.md](F1_SOURCE_GROUND_TRUTH_20260915.md)

The staging root contains exactly the 15 PDFs selected by the existing F1
canonical-resolution manifest. Every staged file has the recorded size and
SHA-256; original Zotero files were read-only inputs and were not changed or
deleted.

## Configuration found and used for planning

The earlier claim that the local project had no configuration was incorrect.
The following files were found by bounded inspection:

- `D:\auto-generate\config.ini` (root project config; SHA-256
  `e1c2e29c16d86d1ef6d64695764a6be8d8c0374423ed2684d2efcd0da26b5133`).
- `D:\auto-generate\.env` (root project dotenv; values were never printed or
  committed).
- The user F1 recovery config:
  `C:\Users\12130\Desktop\新建文件夹\博good good study\不相关广告-观众信任项目_找回材料\F1_RECOVERY_WORKSPACE_20260905\F1_RECOVERY_CONFIG_20260905.ini`.
- The committed acceptance copy:
  [F1_ACCEPTANCE_CONFIG_20260915.ini](F1_ACCEPTANCE_CONFIG_20260915.ini),
  containing no real secrets. It makes the acceptance runtime local-parser,
  uses a finite 7200-second job deadline, and writes outputs under the local
  acceptance namespace.

The F1 recovery config has placeholder provider keys and a local parser. A
bounded preflight with only that config failed closed because the ambient
preprocess setting was `allow_local_parse_fallback=false` while the config
declared `fallback_parser=local`. A no-network preflight of the acceptance copy
with an explicit `ALLOW_LOCAL_PARSE_FALLBACK=true` child setting passed:

- action: `analyze`
- route: `Primary_Reader_API`, model `deepseek-v4-flash-vision-exp`, provider
  family `deepseek`, `chat_completions`, `https://api.deepseek.com`
- parser: local; `remote_parser_not_requested`; MinerU network calls: 0
- required external acknowledgement hosts for this analyze plan: none
- credential source in that child process: `process_env` from the root dotenv
  values, with no secret value emitted

The full `run_all` Q spec additionally reaches custom `Outline_API` and
`Writer_API` hosts (`chat.178266.xyz` and `ai.saigou.work`). The current plan
and Q RuntimeJobSpec carry a matching v2 ACK for those routes; Q still remains
`provider_calls_allowed=false` until the D prerequisite is passed.

A fresh no-network `run_all` preflight of the acceptance config resolved all
configured roles and reported `network_calls=0`, with the following exact
external set and route identity:

- required external hosts: `ai.saigou.work`, `chat.178266.xyz`
- route fingerprint: `344765ad52227adc74d73030a8c0942f527bc84bf136babf0c873144c30392a9`
- reachable role routes: Primary Reader on DeepSeek; Outline candidate/
  arbitration on Anthropic; Writer and structure/evidence critique on the
  configured OpenAI-compatible gateway; Validator and Free Mode on DeepSeek
- MinerU: `remote_parser_not_requested`

Those model and endpoint settings are configuration facts, not an automatic
external-host acknowledgement. The current v2 ACK was supplied explicitly for
the two custom hosts, but Q made no call because D did not pass.

The selected DeepSeek transport was also checked directly from the acceptance
configuration and current input builder: `send_original_pdf=never`,
`image_transport=base64`, and `force_pdf_file_input_for_provider=false`. The
DeepSeek vision capability probe reports image input and base64 support, but no
PDF file-input support. Stage 1 therefore constructs rendered image inputs;
the chat adapter encodes those images as `data:image/...;base64` values in
`image_url` fields alongside extracted text. No original PDF file is sent by
this F1 route.

## C/D/Q live execution

| Gate | Selection | Runtime spec SHA-256 | Live result | Provider calls |
|---|---|---|---|---:|
| C | F1-01 | `e0d0f5815c371bf691c6ca71f585b00720ab33227691f0a3bbe255a5d6e37827` | PASS: one source, canonical Stage 1, closure complete | 1 |
| D | F1-01, F1-03, F1-14 | `8a0c4633f4305abd62aab826c12d53e4b72e96227287e864144240d7644fe6d6` | FAIL: no `ocr_scanned` production-derived profile | 3 |
| Q | F1-01…F1-15 exact set | `cb7541d40e627bf9017649773e82b3ffb476731429f4b8e6c3e95a173f9013d7` | BLOCKED: D prerequisite not passed; custom-host v2 ACK is valid | 0 |

The live command used the same control-plane entrypoint with owner authorization
and the root dotenv loaded only into the child process. The durable parent result
is [parent_acceptance_result_v2.json](f1-acceptance-20260916-r3-ff58c4000d33/parent_acceptance_result_v2.json).
The child receipts are:

- C: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-ff58c4000d33/C/scenario_execution_receipt.json)
- D: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-ff58c4000d33/D/scenario_execution_receipt.json)
- Q: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-ff58c4000d33/Q/scenario_execution_receipt.json)

The result binds every child receipt to checkout SHA
`ff58c4000d3350436b322d5cdd9b27223b62125c`, corpus manifest SHA
`f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`, and
the plan identity above. C recorded one successful HTTP 200 DeepSeek
`Primary_Reader_API` call. D recorded three successful HTTP 200 calls on the
same route; the four calls consumed 49,787 output tokens in total.

The D production-derived profiles were:

- F1-01: `text_heavy` (27 pages; 6 image pages; 0 OCR pages).
- F1-14: `visual_table_heavy` (11 pages; 11 image pages; 0 OCR pages).
- F1-03: `text_heavy` (20 pages; 9 image pages; 0 OCR pages).

Therefore C is a verified live gate, but D is not a PASS: the runtime and
Provider receipts are valid while the required three-way modality criterion is
not met. Q was stopped before any Provider call because the configured custom
Outline/Writer hosts have a valid current v2 acknowledgement, but D remains an
unmet prerequisite.

## Why the full parent remains blocked

The earlier live attempt was correctly rejected before transport because owner
authorization had not yet been given. After the explicit authorization, C and D
did run using only extracted text and rendered images; no original PDF file was
sent. The current blocker is corpus modality, not authorization or PDF format.

To make D pass, the owner must provide an approved source selection whose
production-derived profiles include an actual `ocr_scanned` member, or approve a
revised acceptance corpus/criterion. The existing machine-only source ledger
states that no scan-primary source was established in the current 15-paper
corpus, so the gate must not be weakened or promoted. Q's exact v2
external-host acknowledgement is now valid, but Q remains correctly blocked by
the unmet D prerequisite; no custom-host content was sent.

## Acceptance items not verified

- D heterogeneous modality gate and Q full 15-paper chain: Outline v3, evidence
  packets, Writer, validation, citation/source closure, DOCX output, and
  canonical JobOutcome.
- Human original-PDF ground truth, claim-level citation review, negative
  validator challenge, repair/revalidation, and DOCX visual QA.
- Production GUI/Playwright flow and real OCR/scanned-primary flow.
- Real MinerU create→upload→poll→download→parse and kill/resume.
- Hosted CI run `35079384646` for the prior live-evidence checkout
  `387bb723...` completed successfully across all six jobs. This report-only
  ACK update is pushed separately and has its own CI run; the PR remains open
  and unmerged.
