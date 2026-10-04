## Secrets
- Never read `.env` files or print environment variables. API keys are managed by `envh`.
- To run anything that needs a key, follow the `envh` skill: `envh list`, then `envh run --preset <preset> --reason "..." -- <command>` for one command, or `envh session start <preset> --minutes N --reason "..."` first when several commands need keys.
- If a request is denied or no preset fits, stop and tell me what you needed and why. Never run `sudo`.
