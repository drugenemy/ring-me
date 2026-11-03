"""Call records: kept in memory, saved as one JSON file each so a restart keeps history."""
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# A call is "open" while a session could still get an answer from it.
FINAL = {"answered", "replied", "failed", "cancelled", "no_answer", "delivered"}


@dataclass
class Call:
    id: str
    reason: str
    details: str = ""
    question: str = ""
    session: str = ""
    urgent: bool = False
    mode: str = "call"  # "call", "text" (wants a reply), "notice" (no reply), "inbound"
    status: str = "queued"
    # queued -> ringing -> talking -> answered
    #                   \-> missed (texted him; waiting for a text reply) -> replied | no_answer
    # text mode: queued -> sent -> replied | no_answer
    # notice mode: queued -> delivered
    answer: str = ""
    summary: str = ""
    transcript: list = field(default_factory=list)
    error: str = ""
    attempts: int = 0
    conversation_id: str = ""
    message_ids: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    first_word_ms: int | None = None

    @property
    def final(self) -> bool:
        return self.status in FINAL

    def public(self) -> dict:
        d = asdict(self)
        d.pop("message_ids")
        d["final"] = self.final
        return d


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "calls"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.calls: dict[str, Call] = {}
        for path in sorted(self.dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
                self.calls[raw["id"]] = Call(**{k: v for k, v in raw.items() if k in Call.__dataclass_fields__})
            except (ValueError, KeyError, TypeError):
                continue

    def new(self, **kwargs) -> Call:
        call = Call(id="c_" + secrets.token_hex(6), **kwargs)
        self.calls[call.id] = call
        self.save(call)
        return call

    def get(self, call_id: str) -> Call | None:
        return self.calls.get(call_id)

    def save(self, call: Call) -> None:
        call.updated_at = time.time()
        tmp = self.dir / f"{call.id}.json.tmp"
        tmp.write_text(json.dumps(asdict(call), ensure_ascii=False, indent=1))
        tmp.replace(self.dir / f"{call.id}.json")

    def waiting_for_text(self) -> list[Call]:
        """Calls where the owner was texted and their reply would answer them, newest first."""
        open_ = [c for c in self.calls.values() if c.status in ("missed", "sent")]
        return sorted(open_, key=lambda c: c.created_at, reverse=True)
