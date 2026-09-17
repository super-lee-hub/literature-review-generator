# F1 C→D→Q acceptance report

Status: `C=PASS / D=FAIL_MODALITY / I=PASS_SCOPED / J_AUX=PASS / K=PASS_OFFLINE / E_LOCAL=PASS_OFFLINE_SCOPED / Q=BLOCKED_D_PREREQUISITE`.

This report records the current executable and the exact evidence boundary. It
does not upgrade historical F1 outputs, local fixtures, or no-call preflight
into live acceptance.

## Frozen identities

- C/D/Q live evidence checkout: `09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`.
- C/D/Q live acceptance parent run: `f1-acceptance-20260916-r3-09477d666a0f`.
- Gate I live evidence checkout: `24884c2adaeac2d98af85349dbbccc6980d2c814`.
- Gate I live acceptance parent run: `f1-gui-acceptance-20260917-r2`.
- Gate K offline evidence checkout: `96e6f829e4fc0b99d636190b742129e74226c8b0`.
- Gate K offline acceptance parent run: `f1-k-offline-acceptance-20260917-96e6f829e4fc`.
- Gate E current local controlled-probe executable checkout: `a2c934081e93834338249f44e17d4e9af89b2ddc`.
- Gate E current local controlled-probe parent run: `f1-e-local-acceptance-20260917-positive` (`PASS_OFFLINE` scenario; parent projection remains `NOT_VERIFIED` because the provider is localhost).
- Gate E prior r13 controlled probe: `0699ddc2fc160f820af3d424dd52655ed44eb8a6` / `f1-e-local-acceptance-20260917-r13` (`NOT_VERIFIED`, unknown POST fail-closed).
- The stored plans intentionally leave `final_executable_sha` blank so each authorized run binds the exact clean checkout at execution time. Each acceptance receipt remains bound to the SHA recorded for its own parent; later documentation-only commits do not change the runtime evidence sets.
- Branch: `codex/f1-validation-authority-closure`
- Acceptance plan: [F1_ACCEPTANCE_PLAN_20260915.json](F1_ACCEPTANCE_PLAN_20260915.json)
- Plan identity SHA-256: `6cf51ea5b01a4986a4489b84f16addc8cf9e7672624afad5ea754db18d218f8d`
- Plan file SHA-256: `7f9be873cc9f9c563d2f6fd176531d983bb976c09f4927ab9fccabd0867d626a`
- Acceptance budget: at most 4 Provider calls, 128,000 output tokens, 4 retry attempts, and 7,200 wall-clock seconds.
- Gate I plan: [F1_GUI_ACCEPTANCE_PLAN_20260917_R2.json](F1_GUI_ACCEPTANCE_PLAN_20260917_R2.json)
- Gate I plan identity SHA-256: `a2439e3c941914b5426efb963c25a5d73539ef281b44c18b06a9abcb3fc146ac`
- Gate I plan file SHA-256: `f8f28850ae89652c504942a2488fc924e6b1e1619c497eba865d64ee9f776287`
- Gate I input manifest file SHA-256: `7bdfa3460e52ee41f971317cb52d56073273eb36a9451453940a73f0efd6694b`
- Gate I acceptance budget: at most 2 Provider calls, 128,000 output tokens, 2 retry attempts, and 1,800 wall-clock seconds.
- Gate K plan: [F1_K_OFFLINE_ACCEPTANCE_PLAN_20260917.json](F1_K_OFFLINE_ACCEPTANCE_PLAN_20260917.json)
- Gate K plan identity SHA-256: `84bd93d50c0743f4b09edef6b51b0bd9b281ac8d8dda20d315701b5397b0df4d`
- Gate K acceptance budget: at most 4 offline worker calls, 1,000 output tokens, 1 retry attempt, and 300 wall-clock seconds.
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
`Writer_API` hosts (`chat.178266.xyz` and `ai.saigou.work`). The recorded plan
and Q RuntimeJobSpec carry a matching v2 ACK for those routes, but that ACK
expired at `2026-09-17T11:51:39Z`; any future Q attempt must generate a fresh
ACK. Q remains `provider_calls_allowed=false` until both the D prerequisite and
current ACK are satisfied.

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

## F1 C/D/Q and supporting acceptance execution

