"""oc-memory — CLI tool for manual summarization and vector retrieval."""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings
from .memory_manager import MemoryManager, Message
from .summarizer import Summarizer


def cmd_summarize(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    summarizer = Summarizer(settings)

    messages = []
    if args.file:
        with open(args.file) as f:
            raw = json.load(f)
        messages = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in raw]
    elif args.text:
        messages = [{"role": "user", "content": args.text}]
    else:
        print("Provide --file or --text", file=sys.stderr)
        sys.exit(1)

    result = summarizer.summarize(messages)
    print(f"Source: {result.source}")
    print(f"Tokens: {result.tokens}")
    print(f"---\n{result.text}")


def cmd_retrieve(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    mm = MemoryManager(settings)
    results = mm.retrieve_relevant(args.q, top_k=args.k)
    if not results:
        print("No results found.")
        return
    for i, text in enumerate(results, 1):
        print(f"\n--- Result {i} ---")
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="oc-memory", description="OpenClaw Memory CLI")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command")

    p_sum = sub.add_parser("summarize", help="Summarize conversation history")
    p_sum.add_argument("--file", help="JSON file with messages array")
    p_sum.add_argument("--text", help="Plain text to summarize")
    p_sum.add_argument("--conversation-id", dest="cid", help="Conversation ID (for logging)")

    p_ret = sub.add_parser("retrieve", help="Retrieve relevant memories from Qdrant")
    p_ret.add_argument("--q", required=True, help="Search query")
    p_ret.add_argument("--k", type=int, default=3, help="Number of results")

    args = parser.parse_args()
    if args.command == "summarize":
        cmd_summarize(args)
    elif args.command == "retrieve":
        cmd_retrieve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
