# Prompt Registry

Production prompts live under `prompts/active/` and are loaded only through
`services.prompt_registry.PromptRegistry`. `prompts/registry.json` records each
prompt owner, version, required placeholders, output contract, and SHA-256.

The `legacy/` directory is not a production fallback. Old prompt files are
removed when no current caller is reachable; Git history remains the backup.
