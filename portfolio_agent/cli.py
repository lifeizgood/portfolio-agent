"""Command line entry point.

    python -m portfolio_agent.cli ingest data/samples/brokerage_2026-09-30.txt
    python -m portfolio_agent.cli ask "What is my total net worth and how current is it?" --trace
    python -m portfolio_agent.cli positions | history | review
"""

import argparse
import json

from . import config, store


def main(argv=None):
    p = argparse.ArgumentParser(prog="portfolio-agent")
    p.add_argument("--db", default=None, help=f"SQLite path (default {config.DB_PATH})")
    p.add_argument("--model", default=None, help=f"HF model id (default {config.MODEL_ID})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="extract, validate and store one statement")
    s.add_argument("paths", nargs="+")
    s.add_argument("--institution", help="institution name, to load its playbook")

    s = sub.add_parser("ask", help="ask the ReAct Q&A agent a question")
    s.add_argument("question")
    s.add_argument("--trace", action="store_true", help="print thoughts, actions, observations")

    sub.add_parser("positions", help="latest snapshot per account")
    s = sub.add_parser("history", help="totals over time")
    s.add_argument("--account")
    sub.add_parser("review", help="open review-queue items")

    s = sub.add_parser("playbook", help="save notes for an institution (procedural memory)")
    s.add_argument("institution")
    s.add_argument("notes")

    args = p.parse_args(argv)
    conn = store.connect(args.db)

    if args.cmd == "ingest":
        from .ingest_graph import build_ingest_graph
        from .llm import load_chat_model

        graph = build_ingest_graph(load_chat_model(args.model), conn)
        for path in args.paths:
            out = graph.invoke({"path": path, "institution": args.institution})
            if out["outcome"] == "stored":
                print(f"STORED  {path}  (snapshot {out['snapshot_id']}, "
                      f"{out['attempts']} attempt(s))")
                for w in out.get("warnings", []):
                    print(f"  warning: {w}")
            else:
                print(f"REVIEW  {path}  (item {out['review_id']})")
                for e in out["errors"]:
                    print(f"  - {e}")

    elif args.cmd == "ask":
        from .llm import load_chat_model
        from .react_graph import build_react_graph, format_trace

        graph = build_react_graph(load_chat_model(args.model), conn)
        out = graph.invoke({"question": args.question},
                           {"recursion_limit": config.STEP_BUDGET * 2 + 10})
        if args.trace:
            print(format_trace(out["trace"]), "\n")
        print(out["answer"])

    elif args.cmd == "positions":
        print(json.dumps(store.current_positions(conn), indent=2))
    elif args.cmd == "history":
        print(json.dumps(store.history(conn, args.account), indent=2))
    elif args.cmd == "review":
        print(json.dumps(store.open_review_items(conn), indent=2))
    elif args.cmd == "playbook":
        store.save_playbook(conn, args.institution, args.notes)
        print(f"Saved playbook for {args.institution}.")


if __name__ == "__main__":
    main()
