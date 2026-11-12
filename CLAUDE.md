# Portfolio agent

A personal portfolio tracker that runs only on this Mac. Playwright signs in to bank and brokerage portals. A local Qwen model (no cloud LLM) reads the captured pages. Values go into an append-only SQLite store, and a local FastAPI dashboard shows them. `docs/spec.md` is the spec to build against, and `docs/design/` holds the earlier design checkpoints.

## Privacy and security rules (always)

- **Never read** `data/private/**`, `.env`, `.env.*` or any `*.db` file. They hold credentials, captured portal pages and real balances. Inspect data through the dashboard API instead, which returns only what the UI shows.
- **Never put real data in code, tests, docs or commits.** That covers account numbers (including masked last-4 digits), balances, holdings, and the names of family members or accounts. Tests use made-up values such as "Alex", "Kid - 529" and "…1234". Check `git diff` for these before every commit.
- **Never log, trace or send to the model** passwords, usernames or verification codes. Traces are shown in the Debug tab.
- **Nothing leaves the machine.** No outside services, telemetry or cloud APIs for portfolio data.
- **The owner signs in to portals.** Don't start a Refresh on their behalf without asking; it signs in to their bank.

## Working with the owner

- **Ask before committing or pushing.** Work happens on feature branches, not `main`.
- **Apply settings to their real login, not just the form.** When a request is about their own accounts (for example "ignore Investments on Bank of America"), build the feature, then set it on their live login through the API, check that it took effect on existing accounts, and say what was set.
- **Explain changes in plain terms:** what they'll see and what they need to do. Name any limitation that wasn't verified against the real portal.

## Commands

```bash
.venv/bin/python -m pytest tests -q                 # all tests; run before every commit
.venv/bin/python -m portfolio_agent.cli serve       # dashboard on http://127.0.0.1:8765
.venv/bin/python -m portfolio_agent.cli --db /path/demo.db serve   # demo data; set PA_PORT for another port
```

- **API:** every request other than GET needs the header `X-PA-Client: 1`. For example, `PUT /api/logins/{id}` saves a login's rules and `PUT /api/accounts/{id}` saves an account.
- **The owner runs `serve` in their own terminal.** After backend changes, ask them to restart it. Front-end files (`web/static/`) only need a browser reload.
- **`node` is not installed.** Check JavaScript syntax with `osascript -l JavaScript`, or by loading the page in the browser pane and reading the console.
- **Demos:** for UI checks, build a throwaway database with made-up accounts in the scratchpad and preview it on another port (e.g. 8798/8799). Remove any temporary `.claude/launch.json` entry afterwards.

## Design rules that are easy to break

- **The store is append-only and bitemporal.** Snapshots are never rewritten. Each value has an `as_of_datetime` and a `retrieved_at`, and corrections are new rows.
- **Stored values are what the portal shows.** Two adjustments are applied only when values are read, in `store.py` (`_Values`):
  - the debt sign: accounts with `is_liability` count as negative;
  - the part-owned share: accounts held by an entity in `owners` count at the percentage in `owner_shares` effective on that date.

  View Source, the validator (`previous_snapshot` / `_to_snapshot`) and the holdings list use full values. Never fold either adjustment into stored rows.
- **Per-login rules** (`skip_categories`, `liability_categories`, `skip_other_institutions`, `open_account_details`) also apply to existing accounts, through `registry.apply_login_rules`.
- **Code reads account numbers, not the model.** They come from each account's details page (`browser/portal.py`) and are matched by link name. They override the model's guess, and the validator rejects one number given to several accounts.
- **Parallel refreshes:** logins at the same institution share a browser profile, so they never run at the same time (`store.busy_login_ids`). The model is used by one run at a time (`llm.py`).
