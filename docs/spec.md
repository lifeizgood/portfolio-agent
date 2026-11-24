# Personal Portfolio Agent: Consolidated Spec

This spec brings together the design checkpoints in `docs/design/` and the owner's decisions of 2026-11-11 into one reference to build against. Every requirement is tagged with its source:

| Tag | Source |
|---|---|
| `[2.1]` | *2.1 Capstone Personal Portfolio Agent* (includes all of 1.1) |
| `[3.1]` | *Retrieval* |
| `[4.1]` | *Tree of Thought* |
| `[5.1]` | *Multi-Agent Architecture* |
| `[6.1]` | *Safety and Intervention Plan* |
| `[U]` | Owner decisions, 2026-11-11, in two rounds: build stages and product notes (§2), and rulings on contradictions and open decisions (§13). `[U C4]` means the ruling on item C4. |

Where a ruling (`[U]`) conflicts with a checkpoint doc, the ruling wins. If none of the sources covers a point, this spec doesn't fill the gap; the point is listed in §14 instead. Notes in *italics* compare a requirement with the current code in `portfolio_agent/`.

---

## 1. Purpose and scope

**The user.** One individual investor whose accounts span brokerage, wealth management, retirement and private or alternative funds. They're technical enough to run software locally and don't want to log into each portal by hand [2.1].

**The product.** A locally run, read-only multi-agent system [2.1, 5.1] that:

- builds one verified, bitemporal history of every holding, by account and source, refreshed each night;
- lets the user refresh accounts holding public securities on demand with a **Refresh** button [U];
- shows trends with charts and date selection from the first stage [U];
- later, discovers trends and explains them (Stage 11), then answers questions about them (Stage 12) [U].

**Hard constraints**

- **Read-only.** The agent never trades, transfers or changes account settings. Its job is to "see, record, verify and explain" [2.1, 5.1, 6.1].
- **Local.** The LLM, embeddings and store all run on the Mac [2.1, 3.1]. **No data extracted from brokerage or private-equity accounts leaves the Mac** [U C13].
- **Ticker lookups are allowed.** Ticker symbols may be sent out to look up market or research data. Actual holdings (quantities, values, account details) never are [U C14].
- **Code owns the numbers.** The LLM decides what to do next and interprets what it sees. Deterministic code does the arithmetic, handles credentials and writes to storage. A value becomes history only after code has checked it [2.1].
- **Every value is traceable.** Each number on the dashboard has a **View Source** button that shows the statement, screenshot or other evidence it was extracted from [U C5].
- **Real portals from the start.** The rollout drops the synthetic-data milestones and goes straight to real portals [U D21]. Synthetic data is kept for testing but never shown in the UI: the dashboard shows only accounts under a registered login [U]. Whether the capstone demo may show real data is open (C16).

**Two timestamps per value.** Every value records `as_of_datetime` (when it was true) and `retrieved_at_datetime` (when it was learned), both including the time of day. This keeps several pulls on the same day, such as a nightly run plus a Refresh, as separate records [U]. A Q2 statement received in October still belongs at June 30, not this week [2.1].

**Missing time of day.** If a source gives no time, the current time is used [U D16]. A dated source keeps its own date and takes only the time of day from the clock, so a June 30 statement stays at June 30.

### 1.1 Environment [2.1, U]

| Element | How the agent touches it | Access |
|---|---|---|
| Financial portals (brokerage, wealth mgmt, retirement, private fund) | Sandboxed browser with a persistent profile per institution | Read-only; domain allowlist |
| Institution APIs | Used when the user picks API as an account's access method [U D1]. Third-party aggregators are excluded, because extracted data would leave the Mac [U C13] | Read-only scopes |
| Email: statements | One labeled folder, filtered to registry senders. Holds capital-statement PDFs and the separate emails carrying their passwords [2.1, U D8]; from Stage 13 also monthly brokerage statements [U, 2026-11-12] | Read-only |
| Uploaded statements | Monthly PDF statements the user uploads in the dashboard, for institutions whose portal can't be read daily (Stage 1). Saved privately and read like any other evidence [U, 2026-11-12] | Read-only |
| Email: 2FA codes | Code-only reader; may search **any label**, but only to fetch a 2FA code for a login it just started [U C12] | Read-only; one message, short window |
| Text messages | Code-only reader of the Messages database for SMS 2FA; filters by time window [U D2]. Never exposed to the LLM | Read-only; one message, short window |
| Market data / research data | Looked up by ticker symbol only, never with quantities, values or account details [U C14]. Not used for validation [U C3] | Public, read-only |
| Credential store | Logins and PDF passwords, encrypted at rest in the local database, not in macOS Keychain. Entered, revealed (eye icon) and updated through the UI [U C9, D8, D18]. The tool layer fills login fields directly | Local only |
| Local database | Snapshots, roll-forward data, documents, evidence, playbooks. Plain storage is acceptable for data; credentials are encrypted [U C9] | Read/write by tools only |
| The user | Enters registry details and credentials, approves 2FA, reviews flagged values and proposals, answers pending questions | Human in the loop |

### 1.2 Dashboard [2.1, U]

The dashboard is a local web app served only on localhost: a small **FastAPI backend with a web front end** [U D15]. Every number on it is computed in code; the LLM writes only explanations and, from Stage 12, Q&A answers.

**Data shown**

- By default the dashboard shows the data from last night's run [U].
- A **Refresh** button re-pulls the accounts that hold public securities. The new values are stored with their own timestamps, and the dashboard updates [U].
- A **View Source** button sits next to every displayed value and opens the evidence behind it [U C5].

**Charts and periods (from Stage 1)** [U, 2.1]

- **Period selector.** Presets 1M / 3M / YTD / 1Y / 3Y / All, or a custom date range.
- **Charts:**
  - trend lines;
  - net worth over time, as the total line only (the default) or split by account, institution or type. The chart's settings are remembered in the browser [U, 2026-11-12];
  - a "what changed" bridge: market moves, late or restated private values, cash flows, and unexplained moves;
  - returns shown both ways: **IRR** (money-weighted) and **time-weighted** [U D17].
- **Daily dating rule.** Values are placed by `as_of_datetime`. For each day, each account uses its latest accepted value at or before the end of that day. A value carried forward is labeled with its as-of date [2.1].
- **Per-holding dates** [U]. An account's value on a day is the sum of its holdings, and each holding is placed at its own as-of date when the portal shows one. Example: a private fund "Last Updated 06/30/2026", read in November, counts from June 30.
  - A holding's value is carried forward until a newer value for it appears, a later reading no longer lists it (sold), or the account closes.
  - Older values are never overwritten. The June 30 value stays at June 30 after a September 30 value arrives.
- **Monthly trend rule.** A month uses each account's value on the latest day in that month that has data. Missing days are fine: a skipped or failed nightly run leaves a gap rather than an error [U].
- **"As known on" view.** Shows the history using only values retrieved by a chosen past date [2.1].

**Settings and inbox**

- **Accounts.** Each account shows status OK / stale / needs review, and has a detail view with evidence [2.1].
- **Registry and credentials.** The user adds and edits accounts here, including the login mechanism (Stage 1) and the access method. Logins and PDF passwords are entered and updated here and stored encrypted. A stored password is hidden by default and shown with an eye icon [U D1, C9, D8, D18].
- **Inbox.** One place for:
  - items held for review [6.1];
  - questions the system needs answered, e.g. after an unexpected failure, or when a PDF password is missing [U D8, D9];
  - 2FA code requests, saying why the code couldn't be read automatically [U C12];
  - playbook changes proposed that day, for approval [U D14];
  - suggested registry changes [2.1, 6.1].
