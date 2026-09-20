# Runtime credential provenance

Provider credentials are resolved without mutating the process environment.
The explicit order is:

1. `LLM_*` in the process environment;
2. `.env` beside the selected `config.ini`;
3. `api_key` in `config.ini`.

Two meaningful values from different sources must be equal. If they differ,
configuration loading fails before any provider transport is constructed. The
diagnostic records only source presence, the selected source, equality flags,
provider family, model, endpoint, proxy policy, route, request hashes, and
size/time limits. It never logs a credential or persists a credential hash.

`loaded_from_.env_file` and `YOUR_*_API_KEY_HERE` values are template
sentinels. Example configuration validation may display them, but a required
role in a production runtime rejects them before HTTP.

Provider admission is derived from the durable StagePlan. For example,
`analyze` with `Stage1_Input.primary_reader_only=true` admits only
`Primary_Reader_API`; it does not require an unused Backup Reader or Writer
credential.