| Gate | Selection | Runtime spec SHA-256 | Live result | Provider calls |
|---|---|---|---|---:|
| C | F1-01 | `e0d0f5815c371bf691c6ca71f585b00720ab33227691f0a3bbe255a5d6e37827` | PASS: one source, canonical Stage 1, closure complete | 1 |
| D | F1-01, F1-03, F1-14 | `8a0c4633f4305abd62aab826c12d53e4b72e96227287e864144240d7644fe6d6` | FAIL: no `ocr_scanned` production-derived profile | 3 |
| I | F1-01 GUI PDF flow | `c0301f40006ae22d817b137167a3addc94360fe723cfd2c3e2db3bd76cf11423` | PASS: real localhost GUI submission, completed canonical job, browser/trace evidence | 1 |
| K | two independent Windows/Python contention workers | `N/A (offline-k)` | PASS_OFFLINE: bounded lock wait, no corrupt JSON, no lost Registry/Queue updates, live budget unchanged | 0 |
| E | current local provider-stub crash/resume probe | `9a5aca4698f11ae370c36a70d71a8b95adc49f003b8fff9428563932271366f7` | PASS_OFFLINE_SCOPED: durable receipt boundary terminated, fresh process resumed, no duplicate receipt and no reexecution of a semantically completed call | 2 local stub calls; no external calls |
| Q | F1-01…F1-15 exact set | `cb7541d40e627bf9017649773e82b3ffb476731429f4b8e6c3e95a173f9013d7` | BLOCKED: D prerequisite not passed; recorded custom-host v2 ACK is now expired | 0 |

The live command used the same control-plane entrypoint with owner authorization
and the root dotenv loaded only into the child process. The durable parent result
is [parent_acceptance_result_v2.json](f1-acceptance-20260916-r3-09477d666a0f/parent_acceptance_result_v2.json).
The child receipts are:

- C: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-09477d666a0f/C/scenario_execution_receipt.json)
- D: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-09477d666a0f/D/scenario_execution_receipt.json)
- Q: [scenario_execution_receipt.json](f1-acceptance-20260916-r3-09477d666a0f/Q/scenario_execution_receipt.json)

Gate I was run separately with a primary-reader-only GUI config and its own
budget namespace. The parent projection is `SCOPED_PASS` because the plan
contains only Gate I; this is a live Gate I PASS, not full F1 merge readiness.
The submitted job was `job_f725f32d4ef0`, and the durable evidence is:

- Parent result: [parent_acceptance_result_v2.json](f1-gui-acceptance-20260917-r2/parent_acceptance_result_v2.json)
- Gate I receipt: [scenario_execution_receipt.json](f1-gui-acceptance-20260917-r2/I/scenario_execution_receipt.json)
- Gate I evidence index: [evidence_index_v1.json](f1-gui-acceptance-20260917-r2/I/evidence_index_v1.json)
- Browser evidence, trace archive, screenshot manifest, RuntimeJobSpec, completed JobOutcome, attempt, and one non-test `Primary_Reader_API` receipt are all bound to the same job and current executable SHA.

Gate K was executed separately as an offline supporting acceptance. Its parent
projection is `PASS_OFFLINE`, and its receipt is bound to the current test SHA
`96e6f829e4fc0b99d636190b742129e74226c8b0`:

- Parent result: [parent_acceptance_result_v2.json](f1-k-offline-acceptance-20260917-96e6f829e4fc/parent_acceptance_result_v2.json)
- Gate K evidence index: [evidence_index_v1.json](f1-k-offline-acceptance-20260917-96e6f829e4fc/K/evidence_index_v1.json)
- Contention result: [contention_result.json](f1-k-offline-acceptance-20260917-96e6f829e4fc/K/evidence/K/contention_result.json)
- Durable facts: two independent processes, bounded wait, no corrupt JSON, no lost Registry/Queue update, two offline contention receipts, and unchanged live parent budget.

Gate E was exercised only against a local provider stub, never against an
external F1 route. The latest controlled probe reached the durable provider
receipt boundary and terminated the initial process. The current r14 probe
then resumed in a distinct fresh process. Its durable evidence records one
receipt before interruption and two total receipts after resume, with zero
duplicate receipt IDs and no reexecution of a semantically completed call.
Because the endpoint was a local stub, this is `PASS_OFFLINE_SCOPED`, not a
live-provider PASS. The earlier r13 probe remains retained as a separate
negative unknown-POST boundary. The current durable probe state is:

- Plan: [F1_E_LOCAL_ACCEPTANCE_PLAN_20260917_R14.json](F1_E_LOCAL_ACCEPTANCE_PLAN_20260917_R14.json)
- Parent result: [parent_acceptance_result_v2.json](f1-e-local-acceptance-20260917-r14/f1-e-local-acceptance-20260917-positive/parent_acceptance_result_v2.json)
- Gate E receipt: [scenario_execution_receipt.json](f1-e-local-acceptance-20260917-r14/f1-e-local-acceptance-20260917-positive/E/scenario_execution_receipt.json)
- Evidence index: [evidence_index_v1.json](f1-e-local-acceptance-20260917-r14/f1-e-local-acceptance-20260917-positive/E/evidence_index_v1.json)
- The positive `resume=true` path used only the local controlled stub; no
  external provider call was made.

The C/D/Q result binds every child receipt to checkout SHA
`09477d666a0f31c2b1d95a2a6cb25c00b4e7bfca`, corpus manifest SHA
`f741776ea2eda6b5937f4fc13e40569216eb3173ff597fb80eeea2e46dab3e90`, and
the plan identity above. C recorded one successful HTTP 200 DeepSeek
`Primary_Reader_API` call. D recorded three successful HTTP 200 calls on the
same route; the four calls consumed 49,787 output tokens in total.

Gate I recorded one successful non-test HTTP 200 call to `https://api.deepseek.com`
through `Primary_Reader_API`, with 15 planned/15 sent rendered visual inputs and
`successful_input_mode=multimodal`. The Gate I budget ledger recorded 20,633
output tokens, and the job outcome recorded `source_intake` and `analyze` as
completed with `canonical_ready=true`. The R2 config raised the primary model
and Stage 1 synthesis ceiling to 64,000 tokens after the earlier 32,000-token
attempt correctly failed closed on truncation.

The D production-derived profiles were:

- F1-01: `text_heavy` (27 pages; 6 image pages; 0 OCR pages).
- F1-14: `visual_table_heavy` (11 pages; 11 image pages; 0 OCR pages).
- F1-03: `text_heavy` (20 pages; 9 image pages; 0 OCR pages).

The isolated low-text pages in F1-10 and F1-15 were also rendered and
adjudicated in the machine-only [source ledger](F1_SOURCE_GROUND_TRUTH_20260915.md):
they are a copyright notice, repository/front-matter pages, or selectable-text
figure pages, not scan-primary material.

Therefore C is a verified live gate, but D is not a PASS: the runtime and
Provider receipts are valid while the required three-way modality criterion is
not met. Q was stopped before any Provider call because D remains an unmet
prerequisite; the previously recorded custom-host acknowledgement was valid at
its recorded attempt time but is now expired.

Gate K passing offline does not lift the live F1 prerequisite chain: it proves
the local contention/locking boundary only, not a live Provider, MinerU, or
15-paper Q execution.

The Gate E local r14 probe likewise does not lift the live F1 chain. It proves
the positive local durable resume boundary and cumulative accounting, while
the r13 probe proves that an ambiguous transport-started reservation blocks
rather than silently retrying an unknown request. Live-provider crash/resume
and MinerU recovery remain unverified.

## Separate auxiliary OCR acceptance (Gate J)

The authoritative F1 corpus contains no scan-primary member, so the D
three-way modality gate remains strict. A clearly labelled auxiliary scan was
therefore tested through the existing Gate J contract without entering the Q
15-paper manifest:

- Source label: `F1-AUX-SCAN-01`; staged SHA-256
  `b0ba815d4ddb39944bd3f7ef491661e5a2ad98042eda704bd77388b4eb3feaca`;
  4,307,157 bytes; source is the user-material translation PDF and is not an
  authoritative F1 primary attachment.
- Current-SHA parent: `f1-ocr-aux-20260916-r5-09477d666a0f`; Gate J result:
  `PASS` / parent projection `SCOPED_PASS_OFFLINE`.
- Evidence: [Gate J evidence index](f1-ocr-aux-20260916-r5-09477d666a0f/J/evidence_index_v1.json)
  and [Gate J receipt](f1-ocr-aux-20260916-r5-09477d666a0f/J/scenario_execution_receipt.json).
- Production OCR diagnostics record `ocr_engine=rapidocr`, 17 actual OCR
  pages (`1,2,4,5,6,7,8,9,11,12,13,14,15,16,17,18,19`), matching OCR output
  page/text hashes and the Stage 1 OCR lineage. Stage 1 consumed the OCR text
  and Registry dependency identities were verified. The current-SHA resume
  made no additional Provider call.