- **Recommendations.** Suggested settings learned from use, starting with a Q&A step budget once enough runs exist. Each can be accepted, rejected or revised (§5.11) [U C7, D20].
- **Manual inputs** for specific accounts, from Stage 10 [U].

---

## 2. Build stages [U]

Each stage delivers the whole path end to end:

1. fetch;
2. extract;
3. validate;
4. store;
5. show on the dashboard, with View Source.

Trend lines, charting and date selection are present from Stage 1.

| Stage | Scope |
|---|---|
| 1 | **One financial portal end to end.** Includes entering the login mechanism through the UI, and pulling data on demand with Refresh. |
| 2 | Two financial portals end to end |
| 3 | All financial portals end to end |
| 4 | One institutional account with **browser login** → Capital Account Statement |
| 5 | A second institutional account with browser login |
| 6 | All institutional accounts with browser login |
| 7 | One institutional account whose Capital Account Statement arrives **by email** |
| 8 | A second email-statement institutional account |
| 9 | All email-statement institutional accounts |
| 10 | Manual inputs for specific accounts |
| 11 | Auto-discover trends, with explanations |
| 12 | Q&A about trends |
| 13 | **Brokerage statements by email.** Monthly statements for accounts without daily access (e.g. Rockefeller, Schwab) are picked up from the labeled mail folder and read automatically, through the same reader as uploaded statements. Manual upload then remains only as a fallback, e.g. for a statement that never arrived by email [U, 2026-11-12] |

**Where each component first appears:**

- **Stage 1:** portal fetcher, extractor, validator, store, registry/credential UI, dashboard, nightly run and Refresh. Added during Stage 1: **statement upload** for logins that can't be read daily, with per-institution statement guides (Rockefeller, Schwab, Frec) [U, 2026-11-12].
- **Stage 4:** capital-statement extraction, roll-forward schema and the roll-forward check (V7).
- **Stage 7:** statement fetcher and the password email.
- **Stage 11:** retrieval of investor letters and the trend explanations.
- **Stage 12:** Q&A, with the research-feed decision revisited [U D12].
- **Stage 13:** the statement fetcher extended to brokerage statements; it hands each PDF to the existing statement reader (guides, dating rule, duplicate check).

Research-rating flags and the morning summary are dropped for now [U D22].

---

## 3. Architecture [5.1, 2.1, U]

The design is a LangGraph **supervisor graph** with the orchestrator as supervisor. It mixes parallel, sequential and iterative patterns.

```
nightly:
  orchestrator ── reads registry, picks accounts due by cadence
      │
      ├─ fan-out, ≤ 3 branches at a time ─────────────────────────┐
      │   branch: fetch ─▶ extract ─▶ validate ─ok─▶ store         │
      │                      ▲            │                        │
      │                      └─ retry once (semantics deferred, §8)│
      │                                   │ 2nd rejection          │
      │                                   ▼                        │
      │                              review queue                  │
      ├─ join (all branches finished or timed out) ◀───────────────┘
      └─ (Stage 11+) analyst ─▶ trend explanations

on demand (Refresh button):
  orchestrator ── public-security accounts only ── same branch pipeline

daytime (Stage 12+):
  analyst alone, on demand, read-only access to the store
```

**Two levels of reasoning** [2.1]:

- The **orchestrator plans** from the registry, by rule.
- Each **worker runs a bounded ReAct loop** (Thought → Action → Observation) that must pass verification before it can finish.

**How workers react to an observation** [2.1]:

| Observation | Response |
|---|---|
| As expected | Continue with the playbook's next step. |
| Unexpected but recoverable (expired session, changed layout, slow page) | Re-plan within the branch: log in again, find the table another way, or try an alternate route such as a CSV or statement PDF. |
| Validation failed, or budget spent | Stop, save the evidence (screenshot, rows, the failed check) and queue the item for review. Never guess a value. |

**Unexpected branch failure.** The orchestrator doesn't improvise. The branch stops and a question goes to the inbox, to be answered the next time the user visits the UI [U D9].

**Coordination rules** [5.1]:

- Every message goes through supervisor state. No agent calls another directly, so every handoff is logged and replayable.
- Handoffs are typed and one-way by default. The two exceptions are both two-way:
  - the validator returns the specific failed check to the retry;
  - the user's approved corrections flow back to the extractor.
- There's no brainstorming between agents. A value is accepted because code checks pass, not because several models agree.
- Graph checkpointing lets a crashed run resume at the last finished node.

**Scaling** [5.1]:

- A new institution needs only a registry row and a playbook. That adds fetcher instances but no new role and no graph change.
- A broken portal fails only its own branch. Its account is shown as stale, not dropped from net worth.
- New capabilities attach as new nodes.

**Options considered and rejected:**

| Option | Why rejected |
|---|---|
| Single agent | One tool set for every task lets an injected instruction reach every tool. One broken login blocks unrelated work. The context can't hold ten portals at once [5.1]. |
| LLM critic on the extractor | Shares the extractor's blind spots. The code validator is deterministic, testable and free [5.1]. |
| Planner / researcher / writer split for Q&A | Adds handoffs to answers that are short and grounded in exact queries [5.1]. |
| Separate reporter and Q&A agents | They use identical tools; the handoff adds latency and no capability [5.1]. |
| Purely sequential run | A night becomes as slow as the sum of all portals, and one stuck login blocks the rest [5.1]. |
| Free-form agent network | Hard to test and debug [5.1]. |
| Tree of Thought | Hinders today: branches on live portals are real, irreversible actions; it adds model calls; it can't be tuned without failure data. See §12 for when to revisit [4.1]. |

---

## 4. Agents [5.1, U]

A boundary between agents sits wherever the tools, permissions or failure behavior change, and nowhere else.

**Reading vs extracting** [U C1]

- Any agent may read the raw page, email or PDF it needs for its job.
- **Only the extractor turns content into holdings and values.**
- The other agents capture and pass on the evidence (page, screenshot, PDF) from which values are extracted. They never write values themselves.

