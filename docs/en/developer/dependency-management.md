# Dependency Management

`requirements.txt` and `requirements-dev.txt` are source requirement files.
They are intentionally readable and are not the release installation contract.

The release contract for the supported Hosted Windows runtime is:

- `requirements-py311-windows-prod.lock`: production runtime, Python 3.11,
  Windows, pinned versions and PyPI distribution hashes.
- `requirements-py311-windows-dev.lock`: production runtime plus test,
  type-check, lint, browser, and dependency-audit tools, with the same hash
  and platform constraints.

Regenerate both locks with `uv pip compile` for the matching Python/platform
targets and review the complete diff. Release installation must use
`python -m pip install --require-hashes -r requirements-py311-windows-prod.lock`.
CI uses the development lock and runs `pip check`, Pyright, fatal Ruff rules,
and `pip-audit` against the production lock.

The legacy `requirements-py311-windows.lock` remains for historical checkout
compatibility only. New release and CI changes must use the explicitly named
production or development lock above.

Local Chroma RAG is optional and is intentionally excluded from both release
locks. Its source requirements are isolated in
`requirements-optional-rag.txt`; the current ChromaDB advisory set has no
published fix version in the audit feed, so enabling it requires a separate
owner-reviewed security decision.
