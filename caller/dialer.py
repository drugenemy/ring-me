"""Runs calls one at a time: ring, talk, collect the answer, or fall back to a text."""
import asyncio
import logging
import time
from datetime import datetime

from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer

from .config import Settings
from .phone import Phone
from .store import Call, Store
from .notes import GITHUB_API, render, render_record, save_to_repo
from .context import notes_context
from .worker_client import open_worker_session, start_worker_task
from .voice import API, VoiceSession, fetch_analysis, redact

log = logging.getLogger(__name__)

URGENT_RETRY_GAP = 60
TEXT_REPLY_HOURS = 24
DRAIN_SECONDS = 15
CONNECT_GRACE = 30        # extra time after ringing for the audio to connect
SILENCE_HANGUP = 45       # no audio from his side this long = the call is dead
STALE_AFTER_RESTART = 15 * 60
MAX_PENDING_ON_CALLBACK = 3
ANSWER_TIMEOUT = 30
ANSWER_FIELD = "answer"   # ElevenLabs data-collection field with the decision (optional)
NOT_PICKED_UP = (CallDeclined, CallBusy, TimedOutAnswer, CallDiscarded, asyncio.TimeoutError)


def missed_text(call: Call, cut_off: bool = False, assistant: str = "Your assistant") -> str:
    head = "🔴 URGENT — " if call.urgent else ""
    if cut_off:
        first = f"{head}{assistant}'s call got cut off before you answered."
    elif call.mode == "call":
        first = f"{head}{assistant} tried to call you."
    elif call.mode == "notice":
        first = f"{head}{call.reason}"
    else:
        first = f"{head}Message from a Claude session (via {assistant})."
    lines = [first]
    if call.session:
        lines.append(f"Session: {call.session}")
    if call.mode != "notice":
        lines += ["", call.reason]
    if call.details:
        lines += ["", call.details]
    if call.question:
        lines += ["", f"❓ {call.question}"]
    if call.mode != "notice":
        lines += ["", "Reply to this message and I'll pass it to the session."]
    return "\n".join(lines)


