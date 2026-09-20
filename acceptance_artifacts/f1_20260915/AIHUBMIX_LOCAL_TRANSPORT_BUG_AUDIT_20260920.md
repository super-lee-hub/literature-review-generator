# AihubMix 本地 Transport / Receipt Closure Bug Audit

日期：2026-09-20
审查对象：PR #24，当前分支 `codex/f1-validation-authority-closure`
审查范围：AihubMix 长响应没有回到本地运行时的原因；不重新发送 provider 请求。

## 结论摘要

高置信度结论：AihubMix 上游确实完成了 candidate 生成，但本地程序没有可靠地接收、保存并关闭该响应。当前主要问题不是 API key、余额或模型不可用，而是长响应经过本机环境代理时，`requests.post` 在收到完整 HTTP 响应前被 `RemoteDisconnected` 断开；本地随后只得到 `transient_network`，无法形成 provider receipt、response hash、Registry artifact 或 closure。

这解释了为什么 Dashboard 有成功和输出 token，但本地 Q 仍必须是 `NOT_VERIFIED`：计费/上游 output 不是本地可验证的响应正文。

## Ranked findings

| Rank | Finding | Confidence | Effect |
|---|---|---:|---|
| 1 | 长 LLM 响应使用非流式 `requests.post`，没有读取流式 chunk，也没有保留 AihubMix request ID。 | High | 长响应可在 provider 端完成，但本地连接先断；本地无法恢复原始 JSON。 |
| 2 | 网络失败后的本地重试没有 request-level 去重或上游响应回收保证。 | High | 同一 candidate 可能被重复计费；多条相同输入 token 的成功 Dashboard 记录与此一致。 |
| 3 | AihubMix recovery 是 opt-in，且当前实现只接受唯一时间匹配 task 和 JSON 内容；没有 task 或返回 SSE 时会 fail-closed。 | High | 账户已开启 async 仍可能没有可回收任务；不会误接其他请求，但也无法恢复当前响应。 |
| 4 | Runtime receipt closure 正确地拒绝缺少本地 provider response 的 candidate。 | High | 这是安全行为，不是把失败错误标成 PASS；但它使 provider-side success 无法转化为本地 acceptance。 |
| 5 | 环境代理本身可能有长连接/非流式响应限制；direct proxy 绕过测试又连接超时。 | Medium | 需要网络层或 provider gateway 进一步确认，仓库代码不能单独证明代理的具体 idle timeout。 |

## Direct code evidence

### 1. LLM transport is non-streaming

- `ai_interface.py:911-916` — environment proxy mode calls `requests.post`; direct mode creates a session with `trust_env=False`.
- `ai_interface.py:2003-2015` — the LLM request is sent with `json=final_payload` and then waits for `response.json()`; no `stream=True`, `iter_lines`, SSE accumulator, or first-byte/first-token timeout exists.
- `ai_interface.py:1919-1925` — Chat Completions route uses `/chat/completions` and a bearer header; no `X-Aihubmix-Request-Id` response-header capture exists.
- `runtime/orchestrator.py:914-928` — Outline calls the same uninstrumented transport with a large JSON prompt and configured output cap.

Contrast: the repository already has a bounded streaming implementation for MinerU JSON in `tests/test_pr24_final_fixes.py:363-393`; the analogous long-running LLM path does not use that pattern.

### 2. Failure is classified as transient and then retried

- `ai_interface.py:203-218` — connection/proxy/timeout exceptions become `transient_network`.
- `ai_interface.py:2198-2205` — transient failures retry while the per-call retry budget remains.
- `runtime/orchestrator.py:923-927` — Outline supplies `transport_retries` and node retry limits to the transport.
- `runtime/provider_runtime.py:2362-2390` — the aggregate reservation is completed into a receipt even when the transport result is failed; the failed candidate does not obtain a response hash.

This is fail-closed, but it is not idempotent against a third-party provider that continues processing after the client disconnects.

### 3. Recovery branch is safe but currently cannot recover this request

- `ai_interface.py:222-350` — recovery is opt-in, queries `/ai/v1/tasks`, requires exactly one recent matching model task, downloads `/content`, parses JSON, and rejects ambiguity or missing output.
- `ai_interface.py:1983-1993` and `ai_interface.py:2171-2172` — recovery is attempted only after a final transient failure.
- `tests/test_aihubmix_recovery.py:30-121` — tests cover successful unique recovery, ambiguity rejection, and opt-in behavior.

The implementation is deliberately fail-closed. It does not yet recover SSE task content, and it cannot recover a task that AihubMix did not expose through the API key.

## Acceptance evidence correlation

The following evidence is stronger than a generic billing screenshot because token counts and timing match local receipts:

- R16 relation local receipt: `68080 -> 8367`, HTTP 200, response hash present.
- User-provided Dashboard relation row: `68080 -> 8367`, `AWS/claude-fable-5-1`, success, Trace ID `2026091916070644792102688772764`.
- User-provided Dashboard candidate rows: `56835 -> 19096` and `56835 -> 21101`, `AWS/claude-opus-5`, success.
- Local R16 candidate node: `candidate_1_provider_generation` failed closed with no provider receipt and no response hash.

Evidence files:

- `F1_AIHUBMIX_R16_UPSTREAM_OUTPUT_CORRELATION_20260920.json`
- `F1_AIHUBMIX_R16_UPSTREAM_OUTPUT_CORRELATION_20260920_R2.json`
- `F1_AIHUBMIX_RECOVERY_PROBE_20260919.json`
- `F1_AIHUBMIX_OUTLINE_RECOVERY_RETRY_SUMMARY_20260919_R16.json`

## What is ruled out

- The API key is not universally invalid: three no-document route probes passed and relation calls returned HTTP 200 after recharge.
- The model is not universally unable to generate: provider Dashboard records successful Opus outputs with non-zero output tokens.
- The local runtime is not silently accepting an unverified candidate: the candidate node remains failed/unfinished and Q remains `NOT_VERIFIED`.
- The F1 original PDFs were not transported; the Outline retry reused the canonical Stage1 summaries and local parser boundary.

## What remains unknown

1. Whether the environment proxy closes the non-streaming connection at a fixed idle/read boundary, or whether AihubMix closes it after upstream completion.
2. Whether the Dashboard Trace ID can expose the raw response body to the account owner even when `/ai/v1/tasks` is empty.
3. Whether AihubMix can return a stable `X-Aihubmix-Request-Id` before a long response completes.
4. Whether a streaming Chat Completions request for this exact Claude route yields chunks early enough to keep the proxy connection alive.

## Questions for independent ChatGPT review

1. Does `requests.post(..., stream=False)` plus `response.json()` adequately support a 300+ second, 19k-token Chat Completions response through an environment proxy?
2. Should the transport use streaming JSON/SSE for long AihubMix calls and capture `X-Aihubmix-Request-Id` before parsing the body?
3. What idempotency or recovery design prevents duplicate billing when the provider continues after client disconnect?
4. Is the unique time-window task match sufficiently safe, or should recovery require an externally supplied Trace ID/request ID before accepting content?
5. How should the Registry represent “provider generated upstream but local response unavailable” without treating it as a successful candidate?

## Current disposition

`NOT_READY_TO_MERGE` / `Q NOT_VERIFIED`.

The local code has a real response-recovery gap and now contains an opt-in,
fail-closed recovery branch. The remaining acceptance blocker is the absence of
locally retrievable candidate response bytes and receipt closure, not evidence
that AihubMix produced no output.
