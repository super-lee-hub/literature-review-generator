# Migration History

> Audience: maintainers / AI agents.
> This document tracks migration-era behavior and compatibility decisions.
> Source: the archived migration-era record in [`../../../migrations/`](../../../migrations/).

## Week 1 Migration Notes

- Old project outputs can still be detected, but they are not the durable execution target anymore.
- Real artifacts now belong in `output/<project_name>__<job_id>/`.
- `output/<project_name>/` is reserved for `_latest_job.json` only.

## Legacy Output Recognition

Legacy projects are recognized by files such as:
- `<project>_summaries.json`
- `<project>_checkpoint.json`
- `<project>_literature_review_outline.md`
- `<project>_review_checkpoint.json`
- `<project>_literature_review.docx`

## Resume Semantics

- `strong_resumable`: A readable summaries artifact exists, `stage1_progress_snapshot.json` exists, fingerprint bundle matches the current request
- `weak_resumable`: Summaries exist, no compatible progress snapshot exists, user should prefer a fresh rerun
- `non_resumable`: Required artifacts are missing or fingerprints differ

## summaries-only Legacy State

- Legacy `summaries-only` state is treated as `weak_resumable`
- Must not be silently upgraded to strong resume

## Checkpoint Compatibility

- Legacy `*_checkpoint.json` remains readable during migration
- Not the primary recovery source anymore
- Primary recovery source: workspace, progress snapshot, artifact registry, fingerprint bundle

## When A Fresh Rerun Is Required

- Input source fingerprint changes
- Config fingerprint changes
- Pointer points to a missing workspace
- Summaries are unreadable
- Progress snapshot is stale or mismatched

## Direct PDF vs Zotero

Both modes must normalize into a shared source descriptor before paper processing:
- Direct PDF mode starts with weak metadata and local file-derived identity
- Zotero mode starts with structured metadata and library-backed identity
- Both converge into `SourcePaperDescriptor` before downstream processing

## Citation Manifest V2 (Week 6)

### Runtime Truth Source Upgrade

`citation_manifest_v2.json` is now the primary citation truth source. `citation_manifest_v1.json` is kept as an explicit compatibility projection.

**V2 Structure:** `occurrences`, `clusters`, `bibliography`

**Migration Path:** Existing v1 manifests are auto-migrated to v2 via `migrate_v1_to_v2()`. Validator now consumes `occurrences` as primary input (with fallback to `citations`).

**File Locations:**
- Primary: `output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v2.json`
- Compatibility: `output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v1.json`
