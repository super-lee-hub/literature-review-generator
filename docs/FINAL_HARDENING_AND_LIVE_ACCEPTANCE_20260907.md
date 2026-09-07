# Final hardening and live acceptance — 2026-09-07

## Release decision

`NOT_READY_TO_MERGE`

This report is evidence-bound. The hardening code was committed locally at
`d3ca43a5330cf5ebb0cb4fb91866c8590b956f53`. It has not reached GitHub because
the authenticated OAuth credential rejected the workflow-file update for
missing `workflow` scope. No merge was attempted.

The current local executable SHA is therefore `d3ca43a5330cf5ebb0cb4fb91866c8590b956f53`.
The active PR's remote head remains the pre-hardening
`2e6bdb32aca84c8d948adc846e3db76cd15b46b5` until a credential with the
required GitHub scope is supplied.

## Git and PR state

| Item | Current evidence |
|---|---|
| Remote `main` | `4a5d56e83bf00a7eea529115798c772e0e1f15d6` |
| Local hardening commit | `d3ca43a5330cf5ebb0cb4fb91866c8590b956f53` |
| Active PR | #24, `OPEN`, non-draft, base `main` |
| Active PR remote head | `2e6bdb32aca84c8d948adc846e3db76cd15b46b5` |
| PR #24 Hosted run | `34017515111`, `SUCCESS`, but on the old remote head |
| PR #23 | `MERGED` at `4fa4e57aedc770dc18824ac15265fc36836ec791`; old head `50c0917c...` |

The SSH push was reset by the remote. HTTPS reached GitHub but was rejected:
the OAuth App is not permitted to update `.github/workflows/windows-tests.yml`
without the `workflow` scope. An alternate SSH-over-443 workaround was not
used because it would bypass the current authorization boundary.

## Bugs found and hardened

- Credential source selection was implicit and could silently mix process
  environment, dotenv, and `config.ini` values. It is now deterministic:
  `process environment > .env beside the selected config > config.ini`.
  Conflicting meaningful values fail closed. Diagnostics expose source and
  presence booleans only; they never expose values, hashes, or headers.
- Template/sentinel credentials and malformed required URLs now fail before
  transport. Template/example configuration remains usable for read-only
  doctor inspection, but not production execution.
- Provider admission now follows the formal StagePlan, so an analyze-only
  primary-reader run does not require an unused Backup Reader or Writer route.
- `reviewctl preflight` now builds the same route, proxy policy, payload shape,
  timeout, retry, and request identity used by formal transport, with zero HTTP
  calls.
- Queue JSON corruption is preserved in a quarantine copy and raises a typed
  failure instead of becoming an empty queue. Queue locks and atomic writes
  are bounded; internal `TypeError` is not used as a signature probe, so a job
  executes once.
- Registry, queue, latest-pointer, config/.env, publication, and receipt-ledger
  replacements use the shared bounded atomic-replace helper. Registry rollback
  and publication paths are covered by the same Windows sharing policy.
- Workspace/project/job components, lexical traversal, resolved paths, and
  reparse-point writes are fail-closed. Existing reparse leaves can be named
  for inspection so the review-batch guard reports its domain-specific error;
  actual workspace creation rejects them.
- External Registry closure now verifies an immutable revision-bound snapshot,
  proves the external revision is unchanged after recursive verification, and
  retries a bounded three times on change without holding arbitrary cross-
  Registry locks.
- GUI config and `.env` persistence stages/fsyncs both files and rolls back the
  first publication when the second fails. Control characters are rejected and
  `.env` permissions are best-effort owner-only.
- Provider endpoints are classified as official, third-party gateway, or
  custom/local. GUI formal use and connection tests require an explicit
  acknowledgement for third-party gateways; doctor/preflight show the
  classification.
- MinerU handling now bounds streamed response bytes, ZIP entries, total
  uncompressed bytes, per-entry bytes, compression ratio, structured JSON,
  markdown, and plain text. The exact HTTPS host allowlist remains in force.
- A Python 3.11/Windows lock and `pip check` CI step were added. The interactive
  setup wizard now supplies defaults for all new MinerU limits.
