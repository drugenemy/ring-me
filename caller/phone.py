"""The assistant's Telegram account: places private calls and sends/receives texts.

Uses Telethon for the account and py-tgcalls for the call audio. The account can
reach exactly one person, the owner (OWNER_TG_ID). Nothing here takes a target from
outside.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

import numpy as np
from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NotInCallError
from pytgcalls.types import (
    CallConfig,
    ChatUpdate,
    Device,
    Direction,
    ExternalMedia,
    MediaStream,
    RecordStream,
    StreamFrames,
)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from .audio import FRAME_MS, TG_RATE

log = logging.getLogger(__name__)

AUDIO = AudioParameters(bitrate=TG_RATE, channels=1)
TELEGRAM_TEXT_LIMIT = 4000  # Telegram's hard cap is 4096


def mix(frames: list[bytes]) -> bytes:
    if len(frames) == 1:
        return frames[0]
    arrays = [np.frombuffer(f, dtype=np.int16).astype(np.int32) for f in frames]
    n = max(len(a) for a in arrays)
    total = np.zeros(n, dtype=np.int32)
    for a in arrays:
        total[: len(a)] += a
    return np.clip(total // len(arrays), -32768, 32767).astype(np.int16).tobytes()


class Phone:
    def __init__(self, api_id: int, api_hash: str, session: str, owner_id: int):
        self.owner_id = owner_id
        self.client = TelegramClient(StringSession(session), api_id, api_hash)
        self.calls = PyTgCalls(self.client)
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_hangup: Callable[[], None] | None = None
        self.on_text: Callable[[str, int | None], Awaitable[None]] | None = None
        self.on_incoming_call: Callable[[], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("TG_SESSION is not logged in. Run `python login.py` again.")

        @self.calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def _frames(_, update: StreamFrames):
            if update.chat_id == self.owner_id and self.on_audio and update.frames:
                self.on_audio(mix([f.frame for f in update.frames]))

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.DISCARDED_CALL))
        async def _ended(_, update: ChatUpdate):
            if update.chat_id == self.owner_id and self.on_hangup:
                self.on_hangup()

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _incoming(_, update: ChatUpdate):
            # py-tgcalls waits for this handler before reading the next Telegram
            # update, and taking the call needs those updates. So never handle the
            # call here: hand it to a background task and return at once.
            if update.chat_id == self.owner_id and self.on_incoming_call:
                self._spawn(self.on_incoming_call())
            else:  # anyone else is dropped without a word
                self._spawn(self.hang_up_on(update.chat_id))

        @self.client.on(events.NewMessage(incoming=True, from_users=[self.owner_id]))
        async def _text(event):
            if event.is_private and self.on_text and event.raw_text:
                await self.on_text(event.raw_text, event.reply_to_msg_id)

        await self.calls.start()
        self._patch_library()
        # A string session forgets known users on restart. Loading recent chats puts
        # the owner back in the cache (they must have messaged this account once). The spare
        # account has very few chats, so 100 is plenty.
        await self.client.get_dialogs(limit=100)
        try:
            await self.client.get_input_entity(self.owner_id)
        except ValueError:
            raise SystemExit(
                "This Telegram account does not know the owner (OWNER_TG_ID) yet. Send it any message "
                "from your own Telegram, then restart."
            )
        me = await self.client.get_me()
        log.info("telegram ready as %s (id %s)", me.username or me.first_name, me.id)

    def _patch_library(self) -> None:
        """py-tgcalls 3.0.0 calls discard_call(chat_id) without `is_missed` when a
        connection drops, which raises inside its own handler and leaves the call
        half-open. Give the argument a default."""
        app = self.calls._app
        original = app.discard_call

        async def discard_call(chat_id, is_missed: bool = False):
            return await original(chat_id, is_missed)

        app.discard_call = discard_call

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    async def ring(self, ring_seconds: int) -> None:
        """Call the owner and return once they pick up and audio is connected.
        Raises pytgcalls CallDeclined / CallBusy / TimedOutAnswer / CallDiscarded
        otherwise. Cancel the task to stop ringing (then call hang_up)."""
        await self.calls.play(
            self.owner_id,
            MediaStream(ExternalMedia.AUDIO, AUDIO),
            config=CallConfig(timeout=ring_seconds),
        )
        await self.calls.record(self.owner_id, RecordStream(True, AUDIO))

    async def answer(self, timeout: int = 20) -> None:
        """Pick up the owner's ringing call. Returns once audio is connected."""
        await self.calls.play(
            self.owner_id,
            MediaStream(ExternalMedia.AUDIO, AUDIO),
            config=CallConfig(timeout=timeout),
        )
        await self.calls.record(self.owner_id, RecordStream(True, AUDIO))

    async def pump(self, next_frame: Callable[[], bytes], done: asyncio.Event) -> None:
        """Send one 10 ms frame every 10 ms until `done` is set. Keeps real-time pace
        without drifting; if the loop falls badly behind it resyncs instead of bursting.
        Returns early only when the call is gone."""
        loop = asyncio.get_running_loop()
        step = FRAME_MS / 1000
        t = loop.time()
        errors = 0
        while not done.is_set():
            try:
                await self.calls.send_frame(self.owner_id, Device.MICROPHONE, next_frame())
                errors = 0
            except NotInCallError:
                log.info("call is gone; stopping audio out")
                return
            except Exception as e:
                errors += 1
                if errors in (1, 100):
                    log.warning("send_frame error (%s in a row): %s", errors, e)
                if errors > 500:  # 5 s of nothing but errors
                    return
            t += step
            delay = t - loop.time()
            if delay < -0.2:
                t = loop.time()
            elif delay > 0:
                await asyncio.sleep(delay)

    async def hang_up(self) -> None:
        """End the call in every state (ringing, connected, half-open). Safe to repeat."""
        await self.hang_up_on(self.owner_id)

    async def hang_up_on(self, chat_id: int) -> None:
        try:
            await self.calls.leave_call(chat_id)
        except NotInCallError:
            pass
        except Exception as e:
            log.info("leave_call: %s", e)

    async def text(self, message: str) -> int:
        if len(message) > TELEGRAM_TEXT_LIMIT:
            message = message[: TELEGRAM_TEXT_LIMIT - 20] + "\n…(cut)"
        sent = await self.client.send_message(self.owner_id, message, parse_mode=None)
        return sent.id
