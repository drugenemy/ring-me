"""The owner calls the assistant: answers to waiting sessions + voice notes."""
import asyncio
import base64
import dataclasses
import json

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from pytgcalls.exceptions import CallDeclined

from caller.dialer import Dialer
from caller.store import Store
from tests.fakes import FakePhone, tone
from tests.test_caller import _wait_status, settings


class FakeInbox:
    """ElevenLabs inbox agent: greets, hears him, saves answer 1 and a note, hangs up.
    Also fakes the GitHub contents API."""

    def __init__(self):
        self.init = None
        self.tool_results = {}
        self.github_puts = []
        self.worker_tasks = []
        self.app = web.Application()
        self.app.add_routes([
            web.get("/v1/convai/conversation/get-signed-url", self.signed_url),
            web.get("/ws", self.ws),
            web.put("/repos/{owner}/{repo}/contents/{path:.*}", self.github_put),
            web.get("/repos/{owner}/{repo}/contents/{path:.*}", self.github_get),
            web.post("/tasks", self.worker_task),
            web.post("/sessions", self.worker_task),
        ])
        self.base = ""

    async def signed_url(self, request):
        assert request.query["agent_id"] == "inbox-agent"
        return web.json_response({"signed_url": self.base.replace("http://", "ws://") + "/ws"})

    async def github_put(self, request):
        assert request.headers["Authorization"] == "Bearer gh-token"
        body = await request.json()
        self.github_puts.append((request.match_info["path"], base64.b64decode(body["content"]).decode()))
        return web.json_response({"content": {"path": request.match_info["path"]}}, status=201)

    async def github_get(self, request):
        assert request.headers["Accept"] == "application/vnd.github.raw"
        path = request.match_info["path"]
        text = {"notes/now.md": "Helpdesk A move: Helpdesk B replaces Helpdesk A.",
                "notes/index.md": "- [[projects/day-job]] day job " + "x" * 300}[path]
        return web.Response(text=text)

    async def worker_task(self, request):
        assert request.headers["Authorization"] == "Bearer worker-token"
        self.worker_tasks.append({**(await request.json()), "path": request.path})
        return web.json_response({"id": "t_1", "status": "queued"}, status=201)

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.init = await ws.receive_json()
        await ws.send_json({"type": "conversation_initiation_metadata",
                            "conversation_initiation_metadata_event": {
                                "conversation_id": "conv_in", "agent_output_audio_format": "pcm_16000",
                                "user_input_audio_format": "pcm_16000"}})
        await ws.send_json({"type": "audio", "audio_event": {
            "audio_base_64": base64.b64encode(tone(16000, 200)).decode(), "event_id": 1}})
        heard = 0
        while heard < 16000 * 2 * 0.3:
            msg = await ws.receive()
            if msg.type != WSMsgType.TEXT:
                return ws
            data = json.loads(msg.data)
            if "user_audio_chunk" in data:
                heard += len(base64.b64decode(data["user_audio_chunk"]))
        await ws.send_json({"type": "user_transcript", "user_transcription_event": {
            "user_transcript": "Fix first. And note: call Alex about the Berlin demo on Friday."}})
        for call_id, name, params in [
            ("t1", "record_answer", {"item": 1, "answer": "Fix rounding first, then ship."}),
            ("t2", "record_answer", {"item": 7, "answer": "nonsense"}),
            ("t3", "save_note", {"title": "Call Alex about demo", "note": "- Call Alex on Friday\n- Topic: Berlin demo"}),
            ("t4", "start_task", {"task": "Check the Helpdesk A migration and update the notes"}),
            ("t5", "open_session", {"task": "Rebuild the pricing page"}),
        ]:
            await ws.send_json({"type": "client_tool_call", "client_tool_call": {
                "tool_name": name, "tool_call_id": call_id, "parameters": params,
                "event_id": 2, "expects_response": True}})
            while True:
                data = json.loads((await ws.receive()).data)
                if data.get("type") == "client_tool_result":
                    self.tool_results[call_id] = data
                    break
        await ws.send_json({"type": "agent_response", "agent_response_event": {"agent_response": "Done. Bye."}})
        await ws.close()
        return ws