- `scripts/release_acceptance.py` is offline by default, requires an explicit
  spec/budget/preflight and `AUTO_GENERATE_RUN_LIVE_ACCEPTANCE=1` for real
  runs, uses only `reviewctl`, applies a wall-clock timeout, and maps missing
  credentials to `BLOCKED_CREDENTIALS` rather than PASS.

## Effective runtime route table

The table below is generated from the current shipped `config.ini.example`
through `reviewctl preflight`; the credential values used for that no-network
probe were synthetic local sentinels and were never sent to a provider.

| Semantic role | Config section | Model | Protocol/route | Endpoint classification |
|---|---|---|---|---|
| Stage 1 Primary Reader | `Primary_Reader_API` | `deepseek-v4-flash-vision-exp` | DeepSeek Chat Completions at `https://api.deepseek.com/chat/completions` | Official DeepSeek host |
| Stage 1 Backup Reader | `Backup_Reader_API` | `deepseek-v4-flash` | DeepSeek Chat Completions at `https://api.deepseek.com/chat/completions` | Official DeepSeek host |
| Outline candidate generation | `Outline_API` | `claude-opus-5` | Anthropic Messages at `https://chat.178266.xyz/v1/messages` | Third-party gateway |
| Outline arbitration | `Outline_API` | `claude-opus-5` | Anthropic Messages at `https://chat.178266.xyz/v1/messages` | Third-party gateway |
| Outline relation adjudication | `Free_Mode_API` | `deepseek-v4-pro` | DeepSeek Chat Completions at `https://api.deepseek.com/chat/completions` | Official DeepSeek host |
| Outline coverage critique | `Free_Mode_API` | `deepseek-v4-pro` | DeepSeek Chat Completions at `https://api.deepseek.com/chat/completions` | Official DeepSeek host |
| Outline structure critique | `Writer_API` | `gpt-5.6-sol` | OpenAI Responses-compatible route at `https://ai.saigou.work/v1/responses` | Third-party gateway |
| Outline evidence critique | `Writer_API` | `gpt-5.6-sol` | OpenAI Responses-compatible route at `https://ai.saigou.work/v1/responses` | Third-party gateway |
| Stage 3 Writer | `Writer_API` | `gpt-5.6-sol` | Same Responses-compatible route, once per adopted Outline v3 section | Third-party gateway |
| Validation | `Validator_API` | `deepseek-v4-flash` | DeepSeek Chat Completions at `https://api.deepseek.com/chat/completions` | Official DeepSeek host |

“OpenAI-compatible” or “Anthropic-compatible” describes the wire protocol; it
does not make `ai.saigou.work` or `chat.178266.xyz` an official OpenAI or
Anthropic endpoint.

## Profile and Stage 3 terminology

“生成 profile” means Free Mode calls `generate_free_mode_profile` to turn a
research idea into a structured planning context (research goal, concept
relationship, focus points, and related fields), stores it as a workspace-bound
JSON input, and projects it into the typed `ReviewIntent` used by the Writer.
It is not a personal user profile and it is not a completed review.

“Stage 3 Review/Writer” is one stage at two levels: Stage 3 is the review
generation stage, while `Writer_API` is the provider/model called once for each
adopted Outline v3 section. It is not two independent pipeline stages.

## Verification evidence

| Check | Result |
|---|---|
| `compileall` on changed runtime, services, GUI, setup, scripts, tests | PASS |
| Pyright with the project Python 3.11 environment | `0 errors, 0 warnings, 0 informations` |
| `pip check` | `No broken requirements found.` |
| `git diff --check` | PASS; only expected LF/CRLF normalization notices |
| Hardening + MinerU + setup + hygiene focused slice | `45 passed` |
| Final strict-offline command | `1402 passed, 12 failed, 1 skipped, 1 deselected` |

The 12 final local failures all occur before the test body at
`multiprocessing.get_context("spawn").Queue()` / Windows named-pipe creation
with `PermissionError: [WinError 5]`. They are:

- `test_queue_claim_leases.py`: single-winner claim, stale-worker recovery
  publication, queue-owned Registry publication, generation fencing, and
  staged JSON/DOCX/export publication;
