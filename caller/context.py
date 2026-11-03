"""Fetch a few files from the owner's notes repo, for the inbox agent.

CONTEXT_FILES lists the paths (for example a "where I am right now" page and an
index of everything). Files named index.md are cut to short lines so the voice
agent's prompt stays small and replies stay fast. Cached for 10 minutes.
"""
import asyncio
import logging
import time

import aiohttp

from .notes import GITHUB_API

log = logging.getLogger(__name__)

CACHE_SECONDS = 600
LINE_LIMIT = 110
TOTAL_LIMIT = 26_000

_cache: dict[str, tuple[float, str]] = {}


def compress_index(text: str) -> str:
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith(("---", "updated:", "status:", "tags:")):
            continue
        if len(line) > LINE_LIMIT:
            line = line[: LINE_LIMIT - 1] + "…"
        out.append(line)
    return "\n".join(out)


async def _get(http: aiohttp.ClientSession, api_base: str, repo: str, path: str, token: str) -> str:
    async with http.get(
        f"{api_base}/repos/{repo}/contents/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GitHub HTTP {resp.status} for {path}")
        return await resp.text()


async def notes_context(token: str, repo: str, paths: tuple[str, ...] | list[str],
                        api_base: str = GITHUB_API) -> str:
    """Never raises; returns 'none' when nothing is set up or nothing can be read."""
    if not (token and repo and paths):
        return "none"
    key = f"{api_base}|{repo}|{','.join(paths)}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as http:
            texts = await asyncio.gather(*(_get(http, api_base, repo, p, token) for p in paths))
    except Exception as e:
        log.warning("notes context unavailable: %s", e)
        return hit[1] if hit else "none"
    parts = []
    for path, text in zip(paths, texts):
        body = compress_index(text) if path.rsplit("/", 1)[-1] == "index.md" else text.strip()
        parts.append(f"=== {path} ===\n{body}")
    text = "\n\n".join(parts)
    if len(text) > TOTAL_LIMIT:
        text = text[: TOTAL_LIMIT - 20] + "\n…(cut)"
    _cache[key] = (time.monotonic(), text)
    log.info("notes context loaded (%s characters)", len(text))
    return text
