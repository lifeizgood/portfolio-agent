"""Settings, read from environment variables so nothing is hard-coded."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PRIVATE_DIR = Path(os.getenv("PA_PRIVATE_DIR", DATA_DIR / "private"))  # gitignored

# SQLite file holding the portfolio store (snapshots, registry, inbox, playbooks).
DB_PATH = Path(os.getenv("PA_DB_PATH", DATA_DIR / "portfolio.db"))

# Evidence (screenshots, page HTML/text) saved for View Source.
EVIDENCE_DIR = Path(os.getenv("PA_EVIDENCE_DIR", PRIVATE_DIR / "evidence"))
# Statements the user uploads (PDFs), kept for View Source. Owner-only folder.
STATEMENTS_DIR = Path(os.getenv("PA_STATEMENTS_DIR", PRIVATE_DIR / "statements"))
MAX_UPLOAD_MB = int(os.getenv("PA_MAX_UPLOAD_MB", "50"))
# Statement text handed to the extractor. A consolidated statement for several
# accounts runs long; past this, the rest is cut and the run says so.
STATEMENT_MAX_CHARS = int(os.getenv("PA_STATEMENT_MAX_CHARS", "80000"))
# Persistent browser profiles, one per institution, so a remembered device stays remembered.
BROWSER_PROFILES_DIR = Path(os.getenv("PA_BROWSER_DIR", PRIVATE_DIR / "browser"))

# Key that encrypts stored logins and PDF passwords. Owner-only file, not Keychain (spec D18).
KEY_PATH = Path(os.getenv("PA_KEY_PATH", Path.home() / ".portfolio-agent" / "secret.key"))

# Kill switch: while this file exists, no fetcher runs (spec §9.2).
KILL_FILE = Path(os.getenv("PA_KILL_FILE", PRIVATE_DIR / "STOP"))

# Hugging Face model used by the agents. On a 128GB Mac:
#   Qwen/Qwen2.5-14B-Instruct  ~30GB, good speed/quality balance (default)
#   Qwen/Qwen2.5-32B-Instruct  ~65GB, best JSON/format following, slower
#   Qwen/Qwen2.5-7B-Instruct   ~15GB, fastest
MODEL_ID = os.getenv("PA_MODEL_ID", "Qwen/Qwen2.5-14B-Instruct")
MAX_NEW_TOKENS = int(os.getenv("PA_MAX_NEW_TOKENS", "8192"))   # room for a statement with ~100 holdings

# Q&A ReAct loop. The spec drops the Q&A step budget in favor of a 30 s limit
# (C7, D13); Q&A arrives in Stage 12, so the old cap stays until then.
STEP_BUDGET = int(os.getenv("PA_STEP_BUDGET", "8"))

# Portal fetcher limits (spec §9.2).
FETCH_STEP_BUDGET = int(os.getenv("PA_FETCH_STEPS", "25"))         # LLM navigation steps
FETCH_TIMEOUT_S = int(os.getenv("PA_FETCH_TIMEOUT", "300"))        # placeholder, spec D10
LOGIN_WAIT_S = int(os.getenv("PA_LOGIN_WAIT", "60"))     # after pressing sign-in, for the next page
MAX_LOGINS_PER_NIGHT = int(os.getenv("PA_MAX_LOGINS", "2"))        # per portal, nightly run
MAX_PARALLEL_RUNS = int(os.getenv("PA_MAX_PARALLEL", "3"))          # refreshes at once (spec §4)
TWOFA_WAIT_S = int(os.getenv("PA_2FA_WAIT", "300"))                # wait for the user's code
CAPTCHA_WAIT_S = int(os.getenv("PA_CAPTCHA_WAIT", "300"))          # wait for you to solve one
# Google refuses sign-in from the testing build of Chrome, so "Sign in with Google" logins
# use the installed Google Chrome, with a profile of their own.
# Built once from portfolio_agent/ocr/pdf_ocr.swift: reads image-only PDFs on this Mac.
OCR_BIN = Path(os.getenv("PA_OCR_BIN", Path.home() / ".portfolio-agent" / "bin" / "pdf_ocr"))
OCR_TIMEOUT_S = int(os.getenv("PA_OCR_TIMEOUT", "180"))
GOOGLE_CHROME_APP = Path(os.getenv("PA_GOOGLE_CHROME",
                                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
# Emailed verification codes, read from Gmail over IMAP when the owner sets it up (S19).
IMAP_HOST = os.getenv("PA_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("PA_IMAP_PORT", "993"))
EMAIL_WAIT_S = int(os.getenv("PA_EMAIL_WAIT", "180"))
HEADLESS = os.getenv("PA_HEADLESS", "0") == "1"                    # portals often block headless
# SMS 2FA: the Messages database, read-only, and how long to wait for the text.
MESSAGES_DB = Path(os.getenv("PA_MESSAGES_DB", Path.home() / "Library" / "Messages" / "chat.db"))
SMS_WAIT_S = int(os.getenv("PA_SMS_WAIT", "120"))

# Account details pages (opened when a login's "open each account's details page" is on):
# at most this many per fetch, and only the top of each (name, number, balance) goes to
# the extractor. The whole page, transactions included, is kept as evidence.
MAX_DETAIL_PAGES = int(os.getenv("PA_MAX_DETAIL_PAGES", "20"))
DETAIL_PAGE_CHARS = int(os.getenv("PA_DETAIL_PAGE_CHARS", "4000"))

# Page text handed to the extractor. Long portal pages list some accounts far down;
# cutting them off loses those accounts, so the limit is generous.
PAGE_MAX_CHARS = int(os.getenv("PA_PAGE_MAX_CHARS", "80000"))

# Validator tolerances.
SUM_TOLERANCE = float(os.getenv("PA_SUM_TOLERANCE", "0.01"))     # 1% of total
VALUE_TOLERANCE = float(os.getenv("PA_VALUE_TOLERANCE", "0.02"))  # qty*price vs value
LARGE_MOVE = float(os.getenv("PA_LARGE_MOVE", "0.25"))           # hold >25% unexplained moves
FUTURE_SLACK_MIN = 10
# A live portal page shows current values: its as-of must be within this many days of
# when the page was captured (covers weekends and market holidays).
PORTAL_MAX_AGE_DAYS = int(os.getenv("PA_PORTAL_MAX_AGE_DAYS", "5"))   # clock skew allowed before an as-of counts as "in the future"

# Staleness: nightly accounts need a value retrieved within this many hours.
STALE_HOURS_NIGHTLY = int(os.getenv("PA_STALE_HOURS", "30"))
STALE_DAYS_MONTHLY = int(os.getenv("PA_STALE_DAYS_MONTHLY", "45"))   # monthly statements
# Quarterly capital statements arrive 2-4 months after the quarter ends, so one is
# expected about every 3 months: stale after 4 months without a new one (spec D10).
STALE_DAYS_QUARTERLY = int(os.getenv("PA_STALE_DAYS_QUARTERLY", "120"))
MAX_DOCUMENTS = int(os.getenv("PA_MAX_DOCUMENTS", "40"))   # statements downloaded per run

# Dashboard server: localhost only.
HOST = "127.0.0.1"
PORT = int(os.getenv("PA_PORT", "8765"))

# Offline price file used by get_quote until a real market-data tool is wired in.
QUOTES_FILE = Path(os.getenv("PA_QUOTES_FILE", DATA_DIR / "quotes.json"))
