# Feature Reality Matrix

> Audience: maintainers / AI agents / contributors.
> This is an internal status document, not the primary end-user guide.

## Overview
This matrix provides a comprehensive view of the current implementation status of features in the auto-generate project. It helps track what's implemented, what's partially implemented, what's legacy, and what's planned for future development.

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
| Citation Object Main Chain | implemented | Citation object as primary truth source | Fully functional - structured citations are now the default truth source |
| Validation/Repair | implemented | Validation and repair pipeline | Fully functional - includes preprocess evidence loader and semi-closed loop repair |
| GUI Queue System | implemented | Workflow-page serial background queue | Fully functional for GUI submissions with durable task snapshots and serial processing |
| AI-native Skill Entrypoint | implemented | Repo-local Codex / OMX skill entry surface | Fully functional additive surface via `.codex/skills/auto-generate-orchestrator` + `runtime/*` |
| Runtime Stage Trace | implemented | AI-native runtime source/trace artifacts | Fully functional - persists `source_bundle.json` and `runtime_stage_trace.json` into the job workspace |
| Outline Review Compatibility | partial | Optional outline critique/arbitration/adopt surface | Present as an explicit/manual compatibility path; normal outline/review workflow does not require it |
| Zotero Integration | implemented | Zotero report parsing and library integration | Fully functional |
| PDF Extraction | implemented | PDF text extraction with multiple backends | Fully functional |
| AI Integration | implemented | OpenAI-compatible API integration | Fully functional |
| GUI Interface | implemented | Local GUI for workflow management | Fully functional - includes embedded workflow-page queue operations |
| CLI Interface | implemented | Command-line interface | Fully functional |

## Feature Details

### JobWorkspace
- **Status**: implemented
- **Description**: Provides a structured workspace for each job, managing artifacts and dependencies
- **Notes**: Fully functional with support for artifact tracking and persistence

### ArtifactRegistry
- **Status**: implemented
- **Description**: Tracks artifact dependencies and relationships
- **Notes**: Fully functional with support for artifact registration and resolution

### Config Compatibility
- **Status**: implemented
- **Description**: Provides compatibility layer for configuration files
- **Notes**: Fully functional with support for different config versions

### Review Draft v2
- **Status**: implemented
- **Description**: Updated review draft structure with improved block management
- **Notes**: Fully functional with support for block-based review drafts

### Citation Manifest v3
- **Status**: implemented
- **Description**: Structured citation management with occurrence tracking
- **Notes**: Fully functional with v3 as the normal runtime truth; v2 may still appear as a compatibility artifact in some paths

### Stage1 Multimodal Input
- **Status**: implemented
- **Description**: Support for multimodal inputs in stage 1 analysis
- **Notes**: Fully functional with support for visual artifacts

### Citation Object Main Chain
- **Status**: implemented
- **Description**: Citation object as primary truth source
- **Notes**: Fully functional - structured citations are now the default truth source

### Validation/Repair
- **Status**: implemented
- **Description**: Validation and repair pipeline
- **Notes**: Fully functional - includes preprocess evidence loader and semi-closed loop repair

### GUI Queue System
- **Status**: implemented
- **Description**: Workflow-page serial background queue
- **Notes**: Fully functional for GUI submissions; CLI and AI-native runtime remain direct-run and out-of-queue

### AI-native Skill Entrypoint
- **Status**: implemented
- **Description**: Repo-local Codex / OMX skill entry surface that drives the same workspace/artifact substrate through `RuntimeJobSpec` and `AgentRuntimeBridge`
- **Notes**: Additive surface; not a replacement for CLI or GUI

### Runtime Stage Trace
- **Status**: implemented
- **Description**: AI-native runtime artifacts that record normalized input/source state and stage execution history
- **Notes**: Persists `source_bundle.json` and `runtime_stage_trace.json` in the active job workspace

### Outline Review Compatibility
- **Status**: partial
- **Description**: Optional outline critique/arbitration/adopt compatibility surface
- **Notes**: The normal runtime generates and consumes the markdown outline artifact directly. Explicit/manual outline review helpers still exist, but they are not part of the default outline → review chain.

### Zotero Integration
- **Status**: implemented
- **Description**: Zotero report parsing and library integration
- **Notes**: Fully functional with support for Zotero report parsing and PDF lookup

### PDF Extraction
- **Status**: implemented
- **Description**: PDF text extraction with multiple backends
- **Notes**: Fully functional with support for multiple extraction methods

### AI Integration
- **Status**: implemented
- **Description**: OpenAI-compatible API integration
- **Notes**: Fully functional with support for multiple API providers

### GUI Interface
- **Status**: implemented
- **Description**: Local GUI for workflow management
- **Notes**: Fully functional - includes embedded workflow-page queue operations

### CLI Interface
- **Status**: implemented
- **Description**: Command-line interface
- **Notes**: Fully functional with support for all core commands

## Roadmap Notes

This section records implementation direction/history. It should not be read as a promise that every item is the default end-user workflow today.

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
- Treat any remaining outline-review helpers as optional compatibility code until explicitly removed

### P5: Documentation and GUI Updates
- Update GUI validation entry and configuration text
- Clean up test temporary artifacts
- Generate new truth source documentation
