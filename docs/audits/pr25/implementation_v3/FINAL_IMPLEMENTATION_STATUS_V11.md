# PR25 implementation follow-up V11

Date: 2026-09-30
Status: **PARTIAL / NOT READY**

This report covers the current local implementation worktree. It does not claim that the full R1 or F1 product path is complete.

## Source and remote identity

- Local source checkout: `D:\tmp\pr25-v4-recovery-current-20260928-f`
- Branch: `codex/pr25-v4-controlled-pilot`
- HEAD: `4bd05b10a8a744e9891b650324c718133f5da599`
- The local worktree has 22 modified tracked files, two new regression test files, and new audit report/matrix/ledger/manifest files. No commit or push was made.
- GitHub PR #25 remains OPEN. `git ls-remote` returned `c1ad0da869bc68869a521f60bbda07342fb16058` for both the PR branch and `refs/pull/25/head`; the local repairs are not on that remote SHA. [PR #25](https://github.com/super-lee-hub/literature-review-generator/pull/25)
- No hosted CI run was started for the local changes.

## Implemented and locally verified

| Review item | Current disposition | Evidence |
|---|---|---|
| V3-01, effective input cap | Uses one effective cap bounded by the user limit, route capacity, and the 32,000-token transport ceiling. Relation and candidate requests split before they reach the provider. | `tests/test_pr25_v3_regressions.py` has the 37,397/32,000 and exact-boundary cases. |
| V3-02, section identity | Independent shard-local section IDs remain separate unless a shared global blueprint binds them. | `test_v3_02_same_local_section_id_in_independent_shards_stays_separate` |
| V3-03, claim support | Merged claims retain their support and provenance; conflicting support fails closed. | `test_v3_03_*` regressions |
| V3-04, paper/study scope | Study support is checked for the claim's paper and study identity, not every same-numbered study in a section. Wrong paper, wrong study, and missing-condition controls remain negative. | `test_v3_04_*` regressions |
| V3-05, empty relation selection | Missing selection metadata retains its documented legacy behavior; an explicit empty list schedules no relation POST. | `test_v3_05_*` regressions |
| Provider-call ceilings | `24` is now the default for a new run, not a universal ceiling. Explicit run limits such as 33, 80, and 500 are preserved and remain subject to the run's aggregate budget. | `test_explicit_run_call_budget_is_not_truncated_to_24`; V2 budget tests |
| Retry and zero-budget semantics | New V2 budgets require explicit limits and interpret zero as zero. Unversioned persisted budgets remain V1 and keep their historical zero semantics. | `tests/test_pr25_final_hardening_regressions.py` |
| Numeric and typed source fields | Numeric zero survives semantic extraction, ledger round-trip, and provider request projection. Keyed source fields retain path, scope, identity, hash, and disposition. | `test_source_field_ledger_round_trip_preserves_numeric_zero`; `test_topic_provider_wire_carries_keyed_mapping_source_fields` |
| Pre-transport admission | Local call-cap, missing stability transport, and malformed fixture checks happen before durable aggregate reservation. | Final hardening regressions |
| Candidate 2 | Candidate 2 can be selected; the selected-candidate and revision hashes stay bound to candidate 2; the run stops at `ready_for_adoption`. | `test_arbitration_can_select_candidate_two_and_preserves_its_revision_hash` |
| Writer-to-draft lineage | Outline v3 drafts bind the current final outline, current adoption, and immutable Writer sections. Publication verifies section order/content; the reconciler applies the same strict lineage validator. Legacy draft modes remain supported. | `tests/test_review_draft_v3_lineage.py` |

The running `[Outline_API]` configuration is already `claude-opus-5-5`. I updated the workspace `config.ini.example` from `claude-opus-5` to `claude-opus-5-5`; the existing host and transport settings were preserved. The current source template and configuration-service default also resolve to 5.5. Historical receipts that used `claude-opus-5` were left unchanged.

## Verification

All verification below was provider-free. No test contacted a live provider.

- `tests/test_pr25_final_hardening_regressions.py`: **39 passed**.
- `tests/test_review_draft_v3_lineage.py`: **5 passed**.
- The targeted topic-pilot resume regression passed after correcting the test helper to preserve its explicitly selected two batches.
- The provider-free keyed-source-field wire test passed; candidate 2 selection passed.
- Configuration, model-selection, public-default, and Anthropic transport tests: **119 passed**.
- The local three-PDF runtime-to-verified-export fixture: **2 passed in 549.07 seconds**. This uses local fixtures and does not establish real R1 acceptance.
- The local fixture produced the export path, but DOCX page rendering, Chinese pagination, and GUI/real-browser acceptance were not reverified on this final local source.
- A broader pre-final targeted run reported **322 passed, 1 skipped, and one test-helper failure**. The failure was reproduced, corrected, and rerun successfully by itself. The entire broader command was not rerun after the final zero-limit and reconciliation changes.
- `py_compile` passed for the changed Python implementation and test files. `git diff --check` found no whitespace errors; Git printed only existing LF-to-CRLF working-copy warnings for three tests.

