# Agent operating contract

Read `README.md`, `context.md`, `PROJECT_STATE.md`, and `docs/DEPLOYMENT.md` before changing or deploying this repository.

Non-negotiable rules:

- This repository supports research, backtest, shadow, and paper modes only. Do not add or emulate live-money execution without a new, explicit user decision and security review.
- `LIVE_TRADING_ENABLED` must remain `false`. Production starts with new exposure paused.
- The LLM is never a risk authority and never receives broker credentials.
- Risk rules, restricted securities, sizing, idempotency, and kill switches remain deterministic and independently tested.
- Never commit `.env`, `.env.production`, credentials, account numbers, raw personal data, or market datasets.
- Preserve point-in-time timestamps and append-only decision lineage.
- Never deploy a dirty worktree. Run `make check`, `make doctor`, and the secret scan first.
- Do not claim that a healthy Phase 0 scaffold is a validated trading strategy.
- Maintain `context.md` as the durable project memory. Before every commit, append its stable commit ID, exact subject, scope, validation, decisions, and expected post-commit global state. Also refresh the current architecture/state sections whenever they change.

Safe setup:

```sh
make bootstrap
make check
make doctor
```

Cloud deployment is allowed only after every prerequisite and safety gate in `docs/DEPLOYMENT.md` is satisfied.
