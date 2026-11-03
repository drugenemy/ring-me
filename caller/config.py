"""Settings, read once from environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing environment variable {name}. See .env.example.")
    return value


def _list(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    owner_tg_id: int
    elevenlabs_api_key: str
    elevenlabs_agent_id: str
    caller_token: str
    port: int
    ring_seconds: int
    max_call_seconds: int
    data_dir: Path
    owner_name: str = "there"
    assistant_name: str = "Jessica"
    timezone: str = "UTC"
    elevenlabs_inbound_agent_id: str = ""
    github_token: str = ""
    notes_repo: str = ""
    notes_dir: str = "raw/voice-notes"
    context_files: tuple[str, ...] = ()
    worker_url: str = ""
    worker_token: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def notes_on(self) -> bool:
        """Saving calls and reading context both need a GitHub token and a repo."""
        return bool(self.github_token and self.notes_repo)

    @classmethod
    def from_env(cls) -> "Settings":
        token = _need("CALLER_TOKEN")
        if len(token) < 24:
            raise SystemExit("CALLER_TOKEN is too short. Use at least 24 random characters.")
        timezone = os.environ.get("TIMEZONE", "UTC").strip() or "UTC"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"TIMEZONE={timezone!r} is not a known time zone. Use a name like Europe/Berlin.")
        return cls(
            tg_api_id=int(_need("TG_API_ID")),
            tg_api_hash=_need("TG_API_HASH"),
            tg_session=_need("TG_SESSION"),
            owner_tg_id=int(_need("OWNER_TG_ID")),
            elevenlabs_api_key=_need("ELEVENLABS_API_KEY"),
            elevenlabs_agent_id=_need("ELEVENLABS_AGENT_ID"),
            caller_token=token,
            port=int(os.environ.get("PORT", "8080")),
            ring_seconds=int(os.environ.get("RING_SECONDS", "45")),
            max_call_seconds=int(os.environ.get("MAX_CALL_SECONDS", "600")),
            data_dir=Path(os.environ.get("DATA_DIR", "./data")),
            owner_name=os.environ.get("OWNER_NAME", "").strip() or "there",
            assistant_name=os.environ.get("ASSISTANT_NAME", "").strip() or "Jessica",
            timezone=timezone,
            elevenlabs_inbound_agent_id=os.environ.get("ELEVENLABS_INBOUND_AGENT_ID", "").strip(),
            github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
            notes_repo=os.environ.get("NOTES_REPO", "").strip(),
            notes_dir=os.environ.get("NOTES_DIR", "raw/voice-notes").strip().strip("/") or "raw/voice-notes",
            context_files=_list("CONTEXT_FILES"),
            worker_url=os.environ.get("WORKER_URL", "").strip().rstrip("/"),
            worker_token=os.environ.get("WORKER_TOKEN", "").strip(),
        )