- `test_queue_multiprocess_leases.py`: three multiprocess lease/fence tests;
- `test_registry_transactions.py::test_two_processes_register_without_lost_update`;
- `test_review_batch.py::test_review_batch_same_derivation_has_one_cross_process_writer`;
- `test_runtime_lifecycle_parity.py::test_concurrent_explicit_job_claim_rejects_loser_before_workspace_mutation`;
- `test_validation_adjudication_checkpoint.py::test_adjudication_checkpoint_single_flights_across_processes`.

This sandbox cannot create the named pipes required by those tests. They were
not skipped or weakened. The exact final Hosted Windows run is therefore still
required and was not obtained because the push was blocked by GitHub scope.

`reviewctl doctor --config config.ini.example` was read-only with
`provider_network_calls=0`, `ok=true`, and expected warnings for absent real
credentials/stale local locks. Template `reviewctl preflight` failed closed
with `network_calls=0`; a separate synthetic-key preflight exercised all
`run_all` routes and returned `status=pass`, also with `network_calls=0`.

## Acceptance gates

| Gate | Result | Evidence/boundary |
|---|---|---|
| 1 Offline regression | NOT_VERIFIED | 1402 passed, but 12 local named-pipe failures; Hosted exact-SHA not rerun |
| 2 Exact-path provider preflight | BLOCKED_CREDENTIALS | Active worktree has no production `config.ini`/`.env`; example config correctly rejects template credentials |
| 3 One real F1 paper | BLOCKED_CREDENTIALS / BLOCKED_INPUT | No live credential, authoritative F1 spec, or corpus in active worktree; no `reviewctl run` live call |
| 4 Three heterogeneous papers | BLOCKED_CREDENTIALS / BLOCKED_INPUT | Not started because Gate 3 prerequisites are absent |
| 5 Real cancel/crash/resume | NOT_VERIFIED | No real three-paper process lifecycle run |
| 6 Live multi-provider Outline | BLOCKED_CREDENTIALS | No real gateway calls; synthetic preflight is not live evidence |
| 7 Real Free Mode | BLOCKED_CREDENTIALS | No real Free_Mode_API call; profile/intent fixture contracts only |
| 8 Real Validator | BLOCKED_CREDENTIALS | No real Validator call or current F1 closure |
| 9 GUI Playwright | NOT_VERIFIED | No browser automation run in this round |
| 10 Heavy OCR | NOT_VERIFIED | No live OCR acceptance sample/run |
| Q Full F1 15-paper chain | BLOCKED_INPUT | `paper_artifact = 0/15` live evidence; no Outline/Review/Citation/DOCX/Validator closure |
| R Negative live behavior | NOT_VERIFIED | Offline provider taxonomy tests exist; no live/mock acceptance gate run separately |
| S Secret/privacy scan | NOT_VERIFIED | Report/receipt redaction and `.env` guard code tested; full release scan not run |
| T Governance | NOT_VERIFIED | PR state read; branch-protection configuration remains owner action |

Actual live Provider transport count is **0**. Actual F1 15-paper artifact
count is **0/15**. Outline, Review, Citation, DOCX, Validator, repair, GUI,
OCR, and live MinerU counts are not claimed.

## Remaining owner actions

1. Authenticate GitHub with a credential authorized to update workflow files
   (`workflow` scope), then push local commit `d3ca43a...` to PR24. Do not
   merge until the new Hosted Windows run is terminal and green on that exact
   SHA.
2. Provide/select the real F1 runtime spec, 15-paper corpus, and owner-approved
   rotated credentials. Run the release entrypoint gates in order with explicit
   call/token/timeout budgets; stop on the first failure.
3. Run the 12 cross-process tests in a normal Windows context that permits
   multiprocessing named pipes and record the exact result.
4. Execute the real one-paper, three-paper resume, multi-provider Outline,
   Free Mode, Validator, GUI, OCR, and 15-paper chain gates. Do not replace
   them with synthetic transports or `/models` checks.
5. Inspect branch protection/status checks as a repository owner action.

No Provider credential was rotated, revoked, printed, or written by this
round. No live API, Playwright, heavy OCR, or paid multi-provider call was
made.

**FINAL_STATUS: NOT_READY_TO_MERGE**
