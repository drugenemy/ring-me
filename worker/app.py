"""Background Claude worker: runs Claude Code on the owner's own subscription.

Tasks come from the caller service (the owner said "do X" on a call). Each task runs
in a fresh clone of WORK_REPO with `claude -p`. Claude itself calls the owner with
call_me.py when it is done or needs a decision, and acts on the answer.
At the end the worker texts a short summary.

POST /tasks       {"task", "context", "source"} -> {"id"}   (Bearer WORKER_TOKEN)
POST /sessions    same body: open a claude.ai cloud session for the task and text the owner the link
GET  /tasks/{id}  status, result
GET  /health      no auth
"""
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("worker")

CALL_ME = os.environ.get("CALL_ME_PATH", "/app/skill/call_me.py")

PROMPT = """You are Claude, running as {owner}'s background worker on their own Claude subscription.
{owner} gave you this task by voice on a Telegram call with {assistant}, their phone assistant, so the wording may be rough. Work out what they mean.

TASK: {task}

The last lines of that call:
{context}

How to work:
- You are in a fresh clone of {repo}. If it has a CLAUDE.md, read it first and follow it. Clone other repos with git if you need them; the GitHub token here may only reach some repos.
- Nothing goes outward: no emails, posts, pull requests, deploys, payments or messages to anyone except {owner}. {push_rule}
- You can read and edit files, use git, and run the call-me script. Other commands are blocked. If the task needs more (running tests, installing things), do what you can and say what is left for a full session.
- When you are done, or blocked on something only {owner} can decide, call them: python3 {call_me} "<one short sentence>" --details "<facts they may ask about>" --question "<one decision, if you need one>" --session "worker" --wait
  Then act on the answer. Repeat until there is nothing more. Never call more than 3 times for one task.
- If the call is missed (exit code 2), finish what you safely can without the answer.
- Commit and push what you changed before you stop.
- End your final message with a 2-5 sentence plain summary of what you did and what is left. It is texted to {owner}.
{extra}"""

PUSH_DEFAULT = "In this repo you may commit and push to its default branch. In other repos push a feature branch only."
PUSH_BRANCH = "Push a new feature branch (ring-me/<short-name>) only, never the default branch."


# The worker runs unattended, so it gets a fixed list of tools instead of a free
# shell: read/edit files, git, and the call-me script. Anything else is refused
# automatically (no one is there to approve it). Add more with WORKER_EXTRA_TOOLS.
BASE_TOOLS = [
    "Read", "Grep", "Glob", "Edit", "Write", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git pull:*)", "Bash(git push:*)",
    "Bash(git clone:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git branch:*)",
    "Bash(git fetch:*)", "Bash(ls:*)",
    f"Bash(python3 {CALL_ME}:*)",
]


def allowed_tools() -> list[str]:
    extra = [t.strip() for t in os.environ.get("WORKER_EXTRA_TOOLS", "").split(";") if t.strip()]
    return BASE_TOOLS + extra


def clean_token(value: str) -> str:
    """Tokens pasted from a terminal often carry line breaks, spaces or quotes."""
    value = "".join(value.split())
    return value.strip("'\"")


def parse_cloud_output(out: str) -> tuple[str, str, str]:
    """Read `claude -p --output-format json --cloud ...` output. Returns (url, title, error)."""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("url"):
                return str(data["url"]), str(data.get("title") or ""), ""
            if data.get("ok") is False or data.get("error"):
                return "", "", str(data.get("error") or "unable to create the session")
    m = re.search(r"View:\s*(\S+)", out) or re.search(r"(https://claude\.ai/code/\S+)", out)
    if m:
        t = re.search(r"Created cloud session:\s*(.+)", out)
        return m.group(1), (t.group(1).strip() if t else ""), ""
    return "", "", ""


def token_shape(value: str) -> str:
    """Describe a token without revealing it: for the startup log."""
    raw = value or ""
    clean = clean_token(raw)
    has_quotes = '"' in raw or "'" in raw
    has_space = raw != "".join(raw.split())
    return (f"length {len(clean)} (raw {len(raw)}), starts with sk-ant-oat01-: {clean.startswith('sk-ant-oat01-')}, "
            f"had spaces/line breaks: {has_space}, had quotes: {has_quotes}")


