import asyncio
from pathlib import Path

import numpy as np
from aiohttp.test_utils import TestClient, TestServer
from pytgcalls.exceptions import TimedOutAnswer

from caller.audio import TG_FRAME_BYTES, Chunker, Playout, Resampler, rate_from_format
from caller.config import Settings
from caller.dialer import Dialer
from caller.server import make_app
from caller.store import Store
from tests.fakes import FakeElevenLabs, FakePhone, tone

TOKEN = "t" * 32


def settings(tmp: Path) -> Settings:
    return Settings(tg_api_id=1, tg_api_hash="h", tg_session="s", owner_tg_id=42, owner_name="Sam",
                    elevenlabs_api_key="el-key", elevenlabs_agent_id="agent-1",
                    caller_token=TOKEN, port=0, ring_seconds=5, max_call_seconds=20,
                    data_dir=tmp)


# ---- audio ------------------------------------------------------------------

def test_resampler_keeps_length_and_pitch():
    up = Resampler(16000, 48000)
    src = tone(16000, 1000, 440)
    out = b"".join(up(src[i:i + 640]) for i in range(0, len(src), 640))
    samples = np.frombuffer(out, dtype=np.int16).astype(float)
    held_back_ms = (48000 - len(samples)) / 48
    assert 0 <= held_back_ms < 25  # small filter delay only
    spectrum = np.abs(np.fft.rfft(samples[2000:-2000]))
    peak_hz = np.argmax(spectrum) * 48000 / len(samples[2000:-2000])
    assert abs(peak_hz - 440) < 5


def test_playout_frames_and_interrupt():
    p = Playout()
    assert p.next_frame() == Playout.SILENCE
    p.push(b"\x01" * (TG_FRAME_BYTES * 2 + 10))
    assert p.pending_ms == 20
    assert p.next_frame() == b"\x01" * TG_FRAME_BYTES
    p.clear()
    assert p.next_frame() == Playout.SILENCE


def test_chunker():
    c = Chunker(100)
    assert c.push(b"x" * 60) == []
    assert c.push(b"x" * 150) == [b"x" * 100, b"x" * 100]


def test_rate_from_format():
    assert rate_from_format("pcm_48000") == 48000


# ---- full call through the dialer --------------------------------------------

async def _with_fake_el(fake, fn):
    server = TestServer(fake.app)
    await server.start_server()
    fake.base = str(server.make_url("")).rstrip("/")
    try:
        return await fn(fake.base)
    finally:
        await server.close()


async def _wait_status(store, call_id, statuses, seconds=15):
    for _ in range(int(seconds / 0.05)):
        if store.get(call_id).status in statuses:
            return store.get(call_id)
        await asyncio.sleep(0.05)
    raise AssertionError(f"stuck at {store.get(call_id).status}")


def test_answered_call_end_to_end(tmp_path):
    fake = FakeElevenLabs()

    async def go(base):
        store = Store(tmp_path)
        phone = FakePhone()
        dialer = Dialer(settings(tmp_path), phone, store)
        dialer.api_base = base
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="Build done", question="Ship?", session="ring-me")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"answered", "failed", "missed"})
        task.cancel()
        return call, phone

    call, phone = asyncio.run(_with_fake_el(fake, go))
    assert call.status == "answered", call.error
    assert call.answer == "Ship it."
    assert "ship" in call.summary.lower()
    assert [t["role"] for t in call.transcript] == ["claude", "owner", "claude"]
    assert fake.got_pong
    assert fake.audio_bytes_in > 0
    vars_ = fake.init_payload["dynamic_variables"]
    assert vars_["reason"] == "Build done" and vars_["question"] == "Ship?" and vars_["details"] == "none"
    assert phone.hung_up and phone.frames_out > 0
    assert call.first_word_ms is not None
    # saved to disk and reloadable
    assert Store(tmp_path).get(call.id).status == "answered"


def test_owner_hangs_up(tmp_path):
    fake = FakeElevenLabs(end_by_agent=False)

    async def go(base):
        store = Store(tmp_path)
        phone = FakePhone()
        dialer = Dialer(settings(tmp_path), phone, store)
        dialer.api_base = base
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x")
        dialer.submit(call)
        await _wait_status(store, call.id, {"talking"})
        await asyncio.sleep(1.0)
        phone.on_hangup()
        call = await _wait_status(store, call.id, {"answered", "failed"})
        task.cancel()
        return call

    call = asyncio.run(_with_fake_el(fake, go))
    assert call.status == "answered"
    assert fake.closed_by_client


def test_missed_call_then_text_reply(tmp_path):
    async def go():
        store = Store(tmp_path)
        phone = FakePhone(ring_error=TimedOutAnswer())
        dialer = Dialer(settings(tmp_path), phone, store)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="Need a decision", question="A or B?")
        dialer.submit(call)
        await _wait_status(store, call.id, {"missed"})
        assert "Need a decision" in phone.texts[0] and "A or B?" in phone.texts[0]
        await phone.on_text("B", 101)  # replied to the missed-call message
        call = await _wait_status(store, call.id, {"replied"})
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert call.answer == "B" and phone.rings == 1


def test_text_mode_does_not_ring(tmp_path):
    async def go():
        store = Store(tmp_path)
        phone = FakePhone()
        dialer = Dialer(settings(tmp_path), phone, store)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="FYI export done", mode="text")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"sent"})
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert phone.rings == 0 and len(phone.texts) == 1


# ---- HTTP API ---------------------------------------------------------------

class NullDialer:
    current = None

    def __init__(self):
        self.submitted = []

    def submit(self, call):
        self.submitted.append(call.id)

    def cancel(self, call):
        call.status = "cancelled"


def test_api_auth_validation_and_flow(tmp_path):
    async def go():
        store = Store(tmp_path)
        dialer = NullDialer()
        client = TestClient(TestServer(make_app(TOKEN, store, dialer)))
        await client.start_server()
        try:
            h = {"Authorization": f"Bearer {TOKEN}"}
            assert (await client.get("/health")).status == 200
            assert (await client.post("/calls", json={"reason": "x"})).status == 401
            bad = {"Authorization": "Bearer " + "u" * 32}
            assert (await client.post("/calls", json={"reason": "x"}, headers=bad)).status == 401
            assert (await client.post("/calls", json={}, headers=h)).status == 400
            assert (await client.post("/calls", json={"reason": "x" * 301}, headers=h)).status == 400
            assert (await client.post("/calls", json={"reason": "x", "mode": "fax"}, headers=h)).status == 400
            assert (await client.post("/calls", json=["x"], headers=h)).status == 400
            r = await client.post("/calls", json={"reason": "Deploy failed", "urgent": True}, headers=h)
            assert r.status == 201
            cid = (await r.json())["id"]
            assert dialer.submitted == [cid]
            body = await (await client.get(f"/calls/{cid}", headers=h)).json()
            assert body["urgent"] is True and body["status"] == "queued" and "message_ids" not in body
            assert (await client.get("/calls/nope", headers=h)).status == 404
            body = await (await client.post(f"/calls/{cid}/cancel", headers=h)).json()
            assert body["status"] == "cancelled" and body["final"] is True
        finally:
            await client.close()

    asyncio.run(go())
