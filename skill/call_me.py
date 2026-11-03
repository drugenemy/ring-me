#!/usr/bin/env python3
"""Call (or text) the owner on Telegram from any Claude session. Standard library only.

    call_me.py "Build finished, 3 tests fail" --question "Ship anyway?" --wait
    call_me.py "Prod is down" --urgent --details "..." --wait
    call_me.py "FYI: export done" --text
    call_me.py --status c_1a2b3c4d5e6f --wait

Config: CALLER_URL and CALLER_TOKEN from the environment, or from the file
~/.config/ring-me/env (KEY=VALUE lines).

Exit codes: 0 = the owner answered (or the text was delivered without --wait),
            2 = no answer yet (missed, still waiting), 1 = error.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_FILE = Path.home() / ".config" / "ring-me" / "env"
ANSWERED = {"answered", "replied"}
FINAL = {"answered", "replied", "failed", "cancelled", "no_answer", "delivered"}


def load_config() -> tuple[str, str]:
    values = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    url = os.environ.get("CALLER_URL") or values.get("CALLER_URL", "")
    token = os.environ.get("CALLER_TOKEN") or values.get("CALLER_TOKEN", "")
    if not url or not token:
        sys.exit(f"call_me: set CALLER_URL and CALLER_TOKEN (env or {CONFIG_FILE}).")
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not url.startswith("https://") and not local:
        sys.exit("call_me: CALLER_URL must start with https:// (the token would travel in clear text).")
    return url.rstrip("/"), token


class Unreachable(Exception):
    pass


def request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"call_me: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise Unreachable(f"cannot reach {url}: {getattr(e, 'reason', e)}")


def poll(base: str, token: str, call_id: str) -> dict:
    """GET the call, riding out short network blips."""
    for attempt in range(5):
        try:
            return request("GET", f"{base}/calls/{call_id}", token)
        except Unreachable as e:
            if attempt == 4:
                sys.exit(f"call_me: {e}. Check later: call_me.py --status {call_id}")
            time.sleep(2 * (attempt + 1))


def describe(call: dict) -> str:
    out = [f"call {call['id']}: {call['status']}"]
    if call.get("answer"):
        out.append(f"Answer: {call['answer']}")
    if call.get("summary"):
        out.append(f"Summary: {call['summary']}")
    if call.get("transcript"):
        out.append("Transcript:")
        out += [f"  {t['role']}: {t['text']}" for t in call["transcript"]]
    if call.get("error") and call["status"] not in ANSWERED:
        out.append(f"Note: {call['error']}")
    if call["status"] == "missed":
        out.append("No pickup. A Telegram text was sent; the reply will show up here.")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reason", nargs="?", help="One or two short sentences: why you are calling.")
    p.add_argument("--details", default="", help="Facts Claude-on-the-phone may need to answer their questions.")
    p.add_argument("--question", default="", help="The one thing you need them to decide.")
    p.add_argument("--urgent", action="store_true", help="Ring twice if the first call is missed; mark the text URGENT.")
    p.add_argument("--text", action="store_true", help="Send a Telegram text instead of calling (the reply comes back).")
    p.add_argument("--notice", action="store_true", help="Send a Telegram text that needs no reply (FYI).")
    p.add_argument("--session", default=os.environ.get("CALLER_SESSION_LABEL", ""),
                   help="Short label for this session (repo / task).")
    p.add_argument("--wait", action="store_true", help="Block until there is an answer (or --timeout).")
    p.add_argument("--timeout", type=int, default=540, help="Seconds to wait with --wait (default 540).")
    p.add_argument("--status", metavar="ID", help="Check an earlier call instead of starting one.")
    p.add_argument("--json", action="store_true", help="Print raw JSON.")
    a = p.parse_args()

    base, token = load_config()
    if a.status:
        call_id = a.status
    else:
        if not a.reason:
            p.error("reason is required")
        if not a.session:
            a.session = Path.cwd().name
        try:
            created = request("POST", f"{base}/calls", token, {
                "reason": a.reason, "details": a.details, "question": a.question,
                "urgent": a.urgent, "mode": "notice" if a.notice else "text" if a.text else "call", "session": a.session,
            })
        except Unreachable as e:
            sys.exit(f"call_me: {e}")
        call_id = created["id"]
        print(f"call_me: started {call_id}", file=sys.stderr)

    call = poll(base, token, call_id)
    if a.wait:
        end = time.time() + a.timeout
        while call["status"] not in FINAL and time.time() < end:
            time.sleep(3)
            call = poll(base, token, call_id)

    print(json.dumps(call, ensure_ascii=False, indent=1) if a.json else describe(call))
    if call["status"] in ANSWERED or call["status"] == "delivered":
        return 0
    if call["status"] in ("failed", "cancelled"):
        return 1
    if not a.wait and call["status"] in ("queued", "sent") or a.notice:
        return 0
    if call["status"] not in FINAL:
        print(f"\nStill open. Check later: call_me.py --status {call_id} --wait", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