SESSION_PROMPT = """{owner} asked for this by voice, through {assistant} (their phone assistant):

{task}

The last lines of that call:
{context}

Do the work fully, the way you would in any session with them. Follow CLAUDE.md if the repo has one.
{owner} is not watching this session. When you are done, or blocked on a decision only they can make, call them with the call-me skill (--wait, short plain reason, one question) if it is set up here, and act on the answer.
"""


@dataclass
class Task:
    id: str
    task: str
    context: str = ""
    source: str = ""
    kind: str = "work"  # "work": run here with claude -p; "session": open a claude.ai cloud session
    url: str = ""
    status: str = "queued"  # queued -> running -> done | failed
    result: str = ""
    error: str = ""
    cost_usd: float | None = None
    turns: int | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass(frozen=True)
class Settings:
    worker_token: str
    caller_url: str
    caller_token: str
    repo_url: str
    work_dir: Path
    timeout: int
    claude_bin: str
    port: int
    owner_name: str = "the owner"
    assistant_name: str = "Jessica"
    repo: str = ""
    push_default_branch: bool = False
    instructions: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        def need(name):
            v = os.environ.get(name, "").strip()
            if not v:
                raise SystemExit(f"Missing environment variable {name}.")
            return v
        need("CLAUDE_CODE_OAUTH_TOKEN")
        need("GITHUB_TOKEN")
        repo = need("WORK_REPO")
        return cls(
            worker_token=need("WORKER_TOKEN"),
            caller_url=need("CALLER_URL").rstrip("/"),
            caller_token=need("CALLER_TOKEN"),
            repo_url=os.environ.get("REPO_URL", f"https://github.com/{repo}.git"),
            work_dir=Path(os.environ.get("WORK_DIR", "/tmp/work")),
            timeout=int(os.environ.get("TASK_TIMEOUT", "2700")),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            port=int(os.environ.get("PORT", "8080")),
            owner_name=os.environ.get("OWNER_NAME", "").strip() or "the owner",
            assistant_name=os.environ.get("ASSISTANT_NAME", "").strip() or "Jessica",
            repo=repo,
            push_default_branch=os.environ.get("WORKER_PUSH_DEFAULT_BRANCH", "").strip().lower() in ("1", "true", "yes"),
            instructions=os.environ.get("WORKER_INSTRUCTIONS", "").strip(),
        )

    def prompt(self, template: str, task: str, context: str) -> str:
        return template.format(
            owner=self.owner_name, assistant=self.assistant_name, repo=self.repo, call_me=CALL_ME,
            push_rule=PUSH_DEFAULT if self.push_default_branch else PUSH_BRANCH,
            extra=f"\nMore rules from {self.owner_name}:\n{self.instructions}\n" if self.instructions else "",
            task=task, context=context or "(none)")


