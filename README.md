# Personal Portfolio Agent: starter repo

CMU agentic AI capstone. Runs locally on a Mac with Hugging Face models,
LangChain and LangGraph. Your financial data never leaves the machine.

This starter covers the parts you can build and test **before** any browser
automation: the portfolio store, the validator, statement extraction, and the
ReAct Q&A agent. Portal fetchers come later and plug into the same pieces.

## Setup (VS Code on macOS)

```bash
cd portfolio-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # browser for the portal fetcher
pytest -q                     # 95 tests, fake model and fake pages, no download needed
code .
```

## Stage 1: one portal end to end

See `docs/spec.md` §2 for the stages.

```bash
python -m portfolio_agent.cli serve      # dashboard at http://127.0.0.1:8765
```

1. **Accounts & logins → Add login.** One login can hold several accounts; each account gets its own history.
   - Name, institution and login page URL (Merrill: `https://www.mymerrill.com`).
   - Allowed domains default to the login page's domain. Add any domain the portal sends you to, comma-separated (Merrill: `mymerrill.com, ml.com`).
   - Optional login steps: field selectors, and the holdings page URLs (one per line) that together show every account. Leave them blank and the fetcher detects the login fields and uses the local LLM to find the holdings page.
2. **Save the username and password.** They're encrypted with a key in `~/.portfolio-agent/secret.key` (mode 600). Use the eye icon to view them.
3. **Sign in by hand once** so the portal remembers this device:
   `python -m portfolio_agent.cli open-browser <login id>`
   This uses the same browser profile the fetcher uses, stored in `data/private/browser/`.
4. **Press Refresh.** The extractor reads every account on the page.
   - The accounts must add up to the page's total for all accounts; if one is missing, the reading is rejected.
   - Each new account appears under its login as **pending**, and its first value is held in the **Inbox**. Check it with **View Source** (screenshot, page text, page HTML), then approve it.
   - Rename an account, change its type, or set it to **ignored** under Accounts & logins.
   - If the portal asks for a verification code, the inbox asks you for it.
5. **Nightly run:** `python -m portfolio_agent.cli nightly`. Schedule it with launchd. Each login is limited to 2 logins per night.
6. **Institutions without daily access: upload statements.**
   - Add a login with access method **Upload statements** and cadence **Monthly**. It needs no URL or password; Refresh and the nightly run skip it.
   - Each month, press **Upload** on the login and pick the PDFs: one per account, or one consolidated statement.
   - Each file is read like a portal page. Its accounts are checked and stored, or held in the Inbox. A statement is dated by the closing date of its period.
   - Uploading the same statement again is safe: it's ignored once it has been read correctly, and read again if it wasn't.
   - A monthly account turns **stale** 45 days after its last statement.
   - Rockefeller and Schwab statements have built-in layout notes (`statement_guides.py`). For another institution, add notes to the login's playbook.
   - Planned (spec Stage 13): these statements are picked up from email automatically, and upload becomes a fallback.

**Test data:** the fictional samples in `data/samples/` stay for testing. Statements ingested with `cli ingest` aren't tied to any account, so the dashboard never shows them. To keep them out of your real database entirely, use `--db /tmp/test.db`.

**Kill switch:** while `data/private/STOP` exists, no fetcher runs.

**Evidence:** saved under `data/private/evidence/`, which is gitignored.

In VS Code: Cmd+Shift+P → "Python: Select Interpreter" → `.venv`. Install the
Python and Jupyter extensions.

## Choosing the model

Set `PA_MODEL_ID` (default `Qwen/Qwen2.5-14B-Instruct`). With 128GB of memory:

| Model | Memory (bf16) | Use |
|---|---|---|
| `Qwen/Qwen2.5-7B-Instruct` | ~15GB | fastest |
| `Qwen/Qwen2.5-14B-Instruct` | ~30GB | default; good balance |
| `Qwen/Qwen2.5-32B-Instruct` | ~65GB | most reliable JSON / ReAct format, slower |

```bash
export PA_MODEL_ID=Qwen/Qwen2.5-32B-Instruct
```

The first run downloads the model to `~/.cache/huggingface`. Gated models such
as Llama need access approved on their Hugging Face page and `hf auth login`.
Generation runs on the Mac GPU (`mps`) through `transformers`; it's slower than
a server like Ollama, so expect seconds to tens of seconds per step on 32B.

## Try it

```bash
# Ingest: extract -> validate -> store, or retry once -> review queue
python -m portfolio_agent.cli ingest data/samples/brokerage_2026-09-30.txt
python -m portfolio_agent.cli ingest data/samples/northwind_fund_2026-Q2.txt
python -m portfolio_agent.cli ingest data/samples/bad_statement_mismatch.txt   # -> REVIEW

python -m portfolio_agent.cli positions
python -m portfolio_agent.cli review

# ReAct Q&A agent; --trace prints every Thought / Action / Observation
python -m portfolio_agent.cli ask "What is my net worth and how current is each number?" --trace

# Procedural memory: notes the extractor reads before parsing that institution
python -m portfolio_agent.cli playbook "Example Brokerage" "Total is under ACCOUNT SUMMARY; cash sweep has no ticker."
python -m portfolio_agent.cli ingest data/samples/brokerage_2026-09-30.txt --institution "Example Brokerage"
```

All sample data is fictional. The database is `data/portfolio.db` (override
with `PA_DB_PATH`); delete it to start over.

## How the code maps to the design

| Design element | Where |
|---|---|
| ReAct loop (thought → action → observation), step budget | `react_graph.py` |
| Short-term memory: the run's thoughts/actions/observations | `ReactState.messages`, `trace` |
| Long-term factual memory, bitemporal (`as_of_date` + `retrieved_at`) | `store.py` |
| Long-term procedural memory (institution playbooks) | `store.get_playbook/save_playbook` |
| Validator in code: sums, qty×price, dates, unexplained moves | `validator.py` |
| Retry once with validator errors, then human review queue | `ingest_graph.py` |
| Read-only tools; no trade/transfer tool exists | `tools.py` |
| Statement text treated as untrusted data | `extractor.SYSTEM`, `documents.py` |
| LLM judgment task: reading heterogeneous statements | `extractor.py` |

Why JSON instead of native tool calling: local Hugging Face pipelines don't
support LangChain's `bind_tools` reliably, so the model writes each ReAct step
as JSON and the graph parses and routes it. This also makes the reasoning loop
explicit and easy to show in reports.

## Suggested next steps

1. Run ingest on one of your real statements (PDF works) and tune the extractor
   prompt or playbook until the validator accepts it.
2. Add a `fix` command that resolves a review item with your corrected values,
   and store the correction as text for retrieval (the RAG part of the design).
3. Replace the `quotes.json` stub in `tools._get_quote` with a real price source.
4. Move each tool group behind an MCP server; the agent code doesn't change.
5. Add the first portal fetcher for one institution, reusing the ReAct graph
   with browser tools and the same validator.
6. Schedule the nightly run (`launchd` on macOS).
