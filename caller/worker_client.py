"""Hand work to the background Claude worker service."""
import aiohttp


async def _post(worker_url: str, token: str, path: str, task: str, context: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
        async with http.post(
            f"{worker_url}{path}",
            json={"task": task, "context": context, "source": "phone call"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 201:
                raise RuntimeError(f"worker HTTP {resp.status}: {str(body)[:200]}")
            return body["id"]


async def start_worker_task(worker_url: str, token: str, task: str, context: str) -> str:
    """Quick job done by the worker itself; it calls him back."""
    return await _post(worker_url, token, "/tasks", task, context)


async def open_worker_session(worker_url: str, token: str, task: str, context: str) -> str:
    """Open a full claude.ai cloud session for the task; he gets the link by text."""
    return await _post(worker_url, token, "/sessions", task, context)
