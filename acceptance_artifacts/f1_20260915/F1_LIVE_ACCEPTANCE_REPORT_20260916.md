# F1 C→D→Q acceptance report

Status: `BLOCKED_AUTHORIZATION / NOT_VERIFIED`.

This report records the current executable and the exact evidence boundary. It
does not upgrade historical F1 outputs, local fixtures, or no-call preflight
into live acceptance.

## Frozen identities

- Code executable SHA: `12bf400d3cf6fd7711da8750856e25ebc7d0da85`
- Current evidence checkout at the latest dry-run: `f392c8475686b7c28204c967b0a699b98fcd2e2d`.
- The stored plan intentionally leaves `final_executable_sha` blank so a future authorized run binds the exact clean checkout at execution time. The latest dry-run below is bound to the current evidence checkout; a later evidence-only commit does not change production code but will require a fresh receipt for any live claim.
- Branch: `codex/f1-validation-authority-closure`
- Acceptance plan: [F1_ACCEPTANCE_PLAN_20260915.json](F1_ACCEPTANCE_PLAN_20260915.json)
- Plan SHA-256 at the latest dry-run: `9640c9fa1228e8be983db939b804520ed6a422b5e89dde9b1c862b445c03a6cc`
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

The full `run_all` Q spec would additionally reach custom `Outline_API` and
`Writer_API` hosts (`chat.178266.xyz` and `ai.saigou.work`). No current v2 ACK
for those routes is present in the acceptance plan, and the Q runtime spec is
marked `provider_calls_allowed=false` until that route authority is provided.

A fresh no-network `run_all` preflight of the acceptance config resolved all
configured roles and reported `network_calls=0`, with the following exact
external set and route identity:

- required external hosts: `ai.saigou.work`, `chat.178266.xyz`
- route fingerprint: `ab98b7233f2ac6bb205d6a6992260880fd66a684f0cb47570a328c1869ab7a1d`
- reachable role routes: Primary Reader on DeepSeek; Outline candidate/
  arbitration on Anthropic; Writer and structure/evidence critique on the
  configured OpenAI-compatible gateway; Validator and Free Mode on DeepSeek
- MinerU: `remote_parser_not_requested`

Those model and endpoint settings are configuration facts, not an automatic
external-host acknowledgement. The v2 ACK must still be supplied explicitly
for the two custom hosts before Q can send review content.

## C/D/Q plan and dry-run

| Gate | Selection | Runtime spec SHA-256 | Dry-run status | Provider calls |
|---|---|---|---|---:|
| C | F1-01 | `38918dc7734caadbc9333900c094595f80cd42f59dcc4666b4cf2123d930fdb9` | BLOCKED: owner authorization required | 0 |
| D | F1-01, F1-03, F1-14 | `0031c9091e13632897fdaa2d3aa61ed6e4346b487275f02bfdd62213b0708483` | BLOCKED: prerequisite C not passed | 0 |
| Q | F1-01…F1-15 exact set | `3fb64ab7ce960955d09a80f36a0e217d56803bdca3838eba493e255011b1b891` | BLOCKED: prerequisite D not passed | 0 |

The no-network command was:

```text
python -m reviewctl acceptance-run --acceptance-spec acceptance_artifacts/f1_20260915/F1_ACCEPTANCE_PLAN_20260915.json
```

Its latest durable parent result is [parent_acceptance_result_v2.json](f1-acceptance-20260915-f392c8475686/parent_acceptance_result_v2.json).
The result binds every child receipt to checkout SHA
`f392c8475686b7c28204c967b0a699b98fcd2e2d`, corpus manifest SHA `f741776e...`, and the plan SHA above. The
child receipts are:

- C: [scenario_execution_receipt.json](f1-acceptance-20260915-f392c8475686/C/scenario_execution_receipt.json)
- D: [scenario_execution_receipt.json](f1-acceptance-20260915-f392c8475686/D/scenario_execution_receipt.json)
- Q: [scenario_execution_receipt.json](f1-acceptance-20260915-f392c8475686/Q/scenario_execution_receipt.json)

All three are `status=BLOCKED`; the parent is `status=BLOCKED`,
`live_pass=false`, `ready_to_merge=false`. The acceptance budget was not
started (`provider_budget_state_path` is empty in the dry-run result), and no
Provider, Writer, Outline, Validator, or MinerU transport occurred.

## Why live C/D/Q did not run

The security boundary rejected the attempted live command because it would
export private F1 PDF contents to `api.deepseek.com` using credentials loaded
from `.env`, without an explicit direct user approval for that payload and
destination. No workaround or indirect network path was used.

To resume, the owner must explicitly authorize the bounded C/D payload and
destination, then run the acceptance plan with its live authorization in a
network-enabled environment. Q additionally needs a fresh exact v2 external
host acknowledgement covering the current custom Outline/Writer route
fingerprint; the code must not synthesize `acknowledged=true`.

## Acceptance items not verified

- C/D real Provider execution and Provider receipts.
- Q full 15-paper chain: Outline v3, evidence packets, Writer, validation,
  citation/source closure, DOCX output, and canonical JobOutcome.
- Human original-PDF ground truth, claim-level citation review, negative
  validator challenge, repair/revalidation, and DOCX visual QA.
- Production GUI/Playwright flow and real OCR/scanned-primary flow.
- Real MinerU create→upload→poll→download→parse and kill/resume.
- Hosted CI for the new checkout SHA `f392c847...` (the remote PR still has the old
  head because push was unavailable).
