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
python -m portfolio_agent.cli serve      # dashboard at http://127.0.0.1:8765 (each tab has its own address: /dashboard, /inbox, /accounts, /debug)
```

1. **Accounts & logins → Add login.** One login can hold several accounts; each account gets its own history.
   - Name, institution and login page URL (Merrill: `https://www.mymerrill.com`).
   - Allowed domains default to the login page's domain. Add any domain the portal sends you to, comma-separated (Merrill: `mymerrill.com, ml.com`).
   - Optional login steps: the holdings page URLs (one per line) that together show every account. Leave them blank and the fetcher detects the login fields and uses the local LLM to find the holdings page.
   - Tick **Refresh supported** for logins the app can sign in to. Only those get a **Refresh** button and are part of the Dashboard's Refresh; every other login shows **Upload statements**. Access methods API, Email statement and Manual appear greyed out until they're built.
2. **Save the username and password.** They're encrypted with a key in `~/.portfolio-agent/secret.key` (mode 600). Use the eye icon to view them.
3. **Sign in by hand once** so the portal remembers this device:
   `python -m portfolio_agent.cli open-browser <login id>`
   This uses the same browser profile the fetcher uses, stored in `data/private/browser/`.
   - **Sites where you sign in with Google** (e.g. Frec): set **Sign in with** to **Google** on the login, then do this step with Google. Google refuses sign-in from Playwright's testing build of Chrome, so these logins use your installed **Google Chrome** with a profile of their own (`data/private/browser/<institution>-chrome`); `open-browser` opens it as an ordinary Chrome window. Quit that window (Cmd+Q) when you're signed in. Later runs press the site's "Sign in with Google" button and pick your account in Google's chooser; the fetcher never types into Google's pages. When Google wants your password or a verification step again, the run stops and the Inbox asks you to repeat this step. Save the Google account's email as the login's username (leave the password blank) so the right account is picked in Google's chooser.
4. **Press Refresh.** The extractor reads every account on the page.
   - The accounts must add up to the page's total for all accounts; if one is missing, the reading is rejected.
   - Each new account appears under its login as **pending**, and its first value is held in the **Inbox**. Check it with **View Source** (screenshot, page text, page HTML), then approve it.
   - Rename an account, change its type, or set it to **ignored** under Accounts & logins. A page that still lists an ignored account isn't held for missing it, and a card or loan read with only its balance is stored as that balance.
   - If the portal asks for a verification code, the inbox asks you for it.
   - If the portal shows a **CAPTCHA**, the fetcher's window comes to the front and the Inbox says so. Solve it there; the run carries on by itself (it waits up to 5 minutes, `PA_CAPTCHA_WAIT`). The fetcher never answers a CAPTCHA itself.
   - If a page is rejected because it shows a number that isn't an account (e.g. a member number), press **Accept accounts read** on the Inbox item. The number is saved under the login's **Numbers that aren't accounts**, and the accounts read are checked again and held for your approval.
5. **Nightly run:** `python -m portfolio_agent.cli nightly`. Schedule it with launchd. Each login is limited to 2 logins per night.
6. **Institutions without daily access: upload statements.**
   - Add a login with access method **Upload statements** and cadence **Monthly**. It needs no URL or password; leave Refresh supported unticked, and Refresh and the nightly run skip it.
   - Each month, press **Upload** on the login and pick the PDFs: one per account, or one consolidated statement.
   - Each file is read like a portal page. Its accounts are checked and stored, or held in the Inbox. A statement is dated by the closing date of its period.
   - Uploading the same statement again is safe: it's ignored once it has been read correctly, and read again if it wasn't.
   - **Scanned PDFs** (pages that are images, with no text to select) are read with macOS's built-in text recognition, on this Mac only. A small helper (`portfolio_agent/ocr/pdf_ocr.swift`) is built the first time, which takes about a minute; after that a page takes about a second.
   - A monthly account turns **stale** 45 days after its last statement.
   - Rockefeller, Schwab and Frec statements have built-in layout notes (`statement_guides.py`). Frec: only the first page is read, and the account's value is the closing balance of TOTAL PRICED PORTFOLIO (no individual positions). For another institution, add notes to the login's playbook.
   - Planned (spec Stage 13): these statements are picked up from email automatically, and upload becomes a fallback.
7. **Private funds: upload capital account statements.**
   - Add a login with access method **Upload statements**, statement type **Capital account statements** and cadence **Quarterly**.
   - Each fund in a statement becomes an account of type *private fund*, valued at its **ending capital balance** for the period (one holding). It's dated by the end of the period, not the issue date.
   - Commitments, contributions, distributions and fees aren't stored yet (the roll-forward, spec §5.2, comes later).
   - A quarterly account turns **stale** 120 days after its last statement (`PA_STALE_DAYS_QUARTERLY`).
   - **A sign-in form that emails a code on request** (e.g. Tattva's "Generate New OTP"): put that button's label under login steps → *Button that sends the code*. On Refresh the fetcher fills the username and password, presses it, reads the code from Gmail (set up under Accounts & logins → *Verification codes by email*; if the email comes from another domain than the portal, put it in *Emailed codes come from*) or else asks you in the Inbox, then signs in and waits up to a minute for the portal's next page. With a documents tab such as **NAV**, it downloads the newest month's statement. The documents tab can also be the page's own address (e.g. `https://portal.example.com/statements`), which is opened directly after sign-in; it must be on the login's own site.
   - **From a fund portal instead:** set access method **Browser** with statement type **Capital account statements**. On Refresh the fetcher signs in and reads the portal's summary page: each fund row becomes an account with its own as-of date. Strategic Partners has built-in notes: **Unrealized Value**, dated by the **As of Date** column (the last one) of **Performance Summary (USD)**; code reads each fund's date from its row, not the model.
   - **Dashboards:** tick **Read the page shown after sign-in** when the page you land on lists every account (Frec: each account's total value). The fetcher then reads that page and clicks nothing.
   - **Slow portals:** set **Wait after sign-in (seconds)** on the login (Star Mountain: 30). The fetcher waits that long before reading; Cancel still works during the wait.
   - **Or from each fund's own Documents tab** (e.g. Star Mountain): set **file name contains** (e.g. `Capital Statement`) under login steps. The fetcher opens each fund from the dashboard (the names under a column you set, e.g. **Fund Name**; else links containing text you set; else the local model picks them by name), opens its Documents tab, waits for the list, and downloads that fund's latest matching file. Star Mountain has built-in notes: **Ending Balance** for the period, dated by the date at the top.
   - **Or from its Documents page:** set the login's documents tab (login steps). The fetcher then opens that tab, filters for **Capital Account Statements** (or, with no such filter, types it into the page's search box; both labels can be changed under login steps), and downloads only the **latest** statements: those dated in the newest quarter listed, one per fund. They come only from the portal's own domains. Statements already read are skipped.

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