class Dialer:
    def __init__(self, settings: Settings, phone: Phone, store: Store):
        self.s = settings
        self.phone = phone
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.current: Call | None = None
        self.api_base = API
        self.github_api = GITHUB_API
        self._voice: VoiceSession | None = None
        self._ring_task: asyncio.Task | None = None
        self._hung_up = False
        self._last_audio_at = 0.0
        self._background: set[asyncio.Task] = set()
        self._line = asyncio.Lock()  # one Telegram call at a time, in or out
        self.connect_grace = CONNECT_GRACE
        self.retry_gap = URGENT_RETRY_GAP
        self.analysis_wait = 30
        phone.on_audio = self._on_audio
        phone.on_hangup = self._on_hangup
        phone.on_text = self._on_text
        phone.on_incoming_call = self._on_incoming_call

    def _names(self, now: datetime) -> dict:
        """Dynamic variables every agent prompt can use."""
        return {"owner_name": self.s.owner_name, "assistant_name": self.s.assistant_name,
                "local_time": now.strftime("%A %H:%M"), "timezone": self.s.timezone}

    def _hello(self) -> str:
        return f"Hey {self.s.owner_name}, it's {self.s.assistant_name}."

    # ---- events from Telegram -------------------------------------------------

    def _on_audio(self, pcm: bytes) -> None:
        self._last_audio_at = time.monotonic()
        if self._voice is not None:
            self._voice.feed(pcm)

    def _on_hangup(self) -> None:
        self._hung_up = True
        if self._voice is not None:
            log.info("owner hung up")
            self._voice.stop()

    async def _on_incoming_call(self) -> None:
        """The owner is calling the assistant's Telegram account."""
        if not self.s.elevenlabs_inbound_agent_id:
            await self.phone.hang_up()
            await self.phone.text("I can't pick up calls yet. Reply in text instead.")
            return
        if self._line.locked():
            log.info("incoming call while busy; declining")
            await self.phone.hang_up()
            await self.phone.text("I'm on another call. Try again in a minute.")
            return
        async with self._line:
            call = self.store.new(reason=f"{self.s.owner_name} called", mode="inbound", status="talking")
            self.current = call
            try:
                await self._inbound(call)
            except Exception as e:
                log.error("inbound call failed: %s", redact(f"{type(e).__name__}: {e}"))
                call.status = "failed"
                call.error = redact(f"{type(e).__name__}: {e}")[:300]
                self.store.save(call)
            finally:
                await self.phone.hang_up()
                self.current = None
                self._voice = None

    def _pending_brief(self, pending: list[Call]) -> str:
        if not pending:
            return "none"
        parts = []
        for i, c in enumerate(pending, 1):
            item = f"{i}. From {c.session or 'a Claude session'}: {c.reason}"
            if c.question:
                item += f" Question: {c.question}"
            if c.details:
                item += f" Facts: {c.details[:600]}"
            parts.append(item)
        return "\n".join(parts)

    async def _inbound(self, call: Call) -> None:
        # Only things that need his answer: missed calls, or texts that asked a question.
        pending = [c for c in self.store.waiting_for_text()
                   if c.mode == "call" or c.question][:MAX_PENDING_ON_CALLBACK]
        self._hung_up = False
        await asyncio.wait_for(self.phone.answer(), ANSWER_TIMEOUT)
        if pending:
            first = pending[0]
            greeting = f"{self._hello()} You missed my call. {first.reason}"
            if len(pending) > 1:
                greeting += f" And {len(pending) - 1} more thing{'s' if len(pending) > 2 else ''} after that."
        else:
            greeting = f"{self._hello()} What's up?"

        notes: list[dict] = []
        answers: list[dict] = []
        tasks: list[dict] = []
        context = await notes_context(self.s.github_token, self.s.notes_repo, self.s.context_files,
                                      api_base=self.github_api)
        voice_ref: list[VoiceSession] = []

        async def on_tool(name: str, params: dict) -> str:
            if name == "record_answer":
                try:
                    n = int(params.get("item"))
                except (TypeError, ValueError):
                    return "Unknown item number."
                answer = str(params.get("answer", "")).strip()
                if not 1 <= n <= len(pending) or not answer:
                    return "Unknown item number."
                target = pending[n - 1]
                if target.status in ("missed", "sent", "no_answer", "replied"):
                    target.answer = answer if target.status != "replied" else target.answer + "\n" + answer
                    target.status = "replied"
                    self.store.save(target)
                answers.append({"reason": target.reason, "answer": answer, "id": target.id})
                return "Saved. The session will get it."
            if name == "save_note":
                title = str(params.get("title", "")).strip()[:120] or "Voice note"
                note = str(params.get("note", "")).strip()[:4000]
                if not note:
                    return "The note was empty."
                notes.append({"title": title, "note": note})
                return "Saved."
            if name == "start_task":
                task = str(params.get("task", "")).strip()[:2000]
                if not task:
                    return "The task was empty."
                if not (self.s.worker_url and self.s.worker_token):
                    return "The background worker is not set up yet. Offer to save it as a note instead."
                said = voice_ref[0].transcript[-12:] if voice_ref else []
                context = "\n".join(f"{t['role']}: {t['text']}" for t in said)
                task_id = await start_worker_task(self.s.worker_url, self.s.worker_token, task, context)
                tasks.append({"task": task, "id": task_id, "kind": "worker task"})
                return "Started. A Claude worker is on it and will call him when it is done or needs him."
            if name == "open_session":
                task = str(params.get("task", "")).strip()[:2000]
                if not task:
                    return "The task was empty."
                if not (self.s.worker_url and self.s.worker_token):
                    return "Opening sessions is not set up yet. Offer to save it as a note instead."
                said = voice_ref[0].transcript[-12:] if voice_ref else []
                context = "\n".join(f"{t['role']}: {t['text']}" for t in said)
                task_id = await open_worker_session(self.s.worker_url, self.s.worker_token, task, context)
                tasks.append({"task": task, "id": task_id, "kind": "new session"})
                return ("Opening a new Claude session for it now. He gets the link by text in a minute, "
                        "and the session will call him when it is done or needs him.")
            return "Unknown tool."

        now = datetime.now(self.s.tz)
        voice = VoiceSession(
            self.s.elevenlabs_api_key,
            self.s.elevenlabs_inbound_agent_id,
            {"pending": self._pending_brief(pending), "greeting": greeting,
             "context": context, **self._names(now)},
            api_base=self.api_base,
            on_tool=on_tool,
        )
        voice_ref.append(voice)
        await self._converse(voice, call.id)

        call.transcript = voice.transcript
        call.conversation_id = voice.conversation_id or ""
        call.answer = "\n".join(f"{a['reason']} -> {a['answer']}" for a in answers)
        call.summary = "; ".join([n["title"] for n in notes] + [f"task: {t['task'][:80]}" for t in tasks])
        call.status = "answered"
        self.store.save(call)
        if not any(t["role"] == "owner" for t in voice.transcript):
            return  # nothing said; nothing to keep

        saved_path = ""
        if self.s.notes_on:
            try:
                content = render(now, notes, answers, voice.transcript, call.id, tasks,
                                 self.s.assistant_name, self.s.owner_name)
                title = notes[0]["title"] if notes else f"call to {self.s.assistant_name}"
                saved_path = await save_to_repo(self.s.github_token, self.s.notes_repo, now, content, title,
                                                api_base=self.github_api, notes_dir=self.s.notes_dir)
            except Exception as e:
                log.warning("saving the call to the notes repo failed: %s", e)
                call.error = f"notes save failed: {type(e).__name__}"
                self.store.save(call)
        messages = []
        for note in notes:
            text = f"📝 {note['title']}\n\n{note['note']}"
            if saved_path:
                text += f"\n\nSaved to your notes: {saved_path}"
            elif self.s.notes_on:
                text += "\n\n(Could not save it to your notes this time.)"
            messages.append(text)
        if tasks:
            messages.append("🛠 Handed off:\n" + "\n".join(f"- {t['kind']}: {t['task']}" for t in tasks))
        if answers:
            messages.append("✅ Passed to the sessions:\n"
                            + "\n".join(f"- {a['reason']} → {a['answer']}" for a in answers))
        for text in messages:
            try:
                await self.phone.text(text)
            except Exception as e:
                log.warning("could not text the call summary: %s", e)

    async def _on_text(self, text: str, reply_to: int | None) -> None:
        if reply_to is not None:
            target = next((c for c in self.store.calls.values() if reply_to in c.message_ids), None)
            if target is None or target.status == "cancelled":
                return  # a reply to something else; not an answer to any call
            if target.status == "replied":
                target.answer += "\n" + text
            elif target.status in ("missed", "sent", "no_answer"):
                target.answer = text
                target.status = "replied"
            else:
                return
            self.store.save(target)
            log.info("threaded text reply attached to %s", target.id)
            self._spawn(self._archive(target, "text-reply"))
            return
        waiting = self.store.waiting_for_text()
        if len(waiting) == 1:
            target = waiting[0]
            target.answer = text
            target.status = "replied"
            self.store.save(target)
            log.info("text reply attached to %s", target.id)
            self._spawn(self._archive(target, "text-reply"))
        elif len(waiting) > 1:
            await self.phone.text(
                f"{len(waiting)} sessions are waiting for you. Swipe-reply to the message you are answering."
            )

    # ---- queue --------------------------------------------------------------

    def submit(self, call: Call) -> None:
        self.queue.put_nowait(call.id)

    def cancel(self, call: Call) -> None:
        if call.final:
            return
        call.status = "cancelled"
        self.store.save(call)
        if self.current is call:
            if self._ring_task is not None:
                self._ring_task.cancel()
            if self._voice is not None:
                self._voice.stop()

    async def _archive(self, call: Call, kind: str) -> None:
        """Save what happened to the notes repo, if one is set up. Never raises."""
        if not self.s.notes_on:
            return
        try:
            when = datetime.fromtimestamp(call.created_at, self.s.tz)
            me, owner = self.s.assistant_name, self.s.owner_name
            if kind == "call-out":
                title = f"{me} called {owner}: {call.reason}"
                sections = [("Why she called", f"From: {call.session or 'a Claude session'}\n\n{call.reason}"),
                            ("Facts she had", call.details), ("Question", call.question),
                            ("The answer", call.answer), ("Summary", call.summary)]
                transcript = call.transcript
            elif kind == "text-reply":
                title = f"{owner} replied by text: {call.reason}"
                sections = [("What was asked", f"From: {call.session or 'a Claude session'}\n\n{call.reason}"),
                            ("Facts", call.details), ("Question", call.question), ("The reply", call.answer)]
                transcript = None
            else:  # worker results and opened sessions arrive as notices
                title = call.reason
                sections = [("Details", call.details)]
                transcript = None
            content = render_record(when, title[:150], kind, call.id, sections, transcript, me, owner)
            await save_to_repo(self.s.github_token, self.s.notes_repo, when, content,
                               f"{kind} {call.reason}"[:80], api_base=self.github_api,
                               notes_dir=self.s.notes_dir)
        except Exception as e:
            log.warning("could not save %s %s to notes: %s", kind, call.id, e)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _recover_after_restart(self) -> None:
        now = time.time()
        for call in sorted(self.store.calls.values(), key=lambda c: c.created_at):
            if call.status == "talking":
                call.status, call.error = "failed", "service restarted during the call"
                self.store.save(call)
            elif call.status in ("queued", "ringing"):
                if now - call.created_at > STALE_AFTER_RESTART:
                    call.status, call.error = "failed", "expired while the service was down"
                else:
                    call.status = "queued"
                    self.queue.put_nowait(call.id)
                self.store.save(call)

    async def run(self) -> None:
        self._recover_after_restart()
        self._spawn(self._expire_loop())
        while True:
            call_id = await self.queue.get()
            call = self.store.get(call_id)
            if call is None or call.status != "queued":
                continue
            async with self._line:
                await self._process(call)

    async def _process(self, call: Call) -> None:
        self.current = call
        try:
            if call.mode == "text":
                await self._send_text(call, "sent")
            elif call.mode == "notice":
                await self._send_text(call, "delivered")
                self._spawn(self._archive(call, "worker"))
            else:
                await self._do_call(call)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("call %s failed: %s", call.id, redact(f"{type(e).__name__}: {e}"))
            call.error = redact(f"{type(e).__name__}: {e}")[:300]
            self.store.save(call)
            try:
                await self._send_text(call, "missed")
            except Exception as e2:
                if call.status != "cancelled":
                    call.status = "failed"
                    call.error += f"; texting failed too: {type(e2).__name__}"
                    self.store.save(call)
        finally:
            if call.mode == "call":
                await self.phone.hang_up()
            self.current = None
            self._voice = None
            self._ring_task = None

    async def _expire_loop(self) -> None:
        while True:
            try:
                cutoff = time.time() - TEXT_REPLY_HOURS * 3600
                for call in self.store.waiting_for_text():
                    if call.updated_at < cutoff:
                        call.status = "no_answer"
                        self.store.save(call)
            except Exception:
                log.exception("expire loop")
            await asyncio.sleep(600)

    # ---- the work -----------------------------------------------------------

    async def _send_text(self, call: Call, status: str, cut_off: bool = False) -> None:
        if call.status == "cancelled":
            return
        msg_id = await self.phone.text(missed_text(call, cut_off, self.s.assistant_name))
        call.message_ids.append(msg_id)
        if call.status != "cancelled":
            call.status = status
        self.store.save(call)

    async def _ring(self) -> None:
        """Ring once. Returns when connected; raises NOT_PICKED_UP otherwise.
        If the call gets cancelled meanwhile, returns quietly (caller checks status)."""
        self._hung_up = False
        self._ring_task = asyncio.create_task(self.phone.ring(self.s.ring_seconds))
        try:
            await asyncio.wait_for(self._ring_task, self.s.ring_seconds + self.connect_grace)
        except asyncio.CancelledError:
            if self.current is not None and self.current.status == "cancelled":
                return
            raise
        finally:
            self._ring_task = None

    async def _do_call(self, call: Call) -> None:
        tries = 2 if call.urgent else 1
        for attempt in range(tries):
            if call.status == "cancelled":
                return
            call.attempts += 1
            call.status = "ringing"
            self.store.save(call)
            try:
                await self._ring()
            except NOT_PICKED_UP as e:
                log.info("call %s not picked up: %s", call.id, type(e).__name__)
                await self.phone.hang_up()
                call.error = type(e).__name__
                self.store.save(call)
                if attempt + 1 < tries and call.status != "cancelled":
                    await asyncio.sleep(self.retry_gap)
                    continue
                await self._send_text(call, "missed")
                return
            if call.status == "cancelled":
                return  # run() hangs up
            if self._hung_up:  # picked up and dropped before we could speak
                await self.phone.hang_up()
                await self._send_text(call, "missed", cut_off=True)
                return
            await self._talk(call)
            return

    async def _watchdog(self, voice: VoiceSession) -> None:
        while not voice.stopped.is_set():
            await asyncio.sleep(1)
            if time.monotonic() - self._last_audio_at > SILENCE_HANGUP:
                log.info("no audio from the call for %ss; ending", SILENCE_HANGUP)
                voice.stop()

    async def _converse(self, voice: VoiceSession, label: str) -> None:
        """Run one live conversation over the connected Telegram call, then hang up."""
        self._voice = voice
        if self._hung_up:
            voice.stop()
        self._last_audio_at = time.monotonic()
        done = asyncio.Event()
        pump = asyncio.create_task(self.phone.pump(voice.playout.next_frame, done))
        pump.add_done_callback(lambda _: voice.stop() if not done.is_set() else None)
        watchdog = asyncio.create_task(self._watchdog(voice))
        try:
            await asyncio.wait_for(voice.run(), timeout=self.s.max_call_seconds)
        except asyncio.TimeoutError:
            log.info("call %s hit the time limit", label)
        finally:
            # Let the goodbye finish playing before hanging up (not if he already left).
            end = time.monotonic() + DRAIN_SECONDS
            while voice.playout.pending_ms > 0 and time.monotonic() < end and not voice.stopped.is_set():
                await asyncio.sleep(0.1)
            done.set()
            watchdog.cancel()
            await pump
            await self.phone.hang_up()

    async def _talk(self, call: Call) -> None:
        call.status = "talking"
        call.error = ""
        self.store.save(call)
        now = datetime.now(self.s.tz)
        voice = VoiceSession(
            self.s.elevenlabs_api_key,
            self.s.elevenlabs_agent_id,
            {
                "reason": call.reason,
                "details": call.details or "none",
                "question": call.question or "none",
                "session": call.session or "a Claude session",
                "urgency": "urgent" if call.urgent else "normal",
                **self._names(now),
            },
            api_base=self.api_base,
        )
        answered_at = time.monotonic()
        await self._converse(voice, call.id)

        call.transcript = voice.transcript
        call.conversation_id = voice.conversation_id or ""
        if voice.first_audio_at:
            call.first_word_ms = int((voice.first_audio_at - answered_at) * 1000)
        call.answer = "\n".join(t["text"] for t in voice.transcript if t["role"] == "owner")
        self.store.save(call)
        if call.status == "cancelled":
            return
        if not call.answer:
            # He picked up but we never heard him (cut off, bad line). Give him the text route.
            await self._send_text(call, "missed", cut_off=True)
            return
        if call.conversation_id:
            analysis = await fetch_analysis(self.s.elevenlabs_api_key, call.conversation_id,
                                            wait_seconds=self.analysis_wait, api_base=self.api_base)
            call.summary = analysis.get("summary", "")
            decision = (analysis.get("data") or {}).get(ANSWER_FIELD)
            if decision:
                call.answer = str(decision)
        if call.status != "cancelled":
            call.status = "answered"
        self.store.save(call)
        self._spawn(self._archive(call, "call-out"))
