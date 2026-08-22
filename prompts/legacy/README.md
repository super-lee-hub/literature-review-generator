# Legacy prompts

The production runtime does not load prompts from this directory. Historical
prompt files are intentionally not retained here; Git history is the backup.
All production prompt authority is recorded in [`../registry.json`](../registry.json)
and loaded from `prompts/active/` through `services.prompt_registry.PromptRegistry`.
