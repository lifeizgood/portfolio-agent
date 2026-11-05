"""Settings, read from environment variables so nothing is hard-coded."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# SQLite file holding the portfolio store (snapshots, review queue, playbooks).
DB_PATH = Path(os.getenv("PA_DB_PATH", DATA_DIR / "portfolio.db"))

# Hugging Face model used by the agents. On a 128GB Mac:
#   Qwen/Qwen2.5-14B-Instruct  ~30GB, good speed/quality balance (default)
#   Qwen/Qwen2.5-32B-Instruct  ~65GB, best JSON/format following, slower
#   Qwen/Qwen2.5-7B-Instruct   ~15GB, fast for iterating on prompts
MODEL_ID = os.getenv("PA_MODEL_ID", "Qwen/Qwen2.5-14B-Instruct")
MAX_NEW_TOKENS = int(os.getenv("PA_MAX_NEW_TOKENS", "1024"))

# ReAct guardrail: maximum Thought/Action cycles before the agent must stop.
STEP_BUDGET = int(os.getenv("PA_STEP_BUDGET", "8"))

# Validator tolerances.
SUM_TOLERANCE = float(os.getenv("PA_SUM_TOLERANCE", "0.01"))     # 1% of total
VALUE_TOLERANCE = float(os.getenv("PA_VALUE_TOLERANCE", "0.02"))  # qty*price vs value
LARGE_MOVE = float(os.getenv("PA_LARGE_MOVE", "0.25"))           # flag >25% moves

# Offline price file used by get_quote until a real market-data tool is wired in.
QUOTES_FILE = Path(os.getenv("PA_QUOTES_FILE", DATA_DIR / "quotes.json"))
