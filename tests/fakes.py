"""Stand-ins for ElevenLabs and Telegram so a whole call can run in a test."""
import asyncio
import base64
import json

import numpy as np
from aiohttp import WSMsgType, web

from caller.audio import TG_FRAME_BYTES


def tone(rate: int, ms: int, freq: float = 440.0) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16).tobytes()


class FakeElevenLabs:
    """Plays one scripted conversation: greet, wait for the owner's audio, hear him, say bye, hang up."""

    def __init__(self, end_by_agent: bool = True, analysis_status: int = 200):
        self.end_by_agent = end_by_agent
        self.analysis_status = analysis_status
        self.init_payload = None
        self.got_pong = False
        self.audio_bytes_in = 0
        self.closed_by_client = False
        self.app = web.Application()
        self.app.add_routes([
            web.get("/v1/convai/conversation/get-signed-url", self.signed_url),
            web.get("/ws", self.ws),
            web.get("/v1/convai/conversations/{cid}", self.conversation),
        ])
        self.base = ""

    async def signed_url(self, request):
        assert request.headers.get("xi-api-key") == "el-key"
        assert request.query["agent_id"] == "agent-1"
        ws_url = self.base.replace("http://", "ws://") + "/ws?token=x"
        return web.json_response({"signed_url": ws_url})

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        first = await ws.receive_json()
        self.init_payload = first
        await ws.send_json({"type": "conversation_initiation_metadata",
                            "conversation_initiation_metadata_event": {
                                "conversation_id": "conv_1",
                                "agent_output_audio_format": "pcm_16000",
                                "user_input_audio_format": "pcm_16000"}})
        await ws.send_json({"type": "ping", "ping_event": {"event_id": 7, "ping_ms": 5}})
        await ws.send_json({"type": "audio", "audio_event": {
            "audio_base_64": base64.b64encode(tone(16000, 300)).decode(), "event_id": 1}})
        await ws.send_json({"type": "agent_response", "agent_response_event": {
            "agent_response": "Hey Sam, build is done. Ship it?"}})
        while self.audio_bytes_in < 16000 * 2 * 0.3:  # wait for 300 ms of his voice
            msg = await ws.receive()
            if msg.type != WSMsgType.TEXT:
                self.closed_by_client = True
                return ws
            data = json.loads(msg.data)
            if data.get("type") == "pong" and data.get("event_id") == 7:
                self.got_pong = True
            if "user_audio_chunk" in data:
                self.audio_bytes_in += len(base64.b64decode(data["user_audio_chunk"]))
        await ws.send_json({"type": "user_transcript", "user_transcription_event": {
            "user_transcript": "Yes, ship it."}})
        await ws.send_json({"type": "agent_response", "agent_response_event": {
            "agent_response": "Got it. Bye."}})
        await ws.send_json({"type": "audio", "audio_event": {
            "audio_base_64": base64.b64encode(tone(16000, 200)).decode(), "event_id": 2}})
        if self.end_by_agent:
            await ws.close()
        else:
            async for _ in ws:
                pass
            self.closed_by_client = True
        return ws

    async def conversation(self, request):
        if self.analysis_status != 200:
            return web.json_response({"detail": "boom"}, status=self.analysis_status)
        return web.json_response({"status": "done", "analysis": {
            "transcript_summary": "Claude reported the build; Sam said ship.",
            "data_collection_results": {"answer": {"value": "Ship it."}}}})


class FakePhone:
    """Acts like Phone: 'answers' immediately, and when it hears the agent, talks back."""

    def __init__(self, ring_error: Exception | None = None, ring=None):
        self.ring_error = ring_error
        self.ring_fn = ring  # optional async callable(self) replacing the default ring
        self.hang_ups = 0
        self.on_incoming_call = None
        self.frames_out = 0
        self.texts: list[str] = []
        self.hung_up = False
        self.on_audio = None
        self.on_hangup = None
        self.on_text = None
        self.connected = True
        self.rings = 0

    async def ring(self, ring_seconds):
        self.rings += 1
        if self.ring_fn:
            return await self.ring_fn(self)
        if self.ring_error:
            raise self.ring_error

    async def pump(self, next_frame, done):
        voice_sent = False
        while not done.is_set():
            frame = next_frame()
            assert len(frame) == TG_FRAME_BYTES
            self.frames_out += 1
            if any(frame) and not voice_sent:
                voice_sent = True
                asyncio.ensure_future(self._speak())
            await asyncio.sleep(0.002)  # faster than real time; fine for tests

    async def _speak(self):
        pcm = tone(48000, 600, 220)
        for i in range(0, len(pcm), TG_FRAME_BYTES):
            self.on_audio(pcm[i:i + TG_FRAME_BYTES])
            await asyncio.sleep(0.001)

    async def answer(self):
        self.answers = getattr(self, "answers", 0) + 1
        if self.ring_fn:
            return await self.ring_fn(self)

    async def hang_up(self):
        self.hung_up = True
        self.hang_ups += 1

    async def text(self, message):
        self.texts.append(message)
        return 100 + len(self.texts)