This proves the project's OCR step is automatic and operational when a real
scan is present; it does not change the authoritative F1 corpus or make D
pass.

## Why the full parent remains blocked

The earlier live attempt was correctly rejected before transport because owner
authorization had not yet been given. After the explicit authorization, C and D
ran using only extracted text and rendered images; Gate I also completed a real
GUI submission using the same text-plus-rendered-image route. No original PDF
file was sent. The current blocker is corpus modality, not authorization or PDF
format.

To make D pass, the owner must provide an approved source selection whose
production-derived profiles include an actual `ocr_scanned` member, or approve a
revised acceptance corpus/criterion. The existing machine-only source ledger
states that no scan-primary source was established in the current 15-paper
corpus, so the gate must not be weakened or promoted. Q's recorded v2
external-host acknowledgement was valid at the historical attempt time but is
now expired. Q remains correctly blocked by the unmet D prerequisite and
requires a fresh ACK before any custom-host content could be sent; no
custom-host content was sent.

## Acceptance items not verified

- D heterogeneous modality gate and Q full 15-paper chain: Outline v3, evidence
  packets, Writer, validation, citation/source closure, DOCX output, and
  canonical JobOutcome.
- Human original-PDF ground truth, claim-level citation review, negative
  validator challenge, repair/revalidation, and DOCX visual QA.
- F1 primary OCR/scanned-primary flow. Gate I proves the real production
  GUI/Playwright path for one F1-01 PDF analyze job, but it does not prove a
  scanned-primary F1 route.
- Real MinerU create→upload→poll→download→parse and kill/resume.
- Gate K covers offline contention, but a real production crash/resume boundary
  and live cumulative-budget recovery remain unverified.
- Live-provider crash/resume and MinerU recovery remain unverified; Gate E's
  current local r14 evidence is explicitly scoped to `PASS_OFFLINE_SCOPED`.
- Hosted CI for the current branch head is run `35159017730`, `SUCCESS`, 6/6,
  at head `e8b6837ec1165b65c3dce124a3bcd20bfbae6c78`; the current K evidence
  freeze is `96e6f829e4fc0b99d636190b742129e74226c8b0`; the PR remains open and
  unmerged.

## Current-route authorization and protocol recheck (2026-09-17)

The owner supplied an explicit authorization in the current task for F1
extracted text and rendered images to be sent to the configured custom LLM
gateways. A durable v2 acknowledgement was recorded at
[F1_EXTERNAL_HOST_ACKNOWLEDGEMENT_20260917.json](F1_EXTERNAL_HOST_ACKNOWLEDGEMENT_20260917.json)
and bound to the current F1 route fingerprint
`8c5443705306d6a1df9b726579faef434297215ac3b1dbf95a4ce5ba0f608309`, with an
expiry of `2026-09-18T06:20:07.290471Z`. The new authorized Q spec is
[Q_RUNTIME_SPEC_20260917_AUTHORIZED.json](Q_RUNTIME_SPEC_20260917_AUTHORIZED.json).
The admission check passed for exactly `ai.saigou.work` and
`chat.178266.xyz`; `send_original_pdf=never` and `image_transport=base64` remain
in force. The historical Q spec and its blocked run are retained unchanged.

Before any F1 payload was sent, the current routes were probed independently:

- `Writer_API` at `ai.saigou.work/v1/responses`: one real minimal probe,
  HTTP 200, structured response success, five reported output tokens.
- `Outline_API` at `chat.178266.xyz/v1/messages`: two bounded probe runs,
  four physical attempts total, all `transient_network` with no HTTP status.
  A header-only HTTPS check confirmed DNS and TCP/443 reachability but failed
  TLS certificate validation with `SEC_E_CERT_EXPIRED`.

Therefore the authorized Q was not started: Outline is a current route
prerequisite and disabling certificate verification would be unsafe and would
not be a valid acceptance result. No F1 PDF, extracted F1 text, or rendered F1
image was sent by these probes. The F1 acceptance status remains
`NOT_READY_TO_MERGE`; Q is additionally blocked on the expired Outline TLS
certificate/route recovery, while D remains blocked by the strict
`ocr_scanned` modality requirement. The F1 Q configuration uses local parsing,
so no real MinerU task was silently enabled or sent.

## Current-SHA repair and F1 Stage 1 recheck (2026-09-17)

