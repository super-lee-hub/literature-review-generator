# Pipeline Validation Branch Disposition

Audit date: 2026-08-03 (Asia/Shanghai)

The branch `codex/pipeline-validation-update` was reviewed as a source of
possible ideas only. It is not merged, rebased, copied, or cherry-picked into
`codex/platform-hardening-outline-v3`. Its history mixes PPH topic paths,
run-all assumptions, fixed topic IDs, and project-specific workspaces with
potentially reusable state-machine ideas. Any generic behavior below must be
reimplemented against the current `reviewctl`/typed-runtime contracts after
focused tests are added.

| Commit | Subject | Disposition | Reason |
| --- | --- | --- | --- |
| `bf8ccc9` | feat: pipeline and validation updates with expanded test coverage | REJECT_PPH_SPECIFIC | Mixed PPH topic pipeline and project-specific validation bundle |
| `3243e50` | fix(pipeline): unify route truth source, resilient run-all, preserve workspaces | REIMPLEMENT | Route/state ideas may be generic, but the implementation is PPH run-all scoped |
| `ee67b9c` | fix(pipeline): state-driven resume with stage coordinator | REIMPLEMENT | Resume concept is generic; current runtime has a different durable node contract |
| `dc81701` | perf(pipeline): parallel run-all with ThreadPoolExecutor | REJECT_PPH_SPECIFIC | Parallel five-topic run-all behavior is outside this PR |
| `81311ae` | fix(pipeline): thread-safe state init for parallel run-all | REJECT_PPH_SPECIFIC | Same PPH run-all state and topic assumptions |
| `5b43ad6` | fix(pipeline): skip canonical-ready topics, auto-rotate empty workspace shells | REJECT_PPH_SPECIFIC | PPH workspace rotation and topic readiness |
| `d87375e` | fix(pipeline): detect DOCX on disk in inspect_topic_progress | REJECT_PPH_SPECIFIC | PPH topic progress inspection and on-disk assumptions |
| `5e48309` | fix(pipeline): auto-rotate job_id on workspace collision, fix S01 expected_sections | REJECT_PPH_SPECIFIC | Fixed S01 topic and PPH workspace collision policy |
| `3242e05` | fix(pipeline): recognize outline completion without adoption, relax section count check | REIMPLEMENT | The generic distinction is useful, but the PPH completion rule must not be copied |
| `92114e5` | fix(pipeline): downgrade section contract mismatch to warning, allow adoption | REJECT_PPH_SPECIFIC | Would weaken the current fail-closed outline adoption contract |
| `3ab953e` | fix(pipeline): recognize review completion from on-disk DOCX | REJECT_PPH_SPECIFIC | PPH review-batch completion heuristic and DOCX projection assumption |
| `76906f6` | fix(validation): downgrade section contract mismatch to warning | REJECT_LEGACY_V2 | Conflicts with current semantic validation and fail-closed evidence policy |

No files from this branch are included in the current audit commit. The
current implementation must not import `scripts/pph_*`, `scripts/fix_*`,
`tmp_check_snapshot.py`, PPH paper IDs/paths/topic rules, direct Registry JSON
edits, or manual DOCX patching.
