"""Tests for the problems found in the first code review."""
import asyncio
import base64
import dataclasses
import time

from pytgcalls.exceptions import CallDeclined

from caller.dialer import Dialer
from caller.phone import Phone
from caller.store import Store
from caller.voice import VoiceSession, redact
from tests.fakes import FakeElevenLabs, FakePhone, tone
from tests.test_caller import _wait_status, _with_fake_el, settings


def make(tmp, phone, **kw):
    store = Store(tmp)
    dialer = Dialer(dataclasses.replace(settings(tmp), **kw), phone, store)
    dialer.retry_gap = 0.05
    return store, dialer


def test_cancel_while_ringing_stops_ring_and_stays_cancelled(tmp_path):
    async def ring_forever(_):
        await asyncio.sleep(3600)

    async def go():
        phone = FakePhone(ring=ring_forever)
        store, dialer = make(tmp_path, phone)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x", urgent=True)
        dialer.submit(call)
        await _wait_status(store, call.id, {"ringing"})
        await asyncio.sleep(0.1)
        dialer.cancel(call)
        await asyncio.sleep(0.3)
        assert not task.done(), task.exception() if task.done() else None
        task.cancel()
        return store.get(call.id), phone

    call, phone = asyncio.run(go())
    assert call.status == "cancelled"
    assert phone.texts == []          # no "Claude tried to call you" for a cancelled call
    assert phone.hang_ups >= 1        # the ring was ended
    assert phone.rings == 1           # urgent retry did not fire


def test_ring_that_never_connects_times_out(tmp_path):
    async def stuck(_):
        await asyncio.sleep(3600)

    async def go():
        phone = FakePhone(ring=stuck)
        store, dialer = make(tmp_path, phone, ring_seconds=0)
        dialer.connect_grace = 0.3
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"missed", "failed"}, seconds=5)
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert call.status == "missed" and call.error == "TimeoutError"
    assert phone.hang_ups >= 1 and len(phone.texts) == 1


def test_failure_after_connect_hangs_up_and_texts(tmp_path):
    async def broken_record(_):
        raise RuntimeError("record failed")

    async def go():
        phone = FakePhone(ring=broken_record)
        store, dialer = make(tmp_path, phone)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"missed", "failed"})
        await asyncio.sleep(0.05)
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert call.status == "missed" and "record failed" in call.error
    assert phone.hang_ups >= 1


def test_hangup_right_after_pickup_falls_back_to_text(tmp_path):
    async def pick_up_and_drop(phone):
        phone.on_hangup()

    async def go():
        phone = FakePhone(ring=pick_up_and_drop)
        store, dialer = make(tmp_path, phone)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"missed", "answered", "failed"})
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert call.status == "missed"
    assert "cut off" in phone.texts[0]


def test_urgent_rings_twice(tmp_path):
    async def go():
        phone = FakePhone(ring_error=CallDeclined(42))
        store, dialer = make(tmp_path, phone)
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="x", urgent=True)
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"missed"})
        task.cancel()
        return call, phone

    call, phone = asyncio.run(go())
    assert phone.rings == 2 and "URGENT" in phone.texts[0]


def test_analysis_failure_still_answered(tmp_path):
    fake = FakeElevenLabs(analysis_status=500)

    async def go(base):
        phone = FakePhone()
        store, dialer = make(tmp_path, phone)
        dialer.api_base = base
        dialer.analysis_wait = 1
        task = asyncio.create_task(dialer.run())
        call = store.new(reason="Build done")
        dialer.submit(call)
        call = await _wait_status(store, call.id, {"answered", "missed", "failed"})
        task.cancel()
        return call

    call = asyncio.run(_with_fake_el(fake, go))
    assert call.status == "answered"
    assert call.answer == "Yes, ship it."   # raw words when the analysis is unavailable