| Agent | Stage | Responsibility | Tools | Output |
|---|---|---|---|---|
| **Orchestrator** (supervisor) | 1 | Reads the registry and the date each account last produced a valid value. Selects accounts due tonight, or the public-security accounts on Refresh. Dispatches workers, routes failures by rule, joins results. | Registry, run state | Run plan; run status |
| **Portal fetcher** (one per institution) | 1 | Bounded ReAct loop: log in, navigate, capture the holdings page or download the statement, guided by the portal's playbook. Proposes playbook updates; never applies them [U D14]. | Browser, Auth tool, playbook retrieval | Evidence bundle; playbook proposal |
| **Statement fetcher** | 7, 13 | Finds capital statements (Stage 7) and monthly brokerage statements (Stage 13) in the labeled mail folder. Hands each PDF to the same statement reader as uploads, so the guides, dating rule and duplicate check apply unchanged. Scans email for the separate password message and opens the PDF. If no password email is found, asks through the inbox and uses the password the user stores [U D8]. | Mail reader, PDF tools, Auth tool | Evidence bundle |
| **Extractor** | 1 | Turns evidence into rows (holding, quantity, price, value, as-of) and, for capital statements, roll-forward fields. Retrieves the user's approved corrections for that fund first. Has no tools that act. | Document parser, corrections retrieval | Candidate snapshot; candidate roll-forward |
| **Analyst** | 11–12 | Stage 11: discovers trends and writes explanations. Private-fund moves are explained from investor letters only [U D4]. Stage 12: answers questions about trends. Cites numbers from the store and reasons from retrieved letters. Separates "values that changed" from "values that arrived late" before explaining anything [2.1]. | Store queries, calculator, retrieval | Trend explanations; cited answers |
| **Research agent** | dropped for now | Would collect published ratings for public holdings and flag any not rated Buy [5.1]. Dropped for now [U D22]; the research feed is revisited in Stage 12 [U D12]. If revived, it may look up tickers but never send holdings [U C14]. | Ratings tool, research-note retrieval | Rating flags |

Fetching and extracting are separate because fetching acts on live sites and extracting only reads a saved file. The extractor can be re-run on stored evidence without logging in again, and it never holds browser access [5.1].

**Deterministic components (not agents)** [5.1, 2.1, U]:

- **Validator** (§7)
- **Calculator**: returns, CAGR including all cash flows [U D6], and change attribution
- **Roll-forward reconciler** (§5.2) [U D5]
- **Portfolio store**
- **Auth tool**: encrypted credential store, SMS and email 2FA code readers. Secrets never reach the model. To get a 2FA code, it reads email or SMS, whichever suits the portal. If neither yields the code, it tells the user why and asks them to enter it [U C12].
- **Manual-input path** (Stage 10): a UI form that writes through the same validator (V10)
- **Read-only tool layer**, planned as MCP

Every tool returns a structured result with its source. A failure (timeout, page not found, wrong PDF password) comes back as an observation, not a crash.

*Today's code: the orchestrator (`runner.py`: nightly run, Refresh, per-login Refresh, a Daily Refresh that `serve` starts at a time set in the dashboard, 8:15 PM by default (`schedule.py`) [U, 2026-11-23], parallel runs that never share an institution's browser profile), the portal fetcher (`browser/portal.py`, with the LLM fallback navigator in `browser/navigator.py`), the extractor and validator inside the ingest graph (`ingest_graph.py`), statement upload (`statements.py`), and the ReAct Q&A agent (`react_graph.py`, not yet in the dashboard). The Auth tool has the encrypted credential store (`crypto.py`) and the SMS code reader (`sms.py`); there's no email code reader, statement fetcher, analyst or manual-input path yet.*

---

## 5. Data schema

### 5.1 Portfolio store: long-term factual memory [2.1, 3.1, U]

- **Bitemporal, with time of day.** Each value records:
  - account and holding;
  - `as_of_datetime`, with `as_of_date` derived from it;
  - `retrieved_at_datetime`;
  - source type: portal, API, statement (uploaded or, from Stage 7, emailed) or manual;
  - an evidence link.
- **Append-only.** Each pull is a new row, including several pulls on one day. A restatement is a new row with the same as-of and a later retrieval. Reads use the latest retrieval for each as-of.
- **One recorded exception (2026-11-12).** Two values filed under the wrong account (before account numbers on statements were read by code) were deleted at the owner's request, after a database backup. This is not a mechanism: corrections are still new rows [U].
- **Duplicate statements.** The same statement uploaded again (same file, or the same text in a fresh download) is ignored once ingested, and read again if it wasn't. A re-read with identical values for the same account and date is recorded as "unchanged" and adds no row [U, 2026-11-12].
- **Out-of-order ingestion is allowed.** Data for an older as-of date can arrive after newer data and is accepted [U C4].
- **Missing time.** If the source has no time of day, the current time is used (§1) [U D16]. A statement must show its date (the closing date of its period); a statement with no date is an error, never dated by the upload time [U, 2026-11-12].
- **Only validated values are written,** and only by code. A misread number stays in working memory and the review queue; it never becomes a fact that later runs compare against.
- **Every value has viewable evidence** for View Source [U C5].
- **Storage.** Plain (unencrypted) storage is acceptable for data. Credentials are always encrypted (§5.5) [U C9].

