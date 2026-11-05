"""Read-only tools for the Q&A agent.

There is deliberately no tool that trades, transfers or changes settings, so no
chain of reasoning can lead to one. Later, each group of tools can move behind
an MCP server without changing the agent: it only sees names, descriptions and
results.
"""

import ast
import json
import operator
from dataclasses import dataclass
from typing import Callable

from . import config, store

MAX_OBSERVATION_CHARS = 4000


@dataclass
class Tool:
    name: str
    description: str   # shown to the model, including the expected input
    fn: Callable[[dict], object]


def _quotes() -> dict:
    if config.QUOTES_FILE.exists():
        return json.loads(config.QUOTES_FILE.read_text())
    return {}


def _get_quote(args: dict):
    symbol = str(args.get("symbol", "")).upper()
    q = _quotes().get(symbol)
    if not q:
        return {"symbol": symbol, "error": "no quote available"}
    return {"symbol": symbol, **q}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos}


def safe_eval(expr: str) -> float:
    """Arithmetic only: numbers, + - * / ** and parentheses. No names, no calls."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("Only arithmetic is allowed.")
    return ev(ast.parse(expr.replace(",", ""), mode="eval"))


def build_tools(conn) -> dict[str, Tool]:
    tools = [
        Tool("current_positions",
             'Latest known holdings and total for every account, with as_of_date and '
             'retrieved_at. Input: {}',
             lambda a: store.current_positions(conn)),
        Tool("value_history",
             'Account totals over time by as-of date. Input: {"account": "name"} or {} for all',
             lambda a: store.history(conn, a.get("account"))),
        Tool("get_quote",
             'Latest price for a public security. Input: {"symbol": "VTI"}',
             _get_quote),
        Tool("calculate",
             'Exact arithmetic; use this instead of doing math yourself. '
             'Input: {"expression": "(105000 - 98000) / 98000"}',
             lambda a: safe_eval(str(a.get("expression", "")))),
        Tool("review_queue",
             'Open items waiting for human review (values the validator rejected). Input: {}',
             lambda a: store.open_review_items(conn)),
    ]
    return {t.name: t for t in tools}


def run_tool(tools: dict[str, Tool], name: str, args: dict) -> str:
    if name not in tools:
        return f"Error: unknown tool '{name}'. Available: {', '.join(tools)}"
    try:
        result = tools[name].fn(args if isinstance(args, dict) else {})
    except Exception as e:  # tool errors become observations the agent can react to
        return f"Error: {type(e).__name__}: {e}"
    text = json.dumps(result, default=str)
    return text if len(text) <= MAX_OBSERVATION_CHARS else text[:MAX_OBSERVATION_CHARS] + "...(truncated)"