def test_text_replies_match_the_right_call(tmp_path):
    async def go():
        phone = FakePhone(ring_error=CallDeclined(42))
        store, dialer = make(tmp_path, phone)
        task = asyncio.create_task(dialer.run())
        a = store.new(reason="first")
        dialer.submit(a)
        await _wait_status(store, a.id, {"missed"})
        b = store.new(reason="second")
        dialer.submit(b)
        await _wait_status(store, b.id, {"missed"})
        # Unthreaded message while two sessions wait: ask him to swipe-reply.
        await phone.on_text("yes", None)
        assert store.get(a.id).status == "missed" and store.get(b.id).status == "missed"
        assert "Swipe-reply" in phone.texts[-1]
        # Reply to an unrelated old message: ignored.
        await phone.on_text("random", 9999)
        assert store.get(a.id).status == "missed"
        # Threaded replies go to the right call, and a second message is appended.
        await phone.on_text("A is fine", store.get(a.id).message_ids[0])
        await phone.on_text("and push it", store.get(a.id).message_ids[0])
        # Now only b waits: an unthreaded reply goes there.
        await phone.on_text("B no", None)
        task.cancel()
        return store.get(a.id), store.get(b.id)

    a, b = asyncio.run(go())
    assert a.status == "replied" and a.answer == "A is fine\nand push it"
    assert b.status == "replied" and b.answer == "B no"


def test_incoming_call_gets_a_text(tmp_path):
    async def go():
        phone = FakePhone()
        _, dialer = make(tmp_path, phone)
        await phone.on_incoming_call()
        return phone

    phone = asyncio.run(go())
    assert "can't pick up calls" in phone.texts[0]


def test_restart_recovery(tmp_path):
    store = Store(tmp_path)
    talking = store.new(reason="t", status="talking")
    old = store.new(reason="o", status="queued", created_at=time.time() - 3600)
    fresh = store.new(reason="f", status="ringing")
    _, dialer = make(tmp_path, FakePhone())
    dialer._recover_after_restart()
    s2 = dialer.store
    assert s2.get(talking.id).status == "failed"
    assert s2.get(old.id).status == "failed"
    assert s2.get(fresh.id).status == "queued" and dialer.queue.qsize() == 1


def test_late_audio_after_interruption_is_dropped():
    async def go():
        v = VoiceSession("k", "a", {})
        await v._handle({"type": "conversation_initiation_metadata",
                         "conversation_initiation_metadata_event": {
                             "conversation_id": "c", "agent_output_audio_format": "pcm_16000",
                             "user_input_audio_format": "pcm_16000"}})
        audio = base64.b64encode(tone(16000, 100)).decode()
        await v._handle({"type": "audio", "audio_event": {"audio_base_64": audio, "event_id": 3}})
        assert v.playout.pending_ms > 0
        await v._handle({"type": "interruption", "interruption_event": {"event_id": 4}})
        assert v.playout.pending_ms == 0
        await v._handle({"type": "audio", "audio_event": {"audio_base_64": audio, "event_id": 4}})
        assert v.playout.pending_ms == 0      # late chunk of the interrupted reply
        await v._handle({"type": "audio", "audio_event": {"audio_base_64": audio, "event_id": 5}})
        assert v.playout.pending_ms > 0       # the new reply plays

    asyncio.run(go())


def test_redact_signed_url():
    msg = "400, message='Invalid', url='wss://api.elevenlabs.io/v1/convai/conversation?agent_id=a&conversation_signature=SECRET123'"
    assert "SECRET123" not in redact(msg)


def test_long_text_is_cut_and_sent_plain():
    sent = {}

    class Client:
        async def send_message(self, to, text, parse_mode="md"):
            sent.update(to=to, text=text, parse_mode=parse_mode)

            class M:
                id = 1
            return M()

    phone = Phone.__new__(Phone)
    phone.client, phone.owner_id = Client(), 42
    asyncio.run(phone.text("x" * 5000 + " __init__.py"))
    assert len(sent["text"]) <= 4000 and sent["parse_mode"] is None and sent["to"] == 42
