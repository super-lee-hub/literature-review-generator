# MIGRATION_NOTES.md

## Purpose

This document tracks operational migration behavior introduced by the job-workspace execution model.

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

- `strong_resumable`
  - A readable summaries artifact exists
  - `stage1_progress_snapshot.json` exists
  - Fingerprint bundle matches the current request
- `weak_resumable`
  - Summaries exist
  - No compatible progress snapshot exists
  - User should prefer a fresh rerun
- `non_resumable`
  - Required artifacts are missing or fingerprints differ

## summaries-only Legacy State

- Legacy `summaries-only` state is treated as `weak_resumable`.
- It must not be silently upgraded to strong resume.

## Checkpoint Compatibility

- Legacy `*_checkpoint.json` remains readable during migration.
- It is not the primary recovery source anymore.
- Primary recovery source is:
  - workspace
  - progress snapshot
  - artifact registry
  - fingerprint bundle

## When A Fresh Rerun Is Required

- Input source fingerprint changes
- Config fingerprint changes
- Pointer points to a missing workspace
- Summaries are unreadable
- Progress snapshot is stale or mismatched

## Direct PDF vs Zotero

Both modes must normalize into a shared source descriptor before paper processing.

- Direct PDF mode starts with weak metadata and local file-derived identity.
- Zotero mode starts with structured metadata and library-backed identity.
- Both converge into `SourcePaperDescriptor` before downstream processing.

## Citation Manifest V2 (Week 6)

### Runtime Truth Source Upgrade

The citation manifest has been upgraded from v1 to v2 as the primary durable artifact:

**What Changed:**
- `citation_manifest_v2.json` is now the **primary** citation truth source
- `citation_manifest_v1.json` is kept as an explicit **compatibility projection**
- Registry now registers v2 as the canonical artifact version

**V2 Structure:**
```json
{
  "artifact_type": "citation_manifest",
  "artifact_version": "v2",
  "occurrences": [...],
  "clusters": [...],
  "bibliography": [...]
}
```

**Migration Path:**
- Existing v1 manifests are auto-migrated to v2 via `migrate_v1_to_v2()`
- Validator now consumes `occurrences` as primary input (with fallback to `citations`)
- Repair pipeline receives v2 data through validation reports

**File Locations:**
- Primary: `output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v2.json`
- Compatibility: `output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v1.json`

**Consumer Updates:**
- `validation/review_validator.py` - now reads `occurrences` first, falls back to `citations`
- `services/repair_integration.py` - receives v2 data via validation reports
- `main.py` - produces v2 as primary, v1 as projection

