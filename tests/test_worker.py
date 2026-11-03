"""Worker: clone, run claude (faked), text the summary through the caller."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from worker.app import BASE_TOOLS, Settings, allowed_tools, Worker, clean_token, make_app, parse_cloud_output, token_shape

FAKE_CLAUDE = """#!{python}
import json, os, sys, pathlib
args = sys.argv[1:]
pathlib.Path("claude-args.json").write_text(json.dumps(args))
pathlib.Path("seen-env.json").write_text(json.dumps({{k: os.environ.get(k) for k in ("CALLER_URL", "CALLER_TOKEN", "WORKER_TOKEN", "CALLER_SESSION_LABEL")}}))
pathlib.Path("did-work.txt").write_text("yes")
mode = os.environ.get("FAKE_MODE", "ok")
if "--cloud" in args:
    if mode == "cloud-denied":
        print(json.dumps({{"ok": False, "error": "Cloud sessions need a claude.ai sign-in."}}))
        sys.exit(1)
    print(json.dumps({{"ok": True, "session_id": "session_123", "title": "Rebuild pricing page", "url": "https://claude.ai/code/session_123"}}))
    sys.exit(0)
if mode == "error":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Could not reach the repo."}}))
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "result": "Checked the Helpdesk A move. Helpdesk B is live; 2 macros left.", "total_cost_usd": 0.0, "num_turns": 7}}))
"""


def make_repo(tmp: Path) -> str:
    src = tmp / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "CLAUDE.md").write_text("rules\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
    return str(src)


def run_worker(tmp_path, mode, monkeypatch, path="/tasks"):
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("WORKER_TOKEN", "w" * 30)
    received = []

    async def calls(request):
        assert request.headers["Authorization"] == "Bearer " + "c" * 30
        received.append(await request.json())
        return web.json_response({"id": "c_1", "status": "queued"}, status=201)

    async def go():
        caller = web.Application()
        caller.add_routes([web.post("/calls", calls)])
        server = TestServer(caller)
        await server.start_server()
        s = Settings(worker_token="w" * 30, caller_url=str(server.make_url("")).rstrip("/"),
                     caller_token="c" * 30, repo_url=make_repo(tmp_path), work_dir=tmp_path / "work",
                     timeout=60, claude_bin=str(fake), port=0)
        w = Worker(s)
        task = asyncio.create_task(w.run())
        client = TestClient(TestServer(make_app(w, s.worker_token)))
        await client.start_server()
        try:
            assert (await client.post("/tasks", json={"task": "x"})).status == 401
            h = {"Authorization": "Bearer " + "w" * 30}
            assert (await client.post("/tasks", json={"task": ""}, headers=h)).status == 400
            r = await client.post(path, json={"task": "Check the Helpdesk A migration status",
                                                  "context": "owner: check helpdesk"}, headers=h)
            tid = (await r.json())["id"]
            for _ in range(200):
                body = await (await client.get(f"/tasks/{tid}", headers=h)).json()
                if body["status"] in ("done", "failed") and received:
                    break
                await asyncio.sleep(0.05)
            return body, received, s.work_dir / tid
        finally:
            task.cancel()
            await client.close()
            await server.close()

    return asyncio.run(go())


def test_task_runs_with_fixed_tools_and_texts_summary(tmp_path, monkeypatch):
    body, received, workdir = run_worker(tmp_path, "ok", monkeypatch)
    assert body["status"] == "done", body
    assert "Helpdesk B is live" in body["result"]
    msg = received[0]
    assert msg["mode"] == "notice" and msg["reason"].startswith("✅ Worker finished")
    assert "Helpdesk B is live" in msg["details"]
    assert not workdir.exists()  # cleaned up after success


def test_failed_task_is_reported(tmp_path, monkeypatch):
    body, received, workdir = run_worker(tmp_path, "error", monkeypatch)
    assert body["status"] == "failed" and "Could not reach" in body["error"]
    assert received[0]["reason"].startswith("⚠️ Worker failed")
    args = json.loads((workdir / "claude-args.json").read_text())
    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--allowedTools") + 1] == ",".join(allowed_tools())
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    env = json.loads((workdir / "seen-env.json").read_text())
    assert env["CALLER_URL"] and env["CALLER_TOKEN"] == "c" * 30 and env["WORKER_TOKEN"] is None
    assert env["CALLER_SESSION_LABEL"] == "worker"
    assert "Bash" not in BASE_TOOLS  # never a free shell


def test_tokens_are_cleaned_of_spaces_and_line_breaks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", " fake-abc\ndef \n")
    monkeypatch.setenv("GITHUB_TOKEN", "fake gh")
    s = Settings(worker_token="w" * 30, caller_url="https://c", caller_token="c" * 30,
                 repo_url="r", work_dir=tmp_path, timeout=1, claude_bin="claude", port=0)
    env = Worker(s)._env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-abcdef"
    assert env["GITHUB_TOKEN"] == "fakegh"


def test_clean_token_and_shape_never_leak_the_value():
    assert clean_token(' "fake-abc\ndef" ') == "fake-abcdef"
    shape = token_shape('"sk-ant-oat01-' + "q" * 20 + '"')
    assert "qqqq" not in shape and "had quotes: True" in shape and "starts with sk-ant-oat01-: True" in shape


def test_open_session_texts_the_link(tmp_path, monkeypatch):
    body, received, _ = run_worker(tmp_path, "ok", monkeypatch, path="/sessions")
    assert body["status"] == "done" and body["kind"] == "session"
    assert body["url"] == "https://claude.ai/code/session_123"
    assert received[0]["reason"].startswith("🚀 Session opened")
    assert "https://claude.ai/code/session_123" in received[0]["details"]


def test_open_session_failure_is_reported(tmp_path, monkeypatch):
    body, received, _ = run_worker(tmp_path, "cloud-denied", monkeypatch, path="/sessions")
    assert body["status"] == "failed" and "claude.ai sign-in" in body["error"]
    assert received[0]["reason"].startswith("⚠️ Could not open a session")


def test_parse_cloud_output_text_form():
    out = "Created cloud session: Fix bug\nSession ID: s1\nView: https://claude.ai/code/s1?from=cli\nResume with: claude --teleport s1\n"
    assert parse_cloud_output(out) == ("https://claude.ai/code/s1?from=cli", "Fix bug", "")
