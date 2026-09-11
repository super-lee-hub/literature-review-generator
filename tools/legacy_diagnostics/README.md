# Legacy diagnostics

These scripts are retained for manual, non-authoritative troubleshooting only.
They are outside the pytest tree and do not participate in release acceptance.

Network requests are disabled by default. Use `--allow-network` only when an
operator explicitly authorizes a diagnostic request. The production
connectivity path is `python -m reviewctl micro-probe`, which owns route
admission, endpoint trust, ProviderRuntime budgets, and durable receipts.

The scripts must not be used to establish a release gate or to replace the
current RuntimeJobSpec / AgentRuntimeRunner control plane.
