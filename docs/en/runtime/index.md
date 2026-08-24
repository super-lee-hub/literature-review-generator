# Runtime Truth Documentation

> Audience: Maintainers and AI agents.

## Documents

| Document | Content |
|----------|---------|
| [truth-sources.md](./truth-sources.md) | Stage-specific canonical artifacts, data contracts, current main pipeline |
| [compatibility.md](./compatibility.md) | Compatibility paths, deprecated APIs, removal timeline |
| [workspace-layout.md](./workspace-layout.md) | Job workspace structure, output directories, artifact registry, hard constraints |
| [stage1-vision.md](./stage1-vision.md) | Stage 1 MinerU text, full-page visual coverage, batching, and fallback |

## Content Scope

**What belongs here:**
- Canonical artifact definitions for each pipeline stage
- Field-level and data-contract-level compatibility commitments
- Deprecated paths and planned removal schedule
- Workspace, cache, and output directory layout

**What does NOT belong here:**
- Architecture design rationale → see [../developer/index.md](../developer/index.md)
- AI handoff context → see [../ai/index.md](../ai/index.md)
- Feature matrix → see [../reference/index.md](../reference/index.md)