async def run_cmd(*args, cwd=None, env=None, timeout=None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class Worker:
    def __init__(self, s: Settings):
        self.s = s
        self.tasks: dict[str, Task] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._bg: set[asyncio.Task] = set()

    def submit(self, task: str, context: str, source: str, kind: str = "work") -> Task:
        t = Task(id="t_" + secrets.token_hex(5), task=task, context=context, source=source, kind=kind)
        self.tasks[t.id] = t
        if kind == "session":  # quick; never wait behind a long work task
            task_ = asyncio.ensure_future(self._process(t))
            self._bg.add(task_)
            task_.add_done_callback(self._bg.discard)
        else:
            self.queue.put_nowait(t.id)
        return t

    async def run(self) -> None:
        while True:
            await self._process(self.tasks[await self.queue.get()])

    async def _process(self, t: Task) -> None:
        t.status = "running"
        try:
            await self._run(t)
            t.status = "done"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task %s failed", t.id)
            t.status, t.error = "failed", f"{type(e).__name__}: {e}"[:500]
        t.finished_at = time.time()
        try:
            await self._notify(t)
        except Exception as e:
            log.warning("could not text the summary: %s", e)

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update({
            "GIT_ASKPASS": str(Path(__file__).with_name("askpass.sh")),
            "GIT_TERMINAL_PROMPT": "0",
            "CALLER_URL": self.s.caller_url,
            "CALLER_TOKEN": self.s.caller_token,
            "CALLER_SESSION_LABEL": "worker",
        })
        env.pop("WORKER_TOKEN", None)
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN"):
            if name in env:
                env[name] = clean_token(env[name])
        return env

    async def _run(self, t: Task) -> None:
        workdir = self.s.work_dir / t.id
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        env = self._env()
        code, _, err = await run_cmd("git", "clone", "--depth", "50", self.s.repo_url, str(workdir),
                                     env=env, timeout=300)
        if code != 0:
            raise RuntimeError(f"git clone failed: {err.strip()[-300:]}")
        for cfg in (("user.name", "Claude (worker)"), ("user.email", "noreply@anthropic.com")):
            await run_cmd("git", "config", *cfg, cwd=workdir, env=env, timeout=30)
        if t.kind == "session":
            await self._open_session(t, workdir, env)
            shutil.rmtree(workdir, ignore_errors=True)
            return
        prompt = self.s.prompt(PROMPT, t.task, t.context)
        try:
            code, out, err = await run_cmd(
                self.s.claude_bin, "-p", prompt, "--output-format", "json",
                "--permission-mode", "acceptEdits",
                "--allowedTools", ",".join(allowed_tools()),
                cwd=workdir, env=env, timeout=self.s.timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"ran out of time after {self.s.timeout // 60} minutes")
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            data = {}
        t.result = str(data.get("result") or "").strip()
        t.cost_usd = data.get("total_cost_usd")
        t.turns = data.get("num_turns")
        if code != 0 or data.get("is_error"):
            raise RuntimeError((t.result or err.strip()[-300:] or f"claude exited {code}")[:500])
        shutil.rmtree(workdir, ignore_errors=True)

    async def _open_session(self, t: Task, workdir: Path, env: dict) -> None:
        """Create a claude.ai cloud session (the kind the owner sees in the Claude app) for this repo."""
        prompt = self.s.prompt(SESSION_PROMPT, t.task, t.context)
        try:
            code, out, err = await run_cmd(
                self.s.claude_bin, "-p", "--output-format", "json", "--cloud", prompt,
                cwd=workdir, env=env, timeout=240,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("creating the session took too long")
        t.url, title, error = parse_cloud_output(out)
        if t.url:
            t.result = title or "New session"
            return
        detail = error or (err.strip() or out.strip())[-400:] or f"claude exited {code}"
        raise RuntimeError(detail)

    async def _notify(self, t: Task) -> None:
        if t.kind == "session":
            if t.status == "done":
                reason = "🚀 Session opened: " + t.task[:120]
                details = f"{t.url}\n\nIt is working now and will call you when it is done or needs you."
            else:
                reason = "⚠️ Could not open a session: " + t.task[:100]
                details = t.error
        elif t.status == "done":
            reason = "✅ Worker finished: " + t.task[:120]
            details = (t.result or "No summary.")[-3000:]
        else:
            reason = "⚠️ Worker failed: " + t.task[:120]
            details = t.error
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(
                f"{self.s.caller_url}/calls",
                json={"reason": reason[:300], "details": details[:4000], "mode": "notice", "session": "worker"},
                headers={"Authorization": f"Bearer {self.s.caller_token}"},
            ) as resp:
                if resp.status != 201:
                    raise RuntimeError(f"caller HTTP {resp.status}")


def make_app(worker: Worker, token: str) -> web.Application:
    @web.middleware
    async def auth(request, handler):
        if request.path == "/health":
            return await handler(request)
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(_):
        running = [t.id for t in worker.tasks.values() if t.status == "running"]
        return web.json_response({"ok": True, "running": running, "queued": worker.queue.qsize()})

    async def create(request):
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "body must be JSON"}, status=400)
        task = str(body.get("task", "")).strip() if isinstance(body, dict) else ""
        if not task or len(task) > 2000:
            return web.json_response({"error": "task is required (max 2000 characters)"}, status=400)
        kind = "session" if request.path == "/sessions" else "work"
        t = worker.submit(task, str(body.get("context", ""))[:6000], str(body.get("source", ""))[:60], kind)
        return web.json_response({"id": t.id, "status": t.status}, status=201)

    async def get(request):
        t = worker.tasks.get(request.match_info["id"])
        if t is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(asdict(t))

    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    app.add_routes([web.get("/health", health), web.post("/tasks", create),
                    web.post("/sessions", create), web.get("/tasks/{id}", get)])
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    worker = Worker(s)
    runner = web.AppRunner(make_app(worker, s.worker_token))
    await runner.setup()
    await web.TCPSite(runner, "::", s.port).start()  # "::" also serves Railway's private IPv6 network
    log.info("worker listening on :%s", s.port)
    log.info("Claude token: %s", token_shape(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")))
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