## Provider-free request planning

The current request serializer materialized requests from the verified 63-summary pack (`079da38604d1fe4bbdbb6869bbce24cca9e6dafdd0f0ba51d31ec41ed79976a0`). It emitted zero provider POSTs and zero network calls.

| Scale | Data | Topics | Topic batches | Estimated input total | Largest estimated input | Planning time |
|---:|---|---:|---:|---:|---:|---:|
| 10 | First 10 entries from the verified pack | 11 | 10 | 228,092 | 31,540 / 32,000 | 2.832 s |
| 63 | Full verified typed-summary pack | 64 | 55 | 1,389,145 | 31,980 / 32,000 | 105.274 s |
| 200 | Synthetic repeated summaries with unique paper IDs | Not completed | Not completed | Unknown | Unknown | Process ended before a completed row after about 706 seconds elapsed |
| 500 | Not run | — | — | — | — | Omitted because the synthetic 200-paper full plan did not complete |

For completed topic batches, the output reserve was 12,288 tokens per request. These are estimates for the topic-synthesis stage only; they exclude relation adjudication, candidates, critiques, arbitration, Writer, Validator, repairs, and any complete R1 total. The 200/500 entries would be synthetic scaling tests, not additional real studies. The 63-paper maximum estimate is only 20 tokens below the configured input cap, so this planning result does not establish a reliable full-stage run.

The prior V15 full-stage projection remains **not ready**: 14 possible provider exposures, 3 with a logical-call bound, and 11 still unbounded. No new full-stage projection closes those exposures in this turn.

## Live provider and authorization boundary

- Earlier V6 and V9 B01 calls each received HTTP 524 with usage unreported. Their currency charges remain unknown. V6 stopped after B01, as its card required; B05/B29 were not sent under that card.
- The local V11 materializer produced new hashes that differ from the previously approved V10 B01 hash. The V10 card cannot authorize these new request bytes.
- A proposed V11 card is ready at `D:\tmp\pr25-final-hardening-20260930\OWNER_PERMISSION_CARD_V11_PROPOSED.md`. It covers one current-source, one-topic, one-paper request (`topic_synthesis_provider:batch:29`), estimated input 13,684 tokens, output allowance 12,288, at most one POST, zero retries, and an unknown currency charge. It makes no request to continue into other batches or stages.
- No V11 provider POST has been emitted. Full R1 is **not complete** and paid full-R1 admission remains blocked pending a complete finite stage plan and exact current-scope approval.

## Cleanup and agent evidence

The old pytest scratch tree was moved, not deleted, to `D:\tmp\pr25-cleanup-quarantine-20260930\.pytest-tmp-20260915-escalated`. Post-move verification found 72,531 files totaling 825,837,327 bytes, including 90 `.env` files and 108 `.receipt` files. The move removes it from the project tree but does not free disk space. A restore note and inventory are beside it.

Four bounded agents were used. Their launch requests specified `gpt-6-luna/max`; the available agent results do not expose enough runtime metadata to verify the actual model variant or reasoning effort. See `SUBAGENT_LEDGER_V11.json`.

## Acceptance matrix

- `OFFLINE_PRODUCT`: **PARTIAL / LOCAL FIXTURE PASS WITH LIMITS**
- `PAID_R1_ADMISSIBLE`: **BLOCKED** — current full-stage exposure and scaling plan are not closed
- `LIVE_R1`: **NOT COMPLETE** — historical partial B01 524 receipts only; no V11 call
- `F1`: **NOT RUN / NOT VERIFIED**
- DOCX visual QA: **NOT VERIFIED**
- GUI/browser acceptance on this final source: **NOT RUN**
- PR #25: **OPEN / UNMERGED**; the remote still points to `c1ad0da869bc68869a521f60bbda07342fb16058`

See `FINAL_ACCEPTANCE_MATRIX_V11.json`, `SUBAGENT_LEDGER_V11.json`, and `SOURCE_MANIFEST_V11.json` for machine-readable status and source file hashes.
