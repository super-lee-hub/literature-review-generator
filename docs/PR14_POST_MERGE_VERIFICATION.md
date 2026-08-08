# PR #14 Post-Merge Verification

Date: 2026-08-08 (Asia/Shanghai)
Repository: `super-lee-hub/literature-review-generator`

This file separates pre-merge PR evidence, actual post-merge main evidence,
post-merge cleanup evidence, and work that was not run. It does not promote
deterministic provider injection or local-only results to live or remote
verification.

## Pre-merge PR evidence

- PR #14: `feat: upgrade review workflow with Outline Intelligence v3`
  (`https://github.com/super-lee-hub/literature-review-generator/pull/14`)
- PR state: `MERGED`, squash merge
- Squash merge SHA: `2f89c6bce06f282eb91799af329f21425b4eac45`
- Source branch: `codex/platform-hardening-outline-v3` at
  `ec77fd988f4474ff3f639ba70eb8c071214abfbf`
- Source tree and squash-merged main tree:
  `702ef840fcce5b9f935b562be268e77b76d85ef3`
- Final PR Windows workflow run `31200485526` (pull_request merge ref):
  checkout SHA `9324605dd96a129a6435a89a1245931bf3f1760b` (merge commit),
  strict offline result `843 passed, 22 deselected in 744.99s`,
  Pyright `0 errors, 0 warnings, 0 informations`
- Historical local pre-merge evidence is preserved in
  `docs/PR14_FINAL_GAP_AUDIT.md`, labeled as a historical pre-merge
  verification snapshot.

> The ZIP files are absent from the committed diff and remote branch. The local
> operator reports that they were not read or staged; remote GitHub evidence
> cannot independently verify local read access.

## Actual post-merge main evidence

- `main`: `2f89c6bce06f282eb91799af329f21425b4eac45`
- Main tree: `702ef840fcce5b9f935b562be268e77b76d85ef3`
- Old main before PR #14: `ecac15976ebb3b6ee754fe5c0dfe44efacd72e9a`
- Main push CI run `31234443464` (`event=push`, head SHA
  `2f89c6bce06f282eb91799af329f21425b4eac45`):
  - checkout SHA: `2f89c6bce06f282eb91799af329f21425b4eac45`
  - strict offline result: `843 passed, 22 deselected in 824.76s`
  - Pyright: `0 errors, 0 warnings, 0 informations`
  - conclusion: `success`, job `93044300435`

## Post-merge cleanup evidence

- Branch: `codex/post-pr14-contract-cleanup`
- Branch commit: `d6d9838d65ce3cd2afa252a8fe9e6b0fdac6584b`
- Cleanup scope:
  - `strategy_policy` is a legacy-compatible read-only field, stripped by
    mutable config normalization, and excluded from the Stage 1 reuse
    fingerprint
  - typed manifest post-import deletion regression fails closed with
    `typed_manifest_unreadable:`
  - bbox-only visual identity regression changes only one contract-relevant
    bbox and invalidates exact reuse
  - page-range documentation uses "page number and bounding box, plus page
    range when present"
  - historical PR14 documents are labeled as pre-merge snapshots
  - stale push workflow trigger removed

Local cleanup gates and the cleanup PR's GitHub Actions run are appended below
after they are executed.

### Cleanup local gates

- `python -m compileall -q .`: passed
- `python -m pytest --collect-only -q`: `868` tests collected
- `python -m pyright`: `0 errors, 0 warnings, 0 informations`
- Targeted Phase 1 regressions: `92 passed in 810.56s`
- Full strict offline suite:
  `846 passed, 22 deselected in 1679.94s`
- `python -m reviewctl doctor --config config.ini.example`: `ok=true`,
  exit `0`, zero provider network calls; `status=warn` only reports the
  repository's pre-existing stale locks
- `git diff --check`: passed

The 843-test pre-merge result is not presented as the cleanup result. The
cleanup branch reports its own `846 passed` aggregate above.

## Not run

- Live API/provider verification
- Playwright
- Heavy OCR
- Multi-host publication/fencing
- Cross-host single-flight
- Cryptographic provenance claims
- Live verification from deterministic provider injection
