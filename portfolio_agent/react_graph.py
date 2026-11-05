"""Q&A agent: an explicit ReAct loop built in LangGraph.

Local Hugging Face pipelines don't support LangChain's native tool calling
reliably, so the model writes each step as JSON:

    {"thought": "...", "action": "tool_name", "action_input": {...}}
    {"thought": "...", "action": "finish", "action_input": {"answer": "..."}}

    think --tool--> act --observation--> think ... --finish--> END
      |                                      (step budget spent) --> give_up

Working memory (short-term) is the message list: every Thought, Action and
Observation of this run. Long-term memory is the portfolio store, reached only
through tools.
"""

import json
from typing import TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from . import config
from .jsonutil import extract_json
from .tools import Tool, build_tools, run_tool

SYSTEM = """You answer questions about the user's investment portfolio.
You must ground every number in tool results. Never guess a value.
Each reply is ONE JSON object and nothing else:
{{"thought": "what you know and what to do next", "action": "<tool name or finish>", "action_input": {{...}}}}
To end: {{"thought": "...", "action": "finish", "action_input": {{"answer": "final answer for the user"}}}}
In the final answer, say how current each number is (as-of date) and mark values
that are older than a quarter as stale.

Tools:
{tools}"""


class ReactState(TypedDict, total=False):
    question: str
    messages: list[BaseMessage]
    pending: dict | None   # the action chosen by the last thought
    steps: int
    answer: str | None
    trace: list[dict]      # thought/action/observation log, for debugging and reports


def build_react_graph(model, conn, tools: dict[str, Tool] | None = None,
                      budget: int | None = None):
    tools = tools or build_tools(conn)
    budget = budget or config.STEP_BUDGET
    system = SystemMessage(SYSTEM.format(
        tools="\n".join(f"- {t.name}: {t.description}" for t in tools.values())))

    def start(state: ReactState) -> ReactState:
        return {"messages": [HumanMessage(state["question"])], "steps": 0,
                "trace": [], "answer": None, "pending": None}

    def think(state: ReactState) -> ReactState:
        reply = model.invoke([system, *state["messages"]]).content
        msgs = [*state["messages"], AIMessage(reply)]
        steps = state["steps"] + 1
        try:
            step = extract_json(reply)
            action = str(step["action"])
            args = step.get("action_input") or {}
        except (ValueError, KeyError, TypeError) as e:
            msgs.append(HumanMessage(f"Observation: your reply was not valid JSON with "
                                     f"'thought', 'action', 'action_input' ({e}). Reply again."))
            trace = [*state["trace"], {"step": steps, "error": str(e), "raw": reply}]
            return {"messages": msgs, "steps": steps, "pending": None, "trace": trace}

        trace = [*state["trace"], {"step": steps, "thought": step.get("thought"),
                                   "action": action, "action_input": args}]
        if action == "finish":
            answer = args.get("answer") if isinstance(args, dict) else str(args)
            return {"messages": msgs, "steps": steps, "pending": None,
                    "answer": answer or "", "trace": trace}
        return {"messages": msgs, "steps": steps, "trace": trace,
                "pending": {"action": action, "args": args}}

    def act(state: ReactState) -> ReactState:
        p = state["pending"]
        observation = run_tool(tools, p["action"], p["args"])
        trace = list(state["trace"])
        trace[-1] = {**trace[-1], "observation": observation}
        msgs = [*state["messages"], HumanMessage(f"Observation: {observation}")]
        return {"messages": msgs, "pending": None, "trace": trace}

    def give_up(state: ReactState) -> ReactState:
        return {"answer": f"I couldn't answer reliably within {budget} steps. "
                          "See the trace for what I found so far."}

    def route(state: ReactState) -> str:
        if state.get("answer") is not None:
            return END
        if state["steps"] >= budget:
            return "give_up"
        return "act" if state.get("pending") else "think"

    g = StateGraph(ReactState)
    g.add_node("start", start)
    g.add_node("think", think)
    g.add_node("act", act)
    g.add_node("give_up", give_up)
    g.add_edge(START, "start")
    g.add_edge("start", "think")
    g.add_conditional_edges("think", route, ["act", "think", "give_up", END])
    g.add_edge("act", "think")
    g.add_edge("give_up", END)
    return g.compile()


def format_trace(trace: list[dict]) -> str:
    lines = []
    for t in trace:
        if "error" in t:
            lines.append(f"[{t['step']}] invalid reply: {t['error']}")
            continue
        lines.append(f"[{t['step']}] Thought: {t.get('thought')}")
        lines.append(f"    Action: {t['action']} {json.dumps(t.get('action_input'))}")
        if "observation" in t:
            obs = t["observation"]
            lines.append(f"    Observation: {obs[:300]}{'...' if len(obs) > 300 else ''}")
    return "\n".join(lines)
