"""One live conversation with the ElevenLabs agent over its WebSocket API.

Telegram audio goes in with `feed()`. Agent speech comes out of `playout`, which the
phone side reads 10 ms at a time. `run()` returns when the agent ends the conversation
(its end_call tool closes the socket) or when `stop()` is called (the owner hung up).
"""
import asyncio
import base64
import json
import logging
import re
import time

import aiohttp

from .audio import TG_RATE, Chunker, Playout, Resampler, rate_from_format

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
SEND_CHUNK_MS = 100
MAX_QUEUED_CHUNKS = 50  # 5 s of his voice; beyond that the socket is stuck, drop audio

_SIGNATURE = re.compile(r"(conversation_signature|token|signature)=[^&'\"\s]+", re.I)


def redact(text: str) -> str:
    """Remove signed-URL credentials from error text before it is logged or stored."""
    return _SIGNATURE.sub(r"\1=<redacted>", text)


class VoiceError(RuntimeError):
    pass


class VoiceSession:
    def __init__(self, api_key: str, agent_id: str, dynamic_variables: dict[str, str],
                 api_base: str = API, on_tool=None):
        self.api_key = api_key
        self.agent_id = agent_id
        self.dynamic_variables = dynamic_variables
        self.api_base = api_base
        self.on_tool = on_tool  # async (name, params) -> str
        self.playout = Playout()
        self.transcript: list[dict] = []
        self.conversation_id: str | None = None
        self.first_audio_at: float | None = None
        self.stopped = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ready = False
        self._to_agent: Resampler | None = None
        self._from_agent: Resampler | None = None
        self._chunker: Chunker | None = None
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue(MAX_QUEUED_CHUNKS)
        self._last_interrupt_id = -1
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _signed_url(self, http: aiohttp.ClientSession) -> str:
        async with http.get(
            f"{self.api_base}/v1/convai/conversation/get-signed-url",
            params={"agent_id": self.agent_id},
            headers={"xi-api-key": self.api_key},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise VoiceError(f"ElevenLabs signed URL failed: HTTP {resp.status} {body[:200]}")
            return json.loads(body)["signed_url"]

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_str(json.dumps(payload))
        except (ConnectionResetError, aiohttp.ClientError):
            pass

    async def _sender(self) -> None:
        """Single writer for the owner's audio, so chunks stay in order."""
        while True:
            chunk = await self._outbox.get()
            await self._send({"user_audio_chunk": base64.b64encode(chunk).decode()})

    def feed(self, pcm48: bytes) -> None:
        """Called for every 10 ms of the owner's voice. Non-blocking."""
        if not self._ready or self.stopped.is_set():
            return
        for chunk in self._chunker.push(self._to_agent(pcm48)):
            try:
                self._outbox.put_nowait(chunk)
            except asyncio.QueueFull:
                log.warning("audio to ElevenLabs is backing up; dropping 100 ms")

    def stop(self) -> None:
        self.stopped.set()
        if self._ws is not None and not self._ws.closed:
            self._spawn(self._ws.close())

    async def run(self) -> None:
        try:
            async with aiohttp.ClientSession() as http:
                url = await self._signed_url(http)
                if self.stopped.is_set():
                    return
                try:
                    ws = await http.ws_connect(url, heartbeat=None, max_msg_size=0)
                except aiohttp.WSServerHandshakeError as e:
                    raise VoiceError(f"ElevenLabs refused the voice connection (HTTP {e.status})") from None
                except aiohttp.ClientError as e:
                    raise VoiceError(f"cannot reach ElevenLabs: {type(e).__name__}") from None
                try:
                    self._ws = ws
                    if self.stopped.is_set():
                        return
                    self._spawn(self._sender())
                    await self._send({
                        "type": "conversation_initiation_client_data",
                        "dynamic_variables": self.dynamic_variables,
                    })
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
                            continue
                        await self._handle(json.loads(msg.data))
                        if self.stopped.is_set():
                            break
                finally:
                    await ws.close()
        finally:
            self._ready = False
            for task in list(self._tasks):
                task.cancel()

    async def _pong(self, event_id, delay_ms) -> None:
        if delay_ms:
            await asyncio.sleep(min(delay_ms, 1000) / 1000)
        await self._send({"type": "pong", "event_id": event_id})

    async def _handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "conversation_initiation_metadata":
            meta = event["conversation_initiation_metadata_event"]
            self.conversation_id = meta.get("conversation_id")
            in_rate = rate_from_format(meta.get("user_input_audio_format", "pcm_16000"))
            out_rate = rate_from_format(meta.get("agent_output_audio_format", "pcm_16000"))
            self._to_agent = Resampler(TG_RATE, in_rate)
            self._from_agent = Resampler(out_rate, TG_RATE)
            self._chunker = Chunker(in_rate * 2 * SEND_CHUNK_MS // 1000)
            self._ready = True
            log.info("conversation %s started (in %s Hz, out %s Hz)", self.conversation_id, in_rate, out_rate)
        elif kind == "audio":
            audio = event["audio_event"]
            if self._from_agent is None:
                return
            event_id = audio.get("event_id")
            if isinstance(event_id, int) and event_id <= self._last_interrupt_id:
                return  # late audio from a reply the owner talked over
            pcm = base64.b64decode(audio["audio_base_64"])
            if self.first_audio_at is None:
                self.first_audio_at = time.monotonic()
            self.playout.push(self._from_agent(pcm))
        elif kind == "interruption":
            event_id = event.get("interruption_event", {}).get("event_id")
            if isinstance(event_id, int):
                self._last_interrupt_id = max(self._last_interrupt_id, event_id)
            self.playout.clear()
        elif kind == "ping":
            ping = event.get("ping_event", {})
            self._spawn(self._pong(ping.get("event_id"), ping.get("ping_ms") or 0))
        elif kind == "agent_response":
            text = event["agent_response_event"].get("agent_response", "")
            if text:
                self.transcript.append({"role": "claude", "text": text})
        elif kind == "agent_response_correction":
            corr = event.get("agent_response_correction_event", {})
            fixed = corr.get("corrected_agent_response")
            if fixed and self.transcript and self.transcript[-1]["role"] == "claude":
                self.transcript[-1]["text"] = fixed
        elif kind == "user_transcript":
            text = event["user_transcription_event"].get("user_transcript", "")
            if text:
                self.transcript.append({"role": "owner", "text": text})
        elif kind == "client_tool_call":
            call = event["client_tool_call"]
            result, is_error = "This tool is not available on this call.", True
            if self.on_tool is not None:
                try:
                    result, is_error = await self.on_tool(call.get("tool_name"), call.get("parameters") or {}), False
                except Exception as e:
                    log.warning("tool %s failed: %s", call.get("tool_name"), e)
                    result = f"Saving failed: {type(e).__name__}. Say it was not saved."
            if call.get("expects_response", True):
                await self._send({
                    "type": "client_tool_result",
                    "tool_call_id": call.get("tool_call_id"),
                    "result": result,
                    "is_error": is_error,
                })


async def fetch_analysis(api_key: str, conversation_id: str, wait_seconds: int = 45,
                         api_base: str = API) -> dict:
    """After a call, ElevenLabs writes a summary and the data-collection fields.
    Poll until they appear (or give up) and return what we got. Never raises."""
    deadline = time.monotonic() + wait_seconds
    result: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.monotonic() < deadline:
                try:
                    async with http.get(
                        f"{api_base}/v1/convai/conversations/{conversation_id}",
                        headers={"xi-api-key": api_key},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            analysis = data.get("analysis") or {}
                            result = {
                                "summary": analysis.get("transcript_summary") or "",
                                "data": {
                                    k: (v or {}).get("value")
                                    for k, v in (analysis.get("data_collection_results") or {}).items()
                                },
                            }
                            if data.get("status") == "done" and result["summary"]:
                                return result
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    log.info("analysis fetch: %s", type(e).__name__)
                await asyncio.sleep(3)
    except Exception as e:  # analysis is a nice-to-have; the transcript is already saved
        log.warning("analysis fetch gave up: %s", redact(str(e)))
    return result