The current repair commit is `5835a7be7c654785da751c6f29aa6223d59f1819`.
It adds bounded semantic correction for the Backup Reader, reserves the exact
Primary/Backup logical-call budget, records deterministic correction variants
in the provider call graph, normalizes routing fields at the publication
boundary, and uses bounded retry for Windows preprocess-generation
publication. The changed production modules and tests passed the current-SHA
focused regression (`59 passed`), and Hosted Windows CI `35226855031` passed
all 6 jobs at this same commit.

The official-DeepSeek-only Stage 1 R10 attempt used the same 15-paper binding,
reused only the previously completed preprocess cache, kept
`send_original_pdf=never`, and loaded the two configured official DeepSeek
credentials from the root `.env` into the child process without writing or
printing them. The runner completed `source_intake` for all 15 sources and
then stopped at F1-01 when DeepSeek returned `fatal_config_or_auth` because
the configured key is invalid. The canonical runner projection is
`job_status=failed`, `stage1_authority_ready=false`,
`provider_receipt_snapshot_count=0`, and `provider_receipts_incomplete`; this
is a real credential failure, not a successful F1 Stage 1 result. The R10
specification is [F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R10.json](F1_STAGE1_FULL_RUNTIME_SPEC_20260917_R10.json),
and its local outcome is under the R10 workspace recorded by the runner.

The preceding R9 attempt was rejected before transport because the sanitized
acceptance config had only template key sentinels. No custom gateway was used
by either attempt. The root `.env` key must be rotated or replaced before a
new official DeepSeek F1 run can be meaningful; no code path will bypass the
credential check.

Current disposition remains:
`CODE_REPAIR_STATUS=PASS_OFFLINE_TARGETED`,
`F1_LIVE_STATUS=BLOCKED_INVALID_DEEPSEEK_CREDENTIAL`,
`D=FAIL_MODALITY`, `Q=BLOCKED`, and
`FINAL_RELEASE_STATUS=NOT_READY_TO_MERGE`.

The compact, secret-free R14 projection is
[F1_STAGE1_R14_EVIDENCE_SUMMARY_20260918.json](F1_STAGE1_R14_EVIDENCE_SUMMARY_20260918.json).

## R11–R14 official DeepSeek Stage 1 recovery (2026-09-17)

R11 isolated the credential discrepancy: the official Primary route produced
three successful DeepSeek receipts, while the configured `deepseek-v4-pro`
Backup credential was rejected as invalid. R12 therefore used the same
already-verified Primary credential for the configured official Backup model,
with that deviation recorded in the R12 spec. R12's first run and resume
correctly stopped on placeholder content and then a length-budget exhaustion;
neither result was promoted to authority. R13 added one finite 32k-to-64k
length retry but still stopped on F1-02 placeholder content after bounded
Primary/Backup retries.

R14 used the source-grounded corrective prompt on executable SHA
`420663e4eea4e9e2a1ffdee1d5d16193d7e5d08e` and completed the full Stage 1
corpus: 15/15 preprocess generations, 15/15 paper artifacts, 18 expected
provider call IDs, and 22 published successful DeepSeek receipts across
Primary/Backup and the selected visual extraction calls. Its provider receipt
closure was complete. The first runner projection exposed a prompt-hash
binding defect in the higher-level closure map; commit
`9e42105e529a964396c52320d67aaa719a047817` fixed that exact declared-variant
check. A no-provider reconcile and status readback using `9e42105e…` returned
`issues=[]`, `completion_status=complete`, `canonical_ready=true`,
`STAGE1_AUTHORITY_READY=true`, and `VISUAL_QUALIFICATION_READY=true`.

This is a real F1 Stage 1 acceptance result, but not full Q or release
acceptance. The controlled R14 credential source differs from the configured
Backup credential, all 15 semantic summaries remain subject to independent
human/original-PDF ground-truth review, and the authoritative F1 profiles
still do not satisfy the strict D three-way requirement: the selected D
members remain F1-01 `text_heavy`, F1-03 `text_heavy`, and F1-14
`visual_table_heavy`, with no production-derived `ocr_scanned` member.
Therefore Q has not started; Outline v3, Writer, Validator repair, DOCX QA,
live MinerU recovery, and governance closure remain unverified.

Current disposition is:
`CODE_REPAIR_STATUS=PASS_OFFLINE_TARGETED`,
`F1_STAGE1_STATUS=PASS_LIVE_CONTROLLED_CREDENTIAL_CONFIG`,
`D=FAIL_MODALITY`, `Q=BLOCKED`, and
`FINAL_RELEASE_STATUS=NOT_READY_TO_MERGE`.