| Entity | Fields |
|---|---|
| Snapshot | institution, account (display name, never the account number), `as_of_datetime`, `retrieved_at_datetime`, source type, evidence, statement total |
| Statement upload | login, file name, private path, file hash, text hash, uploaded at, last run, last outcome. One row per distinct file; an upload is **ingested** when its last read stored or held its values and none was rejected as a misread [U, 2026-11-12] |
| Holding | symbol (if public), description, quantity, price, value, `price_as_of` (the holding's own date when the portal shows one, e.g. "Last Updated 06/30/2026" for a private fund; may be older than the page, never later) [U] |

*Today's code (`schemas.py`, `store.py`): both entities, with `as_of_datetime`, `retrieved_at`, source type (portal, statement, …), evidence and statement total; append-only bitemporal reads. Debt signs and part-owned shares are applied only when values are read. No roll-forward or cash-flow tables yet.*

### 5.2 Capital-account roll-forward [U D5, D6]

A separate schema stores each capital statement's roll-forward, so it can be reconciled over time.

| Field | Notes |
|---|---|
| account, period start, period end (= as-of) | Keys |
| beginning NAV | |
| contributions | Cash flow, used in CAGR |
| distributions | Cash flow, used in CAGR |
| gains / losses | |
| fees | |
| ending NAV | Must equal the NAV holding in the snapshot |
| `retrieved_at_datetime`, evidence | As in §5.1 |

**Reconciliation**

- **Within a statement:** beginning + contributions − distributions + gains − fees = ending.
- **Across statements:** one period's ending NAV = the next period's beginning NAV. A mismatch usually means a restatement, and is flagged.

The exact field list depends on what real statements show; see D24.

*Today's code: capital statements are uploaded and stored as **one holding**, the ending capital balance for the period, dated by the period end [U, 2026-11-18]. The roll-forward table and V7 aren't built yet.*

### 5.3 Cash flows [U D6]

- CAGR accounts for every withdrawal, contribution and distribution.
- Returns are shown both as **IRR** (money-weighted) and **time-weighted** [U D17].
- For capital-statement accounts, cash flows come from the roll-forward (§5.2).
- For portal accounts, the source of deposits and withdrawals is open (D17).

### 5.4 Account registry: configuration memory [2.1, U]

The user maintains the registry through the UI; agents only read it [U D1].

**Logins and accounts** [U, 2026-11-11]:

- A **login** is one set of portal credentials. One login can hold several accounts, and data is stored per individual account.
- The login carries the fields below: URL, domains, 2FA, cadence, credentials and steps.
- Accounts are found on each fetch and matched by name plus the last 4 digits of the account number, if shown.
- **Account numbers are read by code, not the model:** from each account's details page on a portal, and from the "Account Number" line of a one-account statement. When a portal page reads a new number for an account of the same name, the entry is moved to it; statements never do this, since one statement covers one account [U, 2026-11-12].
- A new account starts as **pending**, and its first value waits for approval.
- The user can rename an account, set its type, or mark it **ignored** or **closed**.
- Every account on a page is kept, including accounts the portal leaves out of its overall total (§7, V11–V12).
- **Include or exclude** [U]. The first time an account is seen, the Inbox asks whether to include it.
  - **Include** stores the value and tracks the account.
  - **Exclude** marks it excluded: it's skipped on every later fetch and left out of charts, totals and net worth.
  - The choice can be changed on Accounts & logins at any time.
- **Login rules** [U]. Each login can carry rules for the accounts its pages list:
  - **Skip categories,** e.g. `Investments` on Bank of America, whose Merrill accounts are already fetched by the Merrill login.
  - **Skip accounts held at another institution:** an account the page labels as held elsewhere, e.g. a Merrill loan on a Bank of America page.
  - **Debt categories,** e.g. `Loans`. These accounts are stored as **negative** values because the money is owed, and they reduce net worth.
  - **Numbers that aren't accounts,** e.g. a credit union member number shown on the page. They don't count as accounts the extraction missed (V12).
  - Each account's debt flag can be changed on Accounts & logins. An account a skip rule matches is marked excluded.

| Field | Records | Used by |
|---|---|---|
| Display name | e.g. "Schwab joint brokerage" | Dashboard, summary, Q&A |
| Institution, account type | Brokerage / retirement / private fund / bank / loan / credit card | Grouping; cadence |
| Access method | Browser or **upload**; API, email statement and manual are shown greyed out and can't be chosen until built [U, 2026-11-22]. Upload: the user uploads monthly PDF statements in the dashboard, for institutions whose portal can't be read daily; nightly runs and Refresh skip these logins. Chosen by the user; the system doesn't decide [U D1, 2026-11-12] | Orchestrator |
| Login mechanism | Portal URL, credential reference, and the login steps the user enters (Stage 1) [U]. **Sign in with:** username and password, or **Google** (the site's "Sign in with Google"; the user signs in to Google by hand once) [U, 2026-11-12] | Fetcher; browser allowlist |
| 2FA method | Remembered device, SMS or email (authenticator app removed [U, 2026-11-22]). The Auth tool tries email or SMS first, and asks the user when it can't get the code [U C12] | Auth tool |
| Statement sender | Full email address of the statement sender | Mail filter |
| PDF password source | Sender of the password email, or a password the user stores [U D8] | Statement fetcher |
| Update cadence | Nightly; **monthly** (stale 45 days after the last statement); or **quarterly** with a 2–4 month lag (stale 120 days after the last statement, provisional pending D10) | Orchestrator; staleness check |
| Refresh supported | Not a setting: yes for every Browser login, no otherwise. Yes: the login has a Refresh button and is part of the Dashboard's Refresh; no: it shows Upload statements [U, 2026-11-22; replaces "holds public securities"; tied to Browser access, U 2026-11-23] | Refresh |
| Status | Active, or closed with a close date | Orchestrator; completeness; history |

- **Every edit is checked in code:** the credential exists, the portal domain is added to the allowlist, and the sender is a full email address [2.1].
- **Agents can suggest, not change.** A new sender or portal address becomes an inbox suggestion. Only the user approves registry changes [2.1, 6.1].

*Today's code (`registry.py`, Accounts & logins in the dashboard): logins with URL, domains, 2FA method, cadence (nightly, monthly), access method (browser or upload; browser logins can be refreshed) and encrypted credentials; accounts with display name, type, category, status (pending, active, excluded, ignored, closed), debt flag and owner; login rules. Not yet: statement sender, PDF password source, quarterly cadence, and registry suggestions from agents.*

### 5.5 Credentials [U C9, D8]

- Covers portal logins and PDF passwords.
- Encrypted at rest in the local database. **macOS Keychain is not used** [U D18].
- Entered and updated through the UI. A stored password is hidden by default and revealed with an eye icon [U D18].
- Filled by the Auth tool directly. Never passed to the model, prompts or logs [2.1, 6.1].

Where the encryption key lives, without Keychain, is open (D18).

### 5.6 Other memory [2.1, U]

| Memory | Holds | Written | Read |
|---|---|---|---|
| Working memory | The run's thoughts, actions and observations; session state; rows awaiting validation | Each loop step | Next Thought |
| Conversation context | The current Q&A thread (Stage 12) | Each question | Follow-ups |
| Playbooks (procedural) | Per institution: login route, 2FA method, where holdings live, layout quirks | Fetchers propose changes; the user approves them daily in the UI [U D14] | Start of each fetcher run |
| Corrections and documents | Values the user corrected and why; statements; investor letters | After review; when documents arrive | When a similar case comes up |

Working memory is cleared after each run, except the evidence trace saved with the value or flag.

**Rule: numbers are queried, never retrieved by similarity.** The vector index holds text only [2.1].

### 5.7 Handoff message [5.1]

Each agent writes a structured message to graph state with these fields:

- account ID
- evidence file path
- rows
- `as_of_datetime`
- `retrieved_at_datetime`
- status

Secrets never appear in messages.

### 5.8 Inbox items: review queue [4.1, 6.1, 2.1, U]

| Kind | Contents |
|---|---|
| Held value | Evidence, extracted rows, the failed check, and a cause tag: **wrong field, wrong column, units, wrong period, real change, parsing error** [4.1] |
| Question | What failed and what the system needs from the user [U D9] |
| Password request | Which statement needs a PDF password [U D8] |
| 2FA code request | Which portal is waiting, why the code couldn't be read from email or SMS, and a field for the code [U C12] |
| Playbook proposal | The proposed change, with the run that prompted it [U D14] |
| Registry suggestion | New sender or portal address [2.1, 6.1] |

*Today's `review_queue` has kinds: review (rejected twice), held value, question, 2FA code, playbook proposal, registry suggestion and alert. There's no cause tag and no password request yet.*

### 5.9 Retrieval index [3.1]

| Element | Details |
|---|---|
| Chunk metadata | account, fund, document type, as-of date, date received, source, page |
| Chunk header (embedded with the text) | fund name, document title, section |
| Sources sharing the index | investor letters, capital-statement notes, playbooks, approved corrections. Research notes are deferred (D12). |

### 5.10 Run trace [6.1]

Every run, nightly or Refresh, keeps a trace of actions and evidence. The trace is used for monitoring, replay and review.

### 5.11 Recommendations [U C7]

Settings suggested from observed runs. The first one is a Q&A step budget [U C7].

- Stored in a **separate recommendations file**, not in this spec or the code [U D20].
- Shown in the UI's Recommendations section, where each one can be **accepted, rejected or revised** [U D20].

---

## 6. Retrieval [3.1, U]

Retrieval is for **text only**: why a value changed, what a statement says, how to get a statement. "What is it worth / how did it change" is an exact store lookup.

| Consumer | Retrieves | Purpose | Stage |
|---|---|---|---|
| Portal fetchers | That portal's playbook | Find statements at the start of a run | 1 |
| Extractor | Approved corrections | Avoid a mistake already fixed (e.g. "Total commitment" read as value) | 1 |
| Analyst | Investor letters only, for private-fund moves [U D4] | Explain why a value changed | 11 |

**Pipeline**

1. **Ingest.** The statement fetcher saves each new document with its metadata.
2. **Parse.** Tables go to the extractor, and their numbers go to the store after validation. Narrative text goes to the index.
3. **Chunk.** Split by section heading. Long sections are split by sentence into chunks of about 300–400 tokens, with a one-sentence overlap.
4. **Embed** with a local sentence-embedding model.
5. **Search.** Filter by metadata first: the account must match and the as-of date must fall in the period asked about. Then rank by **0.7 × semantic + 0.3 × keyword**. Keywords keep tickers and fund names exact.
6. **Select.** Take 8 candidates. The **top 3 scoring ≥ 0.55** go to the LLM. If none score ≥ 0.55, abstain, e.g. "latest letter on file is as of Jun 30" [U C8].
7. **Answer.** The LLM writes only from the retrieved chunks and cites each one (fund plus as-of date). Numbers come from the store. Any percentage in a chunk is cross-checked against the store's change, and a mismatch is flagged (tolerance: D7).
8. **Refresh.** New documents are indexed nightly. Old ones are kept with their as-of dates.

Retrieved text is data, never instructions.

*Not implemented (Stage 11). Playbooks exist as plain text keyed by institution (`store.get_playbook`), and statement layout notes live in `statement_guides.py`.*

---

## 7. Validator checks [2.1, 6.1, 4.1, U]

The validator is deterministic code with no LLM. A snapshot is accepted only if every check passes. The validator returns the specific failed check so the retry is targeted.

| # | Check | Spec | Today's code (`validator.py`) |
|---|---|---|---|
| V1 | **Non-empty** | Some holdings were extracted | ✅ hard error |
| V2 | **Sum to total** | Holding rows sum to the account total shown on the page or statement | ✅ 1% tolerance (`PA_SUM_TOLERANCE`); a missing total is a warning |
| V3 | **Quantity × price** | qty × price ≈ value for each holding, using the portal's own numbers. For an option, qty × price × 100 (one contract is 100 shares). **No market-feed check** [U C3] | ✅ 2% tolerance; options × 100 |
| V4 | **Non-negative values** | A negative holding in an asset account is a likely sign misread, except a **sold option** or **margin cash**, allowed when the account has a stated total for V2 to check against. A sold option's value is set in code to −(contracts × 100 × price), whatever sign the statement prints [U, 2026-11-12] | ✅ |
| V5 | **As-of not in the future** | An as-of date up to one day after today is accepted: portals often date values in Eastern time, which can read as tomorrow [U, 2026-11-22] | ✅ |
| ~~V6~~ | ~~As-of not earlier than the last accepted value~~ | **Removed.** Out-of-order ingestion is valid [U C4] | ✅ removed |
| V7 | **Roll-forward** | Each capital statement reconciles within itself, and with the previous period (§5.2) [6.1, U D5] | ❌ not implemented; no roll-forward schema |
| V8 | **Continuity** | The change from the **nearest earlier accepted value** is explainable [U D19]; a large unexplained move is flagged or held [2.1, 6.1]. Market data looked up by ticker may serve as the benchmark [U C14]. Outcome (C6) and threshold and benchmark (D3) are open | ⚠️ a move above 25% (`PA_LARGE_MOVE`) is held for approval, not rejected; market move is an optional input; the benchmark (D3) is still open |
| V9 | **Traceability** | Each value has stored, viewable evidence, as required by View Source [4.1, U C5] | ⚠️ evidence is stored with every value and shown by View Source; not checked as a rule |
| V11 | **Overall total covered** | When a page shows an overall total, the accounts extracted add up to at least that total. They may add up to more: some accounts sit outside the portal's overall value and are still kept [U] | ✅ `validate_extraction` |
| V13 | **Live page is current** | For a portal page, the as-of is within 5 days of when the page was captured (`PA_PORTAL_MAX_AGE_DAYS`). An account shown with its own as-of date (e.g. a fund valued at last quarter end) uses that date instead, like a holding's `price_as_of` [U, 2026-11-18]. The extractor is told the capture date, and a page with no date uses the capture time (D16) [U] | ✅ `ingest_graph.check` |
| V12 | **Every listed account extracted** | Every account number listed on the page (on its own line, e.g. `12A-34567` or `...1234`) matches an extracted account's last 4 characters [U]. Numbers the user marks as not accounts (a member or card number) are left out; a page rejected only for such numbers can be accepted from the Inbox, which saves them for the login [U, 2026-11-12] | ✅ `unlisted_accounts`, `accept_review` |
| V10 | **Manual input** (Stage 10) | User-entered values pass the same checks as other values for that account type. View Source shows who entered the value, when, and an optional attachment [U D23] | ❌ |

The continuity threshold and the step budget are to be tuned from test runs [2.1].

---

## 8. Retry and escalation flow [4.1, 5.1, 6.1, U]

**Retry semantics are deferred** [U C2]. Until they're revisited, the current code's behavior stands:

1. **First rejection.** Re-extract once, with the validator's errors attached.
2. **Second rejection.** The item goes to the inbox with its evidence and the failed check. The value isn't stored.
3. **Review.** The user fixes the value, and the fix is saved as a **correction**, which is indexed.
4. **Next time.** Retrieval brings the correction back before the extractor reads the same fund's next statement.

The docs' "retry by another route" (e.g. the PDF instead of the web page) is parked with C2.

*Today's code does steps 1–2 (`MAX_ATTEMPTS = 2`; the second rejection becomes a "Rejected twice" Inbox item). Held values are approved or rejected in the Inbox, but there's no fix-and-save correction and no correction indexing yet.*

---

## 9. Guardrails [6.1, 2.1, U]

### 9.1 Risks

| Risk | Example | Harm | Primary control |
|---|---|---|---|
| Wrong value stored | "Total commitment" read as NAV; 1,032,000 read as 1,302,000 | False history that later checks compare against | Validator; review; View Source |
| Wrong period | June 30 value received in October treated as this week's change | Misleading explanations | Bitemporal store; analyst separates late from changed |
| Unintended action | Fetcher clicks transfer or settings | Real, irreversible loss | Read-only tools; blocked controls |
| Account lockout | Repeated logins, repeated Refresh clicks, a flood of 2FA codes | Locked out | Login cap; SMS lock |
| Prompt injection | Email or PDF tells the agent to forward statements | Data leak or tool misuse | Text treated as data; no egress; only the extractor produces values, and it has no acting tools |
| Secret exposure | Password or 2FA code reaches the model, a log or a prompt | Account compromise | Auth tool fills from the encrypted store |
| Data egress | Quantities, values or account details sent to an outside service | Privacy breach | Only ticker symbols may leave the Mac [U C13, C14] |
| Ungrounded answer | "Market conditions" offered as a guess | Confident wrong advice | Citations; cross-check; abstain |
| Silent staleness | Broken portal; old value shown as current | Net worth looks right but isn't | Completeness check; stale label |

### 9.2 Controls

**Tool access**

- Every tool is read-only.
- The browser is restricted to registry portal domains and may **submit only the login form**, plus the documents page's own search box for a capital-statement login [U, 2026-11-18].
- Passwords are typed only on the login's own domains. **Sign in with Google:** `accounts.google.com` is allowed only during sign-in. There the fetcher may only pick an account in Google's chooser; it never types into Google's pages or grants access, and anything else Google asks for stops the run with an Inbox question [U, 2026-11-12].
- Clicks on controls labeled **transfer, trade, withdraw, order, delete, settings** are blocked in code.
- **CAPTCHAs are never answered by the agent.** When one appears, the run brings its (visible) window to the front, posts an Inbox note, and waits up to 5 minutes for the user to solve it; then it continues. With a hidden window it stops and asks the user to sign in by hand [U, 2026-11-18].
- Credentials are filled from the encrypted store by the Auth tool and never pass through the model.
- **No network egress of extracted data**: no aggregators. Outbound lookups may carry ticker symbols only, never quantities, values or account details [U C13, C14].

**Input checks**

- **Statement mail reader:** opens one labeled folder, and only messages from full sender addresses in the registry, including the password sender [6.1, U D8].
- **2FA code readers (SMS and email):**
  - code-only, never exposed to the LLM;
  - each looks for a single code within a short time window after a login the agent started;
  - the email reader may search any label for this purpose only [U C12, D2];
  - if no code can be read, the user is asked for it, with the reason (§10) [U C12].
- **No reader marks, archives or deletes any message** [U D2].
- **Reading content** [U C1]:
  - every agent treats page, email and PDF text as data, never as instructions;
  - only the extractor produces values.
- Only the user can change the registry and playbooks [2.1, U D14].

**Runtime limits**

- Each fetcher has a budget of about **25 steps** and a time limit (D10) [5.1, 6.1].
- **Q&A:** no step budget at first, with a **30-second time limit**. A budget is recommended later from observed runs [U C7, D13].
- At most **2 login attempts per portal per nightly run** [6.1]. Limits for Refresh will be set over time from experience [U C15].
- **One SMS code expected at a time:** SMS logins take a lock.
- A **CAPTCHA, an unknown 2FA prompt or an unexpected domain stops the branch.**
- At most **3 parallel branches**.

**Output constraints**

- Values are written only by code, after the validator passes (§7).
- Every displayed value links to its evidence (View Source).
- Q&A answers and trend explanations cite store numbers and a retrieved document.
- Any percentage in a document is cross-checked against the store.
- The analyst abstains when no source scores ≥ 0.55 (§6).

**Monitoring**

- Every run keeps a trace of actions and evidence.
- A **completeness check** marks any active account without an accepted value inside its cadence as stale. "Inside cadence" means by morning for nightly accounts, and within the expected lag for quarterly funds [2.1].
- A skipped nightly run is not an error for trends, but the accounts it missed still show as stale [U].
- Blocked actions and off-allowlist navigation raise an alert in the inbox.

**Kill switch**

- A single switch stops all fetchers. Because the agent is read-only, stopping it never leaves an account half-changed.

*Today's code: a read-only tool set (`tools.py`) with no trade or transfer tool; the extractor's system prompt treats statement text as data; `STEP_BUDGET = 8` for the Q&A loop, which should become a 30 s time limit with no step cap.*

---

## 10. Human-review triggers [6.1, 2.1, U]

The agent proceeds alone only for routine, verified work: a value that passes every check, or the single retry. Otherwise it defers to the user through the inbox:

| Trigger | Condition | Action |
|---|---|---|
| **Uncertain data** | Validator rejects twice, **or** a change exceeds the continuity threshold (outcome open, C6) | Hold the value with its evidence; don't store it until approved |
| **First of its kind** | First value for a new account; first run after a playbook change was approved | Hold for approval |
| **Security or policy prompt** | CAPTCHA, new 2FA method, security question, device verification, terms-of-service page, or any page asking for an action | Stop the branch; ask the user |
| **Unexpected failure** | A branch fails in a way the rules don't cover | Stop the branch; ask on the next UI visit [U D9] |
| **Missing PDF password** | No password email found | Ask in the UI; store the answer encrypted [U D8] |
| **2FA code unavailable** | The code can't be read from email or SMS, e.g. an authenticator app | Tell the user why, and wait for them to enter the code [U C12] |
| **Playbook proposal** | A fetcher learned something about a portal | Show in the daily approval list [U D14] |
| **Change to trust** | New statement sender; new portal address | Agent suggests; only the user approves |
| **Blocked behavior** | Any blocked click or off-allowlist navigation | Stop the branch; alert |
| **Weak evidence for an answer** | No source scoring ≥ 0.55, or a cross-check mismatch | Analyst says so instead of explaining |
| **Repeated failure** | Same portal fails 3 nights in a row | Alert: the playbook likely needs updating |

Separately, View Source lets the user spot-check any value at any time [U C5].

Accounts in supervised mode add one more case: every new private-fund value waits for approval (§12).

---

## 11. Metrics [6.1, U]

With the synthetic milestones dropped [U D21], metrics are measured on **live runs**. The docs measured accuracy, calibration and injection resistance on a labeled synthetic set [6.1]; how to measure them without one is open (C16). Targets will be tuned on real data.

| Area | Metric | Target |
|---|---|---|
| Correctness | Field accuracy of extracted value and as-of on the test set | ≥ 99% |
| Correctness | Escape rate: accepted values later corrected | 0 |
| Correctness | Roll-forward reconciliation (Stage 4+) | 100% of accepted statements |
| Traceability | Displayed values with working View Source | 100% |
| Groundedness | Claims with a citation / citations that support the claim, spot-checked (Stage 11+) | 100% / ≥ 95% |
| Calibration | Abstains when no source exists / answers when one does (Stage 11+) | 100% / ≥ 90% |
| Safety | Injection cases that trigger any tool call / secrets in logs or prompts / lockouts / extracted data sent off the Mac | 0 / 0 / 0 / 0 |
| Reliability | Coverage: active accounts with an accepted value inside their cadence | ≥ 95% |
| Fallback success | First-time rejections fixed by the single retry | Tracked; rising |
| Escalation rate | Share of values sent to review | 5–15% (see C5) |
| Latency | Nightly run finished by 7 a.m. / Q&A answer time (hard limit) / review time per week | Yes / ≤ 30 s / < 20 min |

**How to read the escalation band.** Above 15%, the validator is too strict. Below 5%, the checks may be letting errors through [6.1].

**What each metric tests.** The escape rate tests the validator. The escalation rate tests its thresholds. The injection set tests the input boundary [6.1].

---

## 12. Rollout and future reasoning [6.1, 4.1]

**Go-live sequence** [6.1, U D21]. The synthetic-test-set step is dropped; each portal goes straight to real data [U D21]. Whether shadow mode still comes before live for each new portal is open (D21).

| Step | What happens |
|---|---|
| ~~Synthetic test set~~ | Dropped [U D21]. |
| Shadow mode (4 nights) | Nightly runs on real accounts; each value is checked against the portal; nothing is trusted. Private funds are back-tested on the last 2–3 statements already in the mailbox. With V6 removed, these can be loaded in any order [U C4]. |
| Live: nightly accounts | Accounts with 4 clean shadow nights go live. |
| Supervised: private funds | Every new value waits for approval until the first live quarterly statement passes the checks. |

An account that fails repeatedly returns to shadow mode.

**When to reconsider Tree of Thought** [4.1]. Over about six months of tagged rejections, ToT gets built if either of these happens:

- ambiguous rejections, where the statement could be read several ways, occur several times; or
- the same error type recurs after a correction was approved for it.

If triggered, ToT would be:

- a **fallback for rejected statements only**;
- beam search with **beam 2, depth 3**;
- pruned by current-statement checks (roll-forward, traceability, date);
- ranked by checks against history and the fund letter;
- if the top two readings disagree, both go to review.

---

## 13. Decision log (2026-11-11) [U]

### 13.1 First round

The owner's ruling on each item from the first version of this spec.

| # | Topic | Ruling | Status |
|---|---|---|---|
| C1 | Injection boundary | Every agent may read what it needs; only the extractor turns content into holdings and values. The others just capture evidence. | Resolved |
| C2 | Retry semantics | Not a concern for now. | Deferred |
| C3 | qty × price vs market feed | No market-feed check; rely on the portals' own numbers. | Resolved |
| C4 | As-of ordering | Don't reject an earlier as-of; out-of-order ingestion is valid. V6 removed. | Resolved |
| C5 | Escalation band vs review-by-design | Values must be accurate; every value gets View Source for manual review. | Resolved in part; the band question stays in C5 |
| C6 | Continuity outcome | Owner asked for an explanation. | Open |
| C7 | Step budgets | No Q&A budget at first; the UI later recommends one in a Recommendations section. | Resolved; see D20 |
| C8 | Abstain threshold | ≥ 0.55. | Resolved |
| C9 | Encrypted store | Data may be plain; all logins encrypted, and updatable in the UI. | Resolved |
| C10 | Market data agent | Owner asked for an explanation. | Folded into C14 |
| C11 | Who writes playbooks | Owner asked for an explanation; settled in effect by D14. | Resolved via D14 |
| C12 | Email 2FA | Portals send codes by email; reading any label is allowed for that purpose only. | Resolved for email; authenticator app is still open (C12) |
| C13 | Data leaving the Mac | No extracted data from brokerage or PE accounts leaves the Mac. | Resolved; raises C14 |
| D1 | API vs browser | The user picks the access method in the UI. | Resolved |
| D2 | SMS reader | A time-window filter is fine; never archive or delete messages. | Resolved |
| D3 | Continuity threshold / benchmark | Owner asked for an explanation. | Open |
| D4 | Explaining private-fund moves | Investor letters only. | Resolved |
| D5 | Roll-forward fields | Separate schema, reconciled over time. | Resolved (§5.2) |
| D6 | Cash flows | All cash flows count in CAGR; store the full roll-forward. | Resolved; raises D17 |
| D7 | Cross-check tolerance | Owner asked for an explanation. | Open |
| D8 | PDF password | Fetcher scans email for the password message; otherwise ask in the UI and store encrypted. | Resolved |
| D9 | Orchestrator re-planning | Ask the user on their next UI visit. | Resolved |
| D10 | Definitions | Owner asked for an explanation. | Open |
| D11 | Corrections schema | Owner asked for an explanation. | Open |
| D12 | Research feed | Revisit in Stage 12. | Deferred |
| D13 | Q&A time limit | 30 s. | Resolved |
| D14 | Playbook rewrites | Proposed changes shown daily in the UI for approval. | Resolved |
| D15 | Dashboard framework | Owner asked for an explanation. | Answered in round 2 |

### 13.2 Second round

| # | Topic | Ruling | Status |
|---|---|---|---|
| C10 | Market data agent | Settled by C14: ticker lookups are allowed, through code. | Resolved |
| C12 | Authenticator-app 2FA | Try email or SMS, whichever makes sense. If that fails, tell the user why and ask for the code. | Resolved |
| C14 | No-egress vs outside data | Looking up external data by ticker is fine, including for research, as long as actual holdings aren't sent. | Resolved |
| C15 | Refresh vs login limits | Keep the nightly limit; Refresh limits to be worked out over time. | Deferred |
| D15 | Dashboard framework | Small FastAPI backend with a web front end. | Resolved |
| D16 | Missing as-of time | Use the current time. | Resolved |
| D17 | CAGR method | Show both IRR and time-weighted. | Resolved in part; portal cash-flow source still open |
| D18 | Credential encryption | Don't use Keychain. Show stored passwords behind an eye icon. | Resolved in part; key location still open |
| D19 | Continuity comparison point | Nearest earlier value. | Resolved |
| D20 | Recommendations output | A separate file; accept, reject or revise in the UI. | Resolved |
| D21 | Rollout vs stages | Drop the synthetic-data milestones; go straight to portals. | Resolved in part; raises C16, shadow mode still open |
| D22 | Morning summary, research flags | Dropped for now. | Resolved |
| D23 | Manual inputs | Yes: same checks, and View Source shows who, when and an optional attachment. | Resolved |

### 13.3 Third round (2026-11-12)

| # | Topic | Ruling | Status |
|---|---|---|---|
| S1 | Institutions without daily access | Upload monthly statements (one per account or consolidated) as a login's access method, cadence monthly. Built first for Rockefeller, then Schwab. | Resolved; built |
| S2 | "Use my Chrome" sign-in | Removed: it didn't work. | Resolved; removed |
| S3 | Sold options | Value = −(contracts × 100 × price per share), even when the statement prints it positive. | Resolved; built |
| S4 | Duplicate statements | Allowed. Ignored if already ingested correctly; read again if not. | Resolved; built |
| S5 | Account numbers on statements | Read by code from the statement; a statement never moves an existing account to another number. | Resolved; built |
| S6 | Misfiled values | Delete them outright rather than mark them withdrawn (one-off, with a backup). | Resolved; done |
| S7 | Email for brokerage statements | Add Stage 13: brokerage statements arrive by email and are read automatically; manual upload becomes a fallback. | Planned |
| S8 | Sign in with Google (Frec) | Supported as a login mechanism; the fetcher never types a Google password. The owner signs in to Google by hand; runs reuse that session. | Resolved; built, not yet tried on the real site |
| S9 | Frec statements | Read the first page only; the value on the closing date is the Closing Balance of TOTAL PRICED PORTFOLIO, stored as one holding (no positions). | Resolved; built, not yet tried on a real statement |
| S10 | Extra numbers on a page | A page rejected only because it shows numbers that aren't accounts (e.g. a member number) can be accepted in the Inbox; the numbers are saved as "not accounts" for the login. | Resolved; built |
| S11 | Capital account statements | Start with upload; store one holding per fund (ending capital for the period, dated by the period end). Login setting "Statement type": account or capital. Roll-forward later. | Resolved; built, not yet tried on a real statement |
| S12 | Capital statements from a fund portal | Browser login → Documents tab → filter or search "Capital Account Statements" → download the latest statements (newest quarter listed) and read each as a capital statement (Strategic Partners first). Tab and filter labels are per-login settings. | Resolved; built, tried on fake pages only |
| S13 | Strategic Partners values from its portal page | After sign-in, read Performance Summary (USD): each fund's Unrealized Value, dated by its own As of Date column (last column, e.g. Jun-30-2026), read by code from the row, overriding the model. The check total is Total Unrealized Value, not Total Current Value (which adds realized value already paid out). No Documents step. Accounts can carry their own as-of on a portal page. | Resolved; built, tried on made-up pages only |
| S14 | Star Mountain capital statements, per fund | Open each fund (named in the dashboard's \"Fund Name\" column) → its Documents tab → wait → latest file named "Capital Statement" → Ending Balance for the period, dated by the date at the top. Waits 30 s after sign-in for the page to load (per-login setting). | Resolved; built, tried on made-up pages only |
| S15 | Frec from its dashboard | Sign in with Google (installed Chrome), then read the page after sign-in: each account's total value, one holding per account. Per-login setting "Read the page shown after sign-in". | Resolved; built, not yet tried on the real site |
| S16 | Scanned statements | A PDF with no text layer is read by OCR on the Mac (macOS Vision), not a cloud service. | Resolved; built |
| S17 | Balance-only accounts and ignored numbers | An account read with a total but no holdings (card, loan, bank) gets one holding worth that total, in code, instead of a retry. The missed-account check skips numbers of the login's ignored or closed accounts, which the page may still list. | Resolved; built, tested on made-up data |
| S18 | Sign-in that sends its code on request (Tattva) | Login step "button that sends the code": after username and password, press it, get the code (from Gmail, S19, else the Inbox), type it, press Login, then wait up to 60 s for the form to go away (slow portals show a notice first; an error message next to the form ends the wait). Then the documents tab (e.g. NAV) with no filter; a monthly login downloads only the newest month's statement. | Resolved; built, tried on a made-up portal only |
| S19 | Emailed verification codes from Gmail | Owner's choice: read emailed codes from Gmail over IMAP with an app password (encrypted). Read-only, PEEK; only messages after the request, from the login's domains or its code sender (e.g. a different domain than the portal). Inbox is the fallback. The only outside connection besides the portals. | Resolved; built, tested with a fake mailbox |

---

## 14. Contradictions and open decisions

### 14.1 Contradictions still open

| # | Contradiction | Sources |
|---|---|---|
| C5 | **Escalation band vs review-by-design.** The target is 5–15% of values sent to review. But every first value, every run after a playbook change, and every private-fund value in supervised mode goes to review by design, so early rates will sit far above the band. Do those count toward the band? | 6.1 |
| C6 | **What happens to a big unexplained jump.** 2.1 says flag it but accept it; 6.1 says hold it until approved. The code now holds it for approval (the 6.1 option) instead of treating it as an extraction error; confirm that's the ruling. | 2.1, 6.1 |
| C16 | **No synthetic data vs testing and the demo.** With the synthetic milestones dropped, there's no labeled set to measure field accuracy, abstain/answer calibration or prompt-injection resistance before a change reaches real accounts. 5.1 and 6.1 also say the demo uses synthetic data only. Options: keep a small synthetic set for tests and the demo only, while rollout uses real portals; or measure accuracy from View Source spot checks and corrections, and test injection on hand-made samples. | U D21 vs 5.1, 6.1 |

### 14.2 Open decisions

| # | Decision needed | Notes |
|---|---|---|
| D3 | **Continuity threshold and benchmark.** How big a jump counts as suspicious (code: 25%), and what it's compared against to tell a real market move from a misread | Ticker lookups are now allowed (C14), so an index or the holdings' own price moves could be the benchmark. |
| D7 | **Cross-check tolerance.** How close a letter's "fund fell 5.8%" must be to the store's −6.0% to count as consistent | 3.1 calls that pair consistent but gives no number. Applies from Stage 11. |
| D10 | **Definitions:** the fetcher's time limit; the rule for when a quarterly statement is "due"; how many failures send an account back to shadow mode; whether 3 failed nights also marks the account stale | |
| D11 | **Corrections schema.** When the user fixes a value, what's saved so the extractor can avoid the same mistake: field, wrong value, right value, reason, fund, statement | Needed by §8 step 3. |
| D17 | **Portal cash flows.** IRR and time-weighted returns both need deposits and withdrawals. Capital statements give them; where do portal accounts' come from (a transactions page, statements, or manual input)? | Method resolved [U D17]. |
| D18 | **Encryption key location** without Keychain: e.g. a key file with owner-only permissions, or a passphrase typed at startup. A passphrase blocks unattended nightly runs until it's entered. | Eye-icon reveal resolved [U D18]. |
| D21 | **Shadow mode.** With synthetic data dropped, does each new portal still run a few shadow nights before going live, or go live at once? | |
| D24 | **Roll-forward details.** The final field list (e.g. separate management fees and carry, recallable distributions), and the reconciliation tolerance | |

---

## Appendix: current code vs spec

| Area | Status in `portfolio_agent/` |
|---|---|
| Bitemporal, append-only store; review queue; playbooks | ✅ `store.py`: `as_of_datetime`, retrieval time, source type, Inbox item kinds, playbooks and playbook proposals |
| Snapshot / Holding schema | ✅ `schemas.py`, with `price_as_of`. ❌ roll-forward table, cash flows, cause tags |
| Validator V1–V5, V11–V13 | ✅ `validator.py`, `ingest_graph.check` (V3 options × 100; V4 allows sold options and margin cash) |
| Validator V6 | ✅ removed (U C4) |
| Validator V7 (roll-forward), V9 (traceability) | ❌ / ⚠️ |
| Validator V8 (continuity) | ⚠️ held for approval above 25%; benchmark open (D3) |
| Extract → validate → retry once → review | ✅ `ingest_graph.py` (retry semantics deferred) |
| Capital-statement extraction (NAV, excluding unfunded commitment) | ✅ `extractor.py` (`CAPITAL_NOTE`, upload logins with statement type "capital"); no roll-forward fields |
| ReAct Q&A with read-only tools | ✅ `react_graph.py`, `tools.py`, not yet in the dashboard. Needs a 30 s limit instead of `STEP_BUDGET = 8`. Not needed until Stage 12. |
| Orchestrator, nightly run, Refresh | ✅ `runner.py`, `cli.py`; parallel runs, cancel, run trace in the Debug tab |
| Portal fetcher | ✅ `browser/portal.py`: persistent profile per institution, playbook steps, LLM fallback navigator, account numbers from details pages |
| Auth tool | ⚠️ encrypted credentials (`crypto.py`, key file `~/.portfolio-agent/secret.key`) and SMS codes (`sms.py`); 2FA code request in the Inbox. ❌ email code reader |
| Statement upload (upload logins, guides, duplicate check, sold options) | ✅ `statements.py`, `statement_guides.py`, `ingest_graph.py` (Stage 1 addition) |
| Statement fetcher (email, Stages 7 and 13) | ❌ |
| Sign in with Google | ✅ `browser/portal.py` (`_google_login`); tried on fake pages only |
| Capital statements from a portal's documents page (Stage 4) | ✅ `browser/portal.py` (`_fetch_documents`), `runner._read_documents`; tried on fake pages only. No pagination yet |
| Registry UI, encrypted credentials | ✅ Accounts & logins: logins, accounts, include/exclude, login rules, debt flags, owners and part-owned shares. ❌ statement sender, PDF password source |
| Dashboard | ✅ FastAPI + web front end (`web/`): net worth, charts (daily/monthly, split by account/institution/type/none, "as known on"), Accounts table with filters, View Source, Inbox, Debug. ❌ IRR / time-weighted returns, recommendations file |
| Completeness / stale check | ✅ `series.account_status` (nightly and monthly cadences) |
| Retrieval index, corrections, citations, cross-check | ❌ (Stage 11) |
| Guardrails: allowlist, blocked clicks, login caps, kill switch | ✅ `browser/guard.py` (allowlist, blocked clicks), `PA_MAX_LOGINS` per night, `data/private/STOP` kill switch |
| Manual inputs (Stage 10) | ❌ |
| Metrics harness | ❌ (`tests/test_core.py` covers the core with a fake model and fictional samples; see C16) |
