"""Run a Lua snippet in REAPER through the bridge, from the command line.

The same call the run_lua tool makes, for when no MCP session has the tool (a client that
connected before the bridge or the server was updated):

    python scripts/run_lua.py "return reaper.CountTracks(0)"
    python scripts/run_lua.py --label "set rate" --file snippet.lua
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper_mcp_server as srv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("code", nargs="?", help="Lua source (or use --file)")
    parser.add_argument("--file", type=Path, help="read the Lua source from a file")
    parser.add_argument("--label", default="run lua", help="undo step name")
    args = parser.parse_args()
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    if not code:
        parser.error("give the code or --file")
    token = srv.undo_label.set(srv.undo_step_label(args.label))
    try:
        response = asyncio.run(srv.run_lua(code))
    finally:
        srv.undo_label.reset(token)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
