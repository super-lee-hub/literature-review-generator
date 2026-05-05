# Feature Implementation Status Matrix

> Audience: maintainers / AI agents / contributors.
> This is an internal status document, not the primary end-user guide.
> Source: FEATURE_MATRIX.md (full migration).

## Legend
- `implemented`: Feature is fully implemented and functional
- `partial`: Feature is partially implemented but not fully functional
- `legacy`: Feature is implemented but will be deprecated in future versions
- `planned`: Feature is planned but not yet implemented

## Core Features

| Feature | Status | Description | Notes |
|---------|--------|-------------|-------|
| JobWorkspace | implemented | Job workspace management with artifact tracking | Fully functional |
| ArtifactRegistry | implemented | Artifact registry for tracking dependencies | Fully functional |
| Config Compatibility | implemented | Configuration compatibility layer | Fully functional |
| Review Draft v2 | implemented | Updated review draft structure | Fully functional |
| Citation Manifest v2 | implemented | Structured citation management | Fully functional |
| Stage1 Multimodal Input | implemented | Support for multimodal inputs in stage 1 | Fully functional |
| Citation Object Main Chain | implemented | Citation object as primary truth source | Fully functional |
| Validation/Repair | implemented | Validation and repair pipeline | Fully functional |
| GUI Queue System | implemented | Workflow-page serial background queue | Fully functional |
| AI-native Skill Entrypoint | implemented | Repo-local Codex / OMX skill entry surface | Fully functional |
| Runtime Stage Trace | implemented | AI-native runtime source/trace artifacts | Fully functional |
| Outline Review Compatibility | partial | Optional outline critique/arbitration/adopt surface | Explicit/manual compatibility path |
| Zotero Integration | implemented | Zotero report parsing and library integration | Fully functional |
| PDF Extraction | implemented | PDF text extraction with multiple backends | Fully functional |
| AI Integration | implemented | OpenAI-compatible API integration | Fully functional |
| GUI Interface | implemented | Local GUI for workflow management | Fully functional |
| CLI Interface | implemented | Command-line interface | Fully functional |

## Roadmap

### P0: Stability and Truth Alignment
- Fix Windows pymupdf4llm/onnxruntime access violation
- Unify --zotero-report and --library-path direct execution chain
- Create feature reality matrix and update documentation

### P1: Citation Object Main Chain
- Make citation object the default truth source
- Extend review_draft_v2 block structure
- Update DOCX v2 path to use manifest bibliography

### P2: Validation and Repair
- Update ReviewValidator input structure
- Modify SummaryRechecker to be canonical-only
- Implement repair root cause classification

### P3: Queue Productization
- Extend QueueJobSpec/QueueJobRuntime
- Add embedded workflow-page queue operations to GUI
- Remove public CLI queue commands and keep CLI direct-run

### P4: Outline review simplification
- Keep markdown outline generation as the normal path
- Avoid claiming critique/arbitration/adopt is part of the default workflow
- Treat any remaining outline-review helpers as optional compatibility code

### P5: Documentation and GUI Updates
- Update GUI validation entry and configuration text
- Clean up test temporary artifacts
- Generate new truth source documentation