def test_callback_answers_waiting_session_and_saves_note(tmp_path):
    fake = FakeInbox()

    async def go():
        server = TestServer(fake.app)
        await server.start_server()
        fake.base = str(server.make_url("")).rstrip("/")
        try:
            store = Store(tmp_path)
            phone = FakePhone(ring_error=CallDeclined(42))
            s = dataclasses.replace(settings(tmp_path), elevenlabs_inbound_agent_id="inbox-agent",
                                    github_token="gh-token", notes_repo="sam/notes",
                                    context_files=("notes/now.md", "notes/index.md"),
                                    worker_token="worker-token")
            dialer = Dialer(s, phone, store)
            dialer.api_base = fake.base
            dialer.github_api = fake.base
            dialer.s = dataclasses.replace(dialer.s, worker_url=fake.base)
            task = asyncio.create_task(dialer.run())
            missed = store.new(reason="Checkout build done, 2 tests fail.", question="Ship or fix first?",
                               session="website")
            dialer.submit(missed)
            await _wait_status(store, missed.id, {"missed"})
            phone.ring_error = None
            await phone.on_incoming_call()   # the owner calls back
            missed = await _wait_status(store, missed.id, {"replied"})
            await asyncio.sleep(0.1)
            task.cancel()
            inbound = next(c for c in store.calls.values() if c.mode == "inbound")
            return missed, inbound, phone
        finally:
            await server.close()

    missed, inbound, phone = asyncio.run(go())
    # the waiting session got his spoken answer
    assert missed.status == "replied" and missed.answer == "Fix rounding first, then ship."
    # the agent was briefed on the missed call
    dv = fake.init["dynamic_variables"]
    assert "You missed my call" in dv["greeting"] and "Ship or fix first?" in dv["pending"]
    # bad item number was refused, good ones accepted
    assert fake.tool_results["t1"]["is_error"] is False
    assert fake.tool_results["t2"]["result"] == "Unknown item number."
    # note saved to the repo and texted
    path, content = fake.github_puts[0]
    assert path.startswith("raw/voice-notes/") and path.endswith("-call-alex-about-demo.md")
    assert "Berlin demo" in content and "## Transcript" in content and "Fix rounding first" in content
    texts = "\n".join(phone.texts)
    assert "📝 Call Alex about demo" in texts and "Saved to your notes" in texts
    assert "Passed to the sessions" in texts
    assert inbound.status == "answered" and phone.hung_up
    # notes loaded into the call, index lines shortened
    assert "Helpdesk B replaces Helpdesk A" in dv["context"] and "x" * 200 not in dv["context"]
    assert "=== notes/now.md ===" in dv["context"]
    assert dv["owner_name"] == "Sam" and dv["assistant_name"] == "Jessica" and dv["timezone"] == "UTC"
    # task handed to the worker with what he said on the call
    assert fake.worker_tasks[0]["task"] == "Check the Helpdesk A migration and update the notes"
    assert "Fix first" in fake.worker_tasks[0]["context"]
    assert fake.tool_results["t4"]["is_error"] is False
    assert fake.worker_tasks[0]["path"] == "/tasks" and fake.worker_tasks[1]["path"] == "/sessions"
    assert fake.worker_tasks[1]["task"] == "Rebuild the pricing page"
    assert "Opening a new Claude session" in fake.tool_results["t5"]["result"]
    assert "Hey Sam, it's Jessica." in dv["greeting"]
    assert "## Work handed off" in content and "new session: Rebuild the pricing page" in content
    assert "Handed off" in texts and "new session: Rebuild the pricing page" in texts


def test_incoming_call_while_busy_is_declined(tmp_path):
    async def go():
        phone = FakePhone()
        s = dataclasses.replace(settings(tmp_path), elevenlabs_inbound_agent_id="inbox-agent")
        dialer = Dialer(s, phone, Store(tmp_path))
        async with dialer._line:
            await phone.on_incoming_call()
        return phone

    phone = asyncio.run(go())
    assert phone.hang_ups == 1 and "another call" in phone.texts[0]
    assert getattr(phone, "answers", 0) == 0


def test_everything_lands_in_raw(tmp_path):
    """Outbound answered call, text reply and worker notice are all saved to the notes repo."""
    from tests.fakes import FakeElevenLabs
    el = FakeElevenLabs()
    gh = FakeInbox()

    async def go():
        el_server, gh_server = TestServer(el.app), TestServer(gh.app)
        await el_server.start_server()
        await gh_server.start_server()
        el.base = str(el_server.make_url("")).rstrip("/")
        try:
            store = Store(tmp_path)
            phone = FakePhone()
            s = dataclasses.replace(settings(tmp_path), github_token="gh-token", notes_repo="sam/notes")
            dialer = Dialer(s, phone, store)
            dialer.api_base = el.base
            dialer.github_api = str(gh_server.make_url("")).rstrip("/")
            dialer.analysis_wait = 1
            task = asyncio.create_task(dialer.run())
            out = store.new(reason="Build done", question="Ship?", session="website")
            dialer.submit(out)
            await _wait_status(store, out.id, {"answered"})
            note = store.new(reason="✅ Worker finished: check Helpdesk A", details="Helpdesk B is live.",
                             mode="notice", session="worker")
            dialer.submit(note)
            await _wait_status(store, note.id, {"delivered"})
            ask = store.new(reason="Pick a name", question="A or B?", mode="text")
            dialer.submit(ask)
            await _wait_status(store, ask.id, {"sent"})
            await phone.on_text("B", None)
            for _ in range(100):
                if len(gh.github_puts) >= 3:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            return gh.github_puts
        finally:
            await el_server.close()
            await gh_server.close()

    puts = asyncio.run(go())
    by_type = {c.split("type: ")[1].split("\n")[0]: (p, c) for p, c in puts}
    assert set(by_type) == {"call-out", "worker", "text-reply"}
    assert all(p.startswith("raw/voice-notes/") for p, _ in puts)
    assert "Ship it." in by_type["call-out"][1] and "**Jessica:**" in by_type["call-out"][1]
    assert "Helpdesk B is live." in by_type["worker"][1]
    assert "## The reply" in by_type["text-reply"][1] and "B" in by_type["text-reply"][1]
