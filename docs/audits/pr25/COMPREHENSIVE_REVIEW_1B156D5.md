# PR25 comprehensive review — executable `1b156d5d`

**Overall status: NOT_READY.** This report uses the supplied V2/V3 materials as acceptance criteria and evidence. Their 40–60 logical-call target, proposed 80-POST line, and model/agent workflow recommendations are not provider authorization. The application hard limit remains 24.

## Identity and PR

- Tested executable: `1b156d5d5de4408645af324ee2e02bdc3e2b670f`, branch `codex/pr25-v4-controlled-pilot`.
- Base: `c1ad0da869bc68869a521f60bbda07342fb16058`; working tree clean at test start.
- PR #25 remains OPEN at remote `c1ad0da869bc68869a521f60bbda07342fb16058`; current local SHA was not pushed or merged. Hosted CI for this SHA is `NOT_RUN`.

## Current offline evidence

- Full offline suite: **2,126 passed, 4 skipped, 26 deselected, 0 failed** in 55:24. Markers excluded: `live_api`, `live_acceptance`, `playwright`, `heavy_ocr`; `tests/conftest.py` installs the offline network guard. JUnit SHA-256: `794c3becc30a70e20a1c8e40eb2c979b3ce10ffcf3a62488b88c7dd80c818fd2`.
- Chromium GUI suite: **24 passed** with the explicit opt-in and isolated local/loopback fixtures; provider posts 0. JUnit SHA-256: `25686fcf5ca199437c5d824832e2c069766b3179eefa9c7f1d1130889087cfc8`.
- Targeted after the reducer-reserve correction: role-shadow 6 passed; current production Runner E2E 1 passed; current runtime E2E 4 passed.
- Pyright 0 errors/warnings/informations; Ruff `E9,F`, `compileall`, and `git diff --check` pass.

The 3-paper false rejection was caused by counting every unused reducer-stage slot again after the planner had materialized reducer levels from runtime fragment bounds and output caps. Commit `1b156d5` removes that duplicate reserve while retaining the actual 24-call limit. The new regression and Runner E2Es pass.

## V3 and V4 requirements

See [the versioned acceptance matrix](FINAL_ACCEPTANCE_MATRIX_1B156D5.json) for production paths and test names. V3-01–05 have offline regression coverage; V3-03 carries a caveat around the shared-blueprint path, and V3-04 is scoped offline study attribution only. V4-01–06 have offline coverage for frozen relation scope, bounded claim-equivalence review, variant parity, fail-closed critique, second-executor replay, and a unified coverage/health quality decision. None of these offline results are live provider acceptance.

### Current 63-source plan and budget boundary

The current provider-free plan closes all **1,016 source claim identities** and **1,016 evidence identities** on the topic wire, with no missing or extra IDs. It has **33 topic batches**, **74 runtime cross-group fragments**, and **74 planner items**; per-fragment bounds are materialized. The 24-call cap blocks at the known 33-topic lower bound. Under the default application config, the cross-group reducer plan also exceeds the 24-call stage guard. Under the 12,288-output V8 config, one complete cross-group result plus wrapper exceeds the effective 32,000-token input cap. Shadow 48/64/80 full upper bounds remain `incomplete_upper_bound`; the old 161-call result is invalid.

The current full-stage projection records **19 unknown call-count exposures**. Their per-call envelopes are bounded, but aggregate calls/retries/tokens/time/cost are not; `ready_for_provider_admission=false`.

The 63 source units are **0 explicit study IDs + 63 paper-level fallbacks**. 45 dossiers have unresolved multi-study mapping; 43 source-field ledger rows remain unresolved. These counts are not claims that 45 papers have verified multiple studies or that 43 facts were lost.

The 11 legacy runtime-control test functions all passed in the full suite. One evidence caveat: the explicit resume test verifies Store + ProviderRuntime admission, not a full public CLI resume lifecycle.

## Paid R1, F1, and delivery

- Full R1 remains blocked before provider transport by the 24-call/topic lower bound and incomplete full-stage budget.
- Historical B01 received HTTP 200 but `finish_reason=length`, 12,288 output tokens, invalid response, no retry, and `canonical_ready=false`; it is not successful R1 evidence.
- The [V8 permission card](D:/tmp/pr25-v8-opus55-output-proposal-20260930-1b156d5/OWNER_PERMISSION_CARD_V8.md) binds B01/B05/B29 to `claude-opus-5-5` at `api.yhlxj.ai`, at most 3 POSTs, 36,864 output tokens, zero retries, one hour, and unknown currency cost. Its preflight passed with 0 network calls; the execution gate rejects without a current approval record. **No V8 POST has been made; exact V8 approval is pending.**
- F1 is separate and remains `NOT_EVALUATED`. No final adopted R1 DOCX exists for visual review, so DOCX layout remains `NOT_VERIFIED`.
- A current source recovery patch/ZIP is being prepared; no push, merge, or main-branch change occurred.

## Subagent evidence

A read-only verifier mapped V3/V4 and the legacy controls; it changed no files and made no provider/network calls. The role catalog specifies `gpt-5.6-luna`/high, while the agent self-reported `GPT-5` and said effort was not observable. Runtime metadata is unavailable, so model/effort compliance is **not verified**; older audit-agent outputs were produced at SHA `860420c` and were not treated as current acceptance evidence. See `SUBAGENT_LEDGER_1B156D5.json`.
