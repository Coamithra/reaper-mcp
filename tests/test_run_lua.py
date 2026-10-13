"""run_lua: arbitrary ReaScript through the bridge, and the gate that keeps it opt-in.

The bridge half runs the whole real ``reaper_mcp_bridge.lua`` under lupa and sends
``RunLua`` requests through the mailbox, so what is asserted is what REAPER would answer.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import reaper_mcp_server as srv

lupa = pytest.importorskip("lupa")

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "reaper_mcp_bridge.lua"

STUB = """
ext_state = {}
clock = 1000
rates = {}
reaper = {
    GetResourcePath = function() return RESOURCE end,
    RecursiveCreateDirectory = function() return 1 end,
    ShowConsoleMsg = function() end,
    defer = function() end,
    GetExtState = function(section, key) return (ext_state[section .. "/" .. key]) or "" end,
    SetExtState = function(section, key, value, persist) ext_state[section .. "/" .. key] = value end,
    DeleteExtState = function(section, key, persist) ext_state[section .. "/" .. key] = nil end,
    time_precise = function() clock = clock + 0.01; return clock end,
    atexit = function() end,
    EnumerateFiles = function(dir, i) return list_dir(dir, i) end,
    Undo_BeginBlock = function() end,
    Undo_EndBlock = function() end,
    UpdateTimeline = function() end,
    GetMediaItem = function(_, i) if i == 0 then return "item0" end end,
    GetActiveTake = function(item) return item .. "/take" end,
    SetMediaItemTakeInfo_Value = function(take, key, value) rates[take .. ":" .. key] = value; return true end,
    GetMediaItemTakeInfo_Value = function(take, key) return rates[take .. ":" .. key] or 1.0 end,
}
"""


@pytest.fixture
def bridge(tmp_path):
    if not str(tmp_path).isascii():
        pytest.skip("Lua io.open decodes filenames in the ANSI code page; needs an ASCII path")
    requests = tmp_path / "Scripts" / "mcp_bridge_data"
    requests.mkdir(parents=True)
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)

    def list_dir(directory, index):
        if index < 0:
            return None
        names = sorted(os.listdir(requests))
        return names[index] if index < len(names) else None

    lua.globals().list_dir = list_dir
    lua.globals().RESOURCE = str(tmp_path).replace("\\", "/")
    lua.execute(STUB)
    lua.execute(BRIDGE.read_text(encoding="utf-8"))

    def send(code):
        (requests / "request_1.json").write_text(
            json.dumps({"func": "RunLua", "args": [code], "id": "x"}), encoding="utf-8")
        lua.globals().main()
        answer = requests / "response_1.json"
        response = json.loads(answer.read_text(encoding="utf-8"))
        answer.unlink()
        return response

    send.lua = lua
    return send


def test_a_returned_value_comes_back_as_the_result(bridge):
    response = bridge("return 6 * 7")
    assert response["ok"] is True
    assert response["result"] == 42


def test_several_returned_values_come_back_as_a_list(bridge):
    response = bridge('return 1, "two", true')
    assert response["result"] == [1, "two", True]


def test_returning_nothing_leaves_the_result_absent(bridge):
    response = bridge("local x = 1")
    assert response["ok"] is True
    assert "result" not in response


def test_the_snippet_reaches_the_api_and_looks_up_its_own_objects(bridge):
    """The case the tool exists for: a setter that wants a take pointer, which no index-based
    request can carry. The snippet resolves it itself and only the number crosses back."""
    response = bridge(
        'local take = reaper.GetActiveTake(reaper.GetMediaItem(0, 0))\n'
        'reaper.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", 1.0158)\n'
        'return reaper.GetMediaItemTakeInfo_Value(take, "D_PLAYRATE")')
    assert response["ok"] is True
    assert response["result"] == pytest.approx(1.0158)
    assert bridge.lua.eval('rates["item0/take:D_PLAYRATE"]') == pytest.approx(1.0158)


def test_print_is_captured_rather_than_sent_to_the_console(bridge):
    response = bridge('print("a", 1)\nprint("b")\nreturn true')
    assert response["output"] == ["a\t1", "b"]


def test_no_print_is_an_empty_list_not_an_object(bridge):
    assert bridge("return 1")["output"] == []


def test_a_compile_error_is_reported_and_nothing_runs(bridge):
    response = bridge("return (")
    assert response["ok"] is False
    assert response["error"].startswith("compile error:")


def test_a_runtime_error_is_reported_with_the_output_so_far(bridge):
    response = bridge('print("before")\nerror("boom")')
    assert response["ok"] is False
    assert response["error"].startswith("runtime error:")
    assert "boom" in response["error"]
    assert response["output"] == ["before"]


def test_a_snippets_globals_never_reach_the_bridges(bridge):
    """A snippet assigning a global must not overwrite anything the bridge itself runs on."""
    response = bridge("DSL_FUNCTIONS = nil\nleaked = 1\nreturn true")
    assert response["ok"] is True
    assert bridge.lua.eval("leaked") is None
    assert bridge.lua.eval("DSL_FUNCTIONS") is not None
    # and the bridge still answers the next request
    assert bridge("return 1")["result"] == 1


def test_empty_code_is_refused(bridge):
    response = bridge("")
    assert response["ok"] is False
    assert "non-empty" in response["error"]


def test_the_tool_sends_the_code_to_the_bridge(reaper):
    asyncio.run(srv.run_lua("return 1"))
    assert reaper.last == ("RunLua", ["return 1"])


def test_a_label_names_the_undo_step_and_is_put_back_after(monkeypatch):
    seen = []

    async def fake_call(func, *args, timeout=None):
        seen.append(srv.undo_label.get())
        return {"ok": True}

    monkeypatch.setattr(srv, "reaper_call", fake_call)
    asyncio.run(srv.run_lua("return 1", label="set jaguar rate"))
    asyncio.run(srv.run_lua("return 1"))
    assert seen[0] == "MCP: set jaguar rate"
    assert seen[1] != "MCP: set jaguar rate"
    assert srv.undo_label.get() is None


def tool_names_with(env_value):
    env = {k: v for k, v in os.environ.items() if k != "REAPER_MCP_ALLOW_LUA"}
    if env_value is not None:
        env["REAPER_MCP_ALLOW_LUA"] = env_value
    out = subprocess.run(
        [sys.executable, "-c",
         "import reaper_mcp_server as s; print(','.join(t.name for t in s.mcp._tool_manager.list_tools()))"],
        cwd=REPO, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip().split(",")


def test_the_tool_is_absent_unless_asked_for():
    assert "run_lua" not in tool_names_with(None)
    assert "run_lua" not in tool_names_with("yes")


def test_the_tool_is_offered_when_enabled():
    assert "run_lua" in tool_names_with("1")
