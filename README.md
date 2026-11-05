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
pytest -q                     # 15 tests, uses a fake model, no download needed
code .
```

In VS Code: Cmd+Shift+P → "Python: Select Interpreter" → `.venv`. Install the
Python and Jupyter extensions.

## Choosing the model

Set `PA_MODEL_ID` (default `Qwen/Qwen2.5-14B-Instruct`). With 128GB of memory:

| Model | Memory (bf16) | Use |
|---|---|---|
| `Qwen/Qwen2.5-7B-Instruct` | ~15GB | fast prompt iteration |
| `Qwen/Qwen2.5-14B-Instruct` | ~30GB | default balance |
| `Qwen/Qwen2.5-32B-Instruct` | ~65GB | most reliable JSON / ReAct format |

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
