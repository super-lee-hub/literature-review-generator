# 当前功能矩阵

本矩阵描述当前 main 的公共能力，不写入会随时间漂移的 pytest 精确数量；精确数量
应记录在 PR 或 release verification evidence 中。Windows GitHub Actions 当前负责
strict-offline tests、Pyright、compile、collection、public CLI smoke、doctor 和
committed-range whitespace gates。

| 能力 | 当前状态 | 边界 |
| --- | --- | --- |
| Outline Intelligence v3 | `E2E_VERIFIED` | 唯一当前生产 Outline 路径 |
| Free Mode typed intent | `E2E_VERIFIED` | `free_mode_intent_input/v1` |
| Free Mode -> ReviewIntent | `E2E_VERIFIED` | literal idea projection 与 writer binding |
| Free Mode Writer replay binding | `E2E_VERIFIED` | replay 时保持 context 与 identity 绑定 |
| Stage 1 independence from Free Mode | `E2E_VERIFIED` | Stage 1 使用独立的当前契约 |
| Concept Mode | `DISABLED` | 过时请求会被拒绝 |
| Validation adjudication single-flight | `E2E_VERIFIED` | 仅声明 single-host 范围 |
| Registry-backed durable adjudication reuse | `E2E_VERIFIED` | provisional 与 durable closure-bound authority |
| `reviewctl` control plane | `E2E_VERIFIED` | `RuntimeJobSpec` -> `AgentRuntimeRunner` |
| Stage 3 review contract | `E2E_VERIFIED` | `review_draft` v3、`citation_manifest` v3、DOCX |
| Queue fencing/publication | `E2E_VERIFIED` | 当前 lease 与 Registry boundary |
| JobOutcome / CurrentArtifactSet | `E2E_VERIFIED` | Registry-backed canonical authority |
| Repair/promotion 与 export admission | `E2E_VERIFIED` | 当前 transaction 与 dependency gates |

## 证据限制

以下范围不由 strict-offline evidence 声明：

- live API/provider verification — `NOT VERIFIED`
- Playwright — `NOT VERIFIED`
- heavy OCR — `NOT VERIFIED`
- multi-host single-flight — `NOT VERIFIED`
- multi-host publication/fencing — `NOT VERIFIED`
- cryptographic provenance verification — `NOT CLAIMED`

`reviewctl doctor` 可能报告的 stale-lock warning 是 non-blocking diagnostic。本矩阵
不授权自动删除 lock。
