"""Save everything the assistant handles into the owner's notes repo.

Calls in both directions, text replies, worker results and opened sessions land
as one markdown file each in NOTES_DIR (default `raw/voice-notes/`), ready for
an LLM wiki or any notes workflow to pick up later.
Uses the GitHub contents API with a fine-grained token that can only write to
that one repository.
"""
import base64
import logging
import re
from datetime import datetime

import aiohttp

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
NOTES_DIR = "raw/voice-notes"


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "call"


def render_record(when: datetime, title: str, kind: str, call_id: str,
                  sections: list[tuple[str, str]], transcript: list[dict] | None = None,
                  assistant: str = "Assistant", owner: str = "Owner") -> str:
    """One raw source file. kind: call-in, call-out, text-reply, worker, session."""
    lines = [
        "---",
        f"date: {when:%Y-%m-%d %H:%M} ({when.tzname()})",
        f"source: {assistant}, phone assistant (ring-me), {kind}",
        f"call_id: {call_id}",
        f"type: {kind}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    for heading, text in sections:
        if text and text.strip():
            lines += [f"## {heading}", "", text.strip(), ""]
    if transcript:
        lines += ["## Transcript", ""]
        names = {"claude": assistant, "owner": owner}
        lines += [f"**{names.get(t['role'], t['role'].capitalize())}:** {t['text']}  " for t in transcript]
    return "\n".join(lines).rstrip() + "\n"


def render(when: datetime, notes: list[dict], answers: list[dict], transcript: list[dict],
           call_id: str, tasks: list[dict] | None = None,
           assistant: str = "Assistant", owner: str = "Owner") -> str:
    """A call the owner made to the assistant."""
    title = notes[0]["title"] if notes else f"{owner} called {assistant}"
    sections = [(n["title"], n["note"]) for n in notes]
    if answers:
        sections.append(("Answers given to waiting sessions",
                         "\n".join(f"- **{a['reason']}** → {a['answer']}" for a in answers)))
    if tasks:
        sections.append(("Work handed off", "\n".join(f"- {t['kind']}: {t['task']}" for t in tasks)))
    return render_record(when, title, "call-in", call_id, sections, transcript, assistant, owner)


async def save_to_repo(token: str, repo: str, when: datetime, content: str, title: str,
                       api_base: str = GITHUB_API, notes_dir: str = NOTES_DIR) -> str:
    """Create the file and return its path. Raises on failure."""
    path = f"{notes_dir}/{when:%Y-%m-%d-%H%M%S}-{slugify(title)}.md"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {
        "message": f"voice note: {title[:60]}",
        "content": base64.b64encode(content.encode()).decode(),
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        for attempt in range(3):
            async with http.put(f"{api_base}/repos/{repo}/contents/{path}", json=body,
                                headers=headers) as resp:
                if resp.status in (200, 201):
                    return path
                text = (await resp.text())[:200]
                if resp.status == 422 and attempt < 2:  # name taken: add a suffix
                    path = path.removesuffix(".md") + f"-{attempt + 2}.md"
                    continue
                raise RuntimeError(f"GitHub HTTP {resp.status}: {text}")
    raise RuntimeError("GitHub: could not pick a free file name")
