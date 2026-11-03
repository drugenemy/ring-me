# ring-me

**Your Claude Code sessions can phone you.** Say "call me when you're done" and walk away. When the work finishes, or when it hits a decision only you can make, your phone rings on Telegram. A voice assistant tells you what happened and asks the one question that matters. Your spoken answer goes straight back into the session, and the work carries on.

```
Claude session ──HTTP──> ring-me (your server) ──Telegram call──> your phone
                              │  ▲
                              ▼  │ live audio
                        ElevenLabs voice agent (any LLM, e.g. Claude Haiku)
```

- **Live calls, not robot alerts.** You can ask questions, push back, add instructions. The answer the session gets is what you said.
- **Missed it? You get a Telegram text instead.** Reply to it, and the reply becomes the answer.
- **Urgent calls ring twice**, one minute apart.
- **Call it back.** Optional: call the assistant yourself. It goes through every session waiting on you, takes voice notes, tells you where things stand (from your notes repo), hands quick jobs to a background Claude worker, or opens a full Claude cloud session for bigger work.
- **Everything is kept.** Optional: every call, text reply and worker result is saved as a markdown file in a GitHub repo you choose. This works well with an LLM-maintained wiki ([Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).
- **It can only ever call you.** One Telegram ID, fixed in settings. Nothing in the API can change who gets called.

## What you need

| | Cost |
|---|---|
| A spare phone number for a second Telegram account (the assistant). Bots can't make calls, so it has to be a real user account. | a SIM |
| An [ElevenLabs](https://elevenlabs.io) account (voice agent) | agent minutes on your plan; most calls are under a minute |
| A place to run a Docker container 24/7 ([Railway](https://railway.com) works out of the box) | ~$5/month |
| Claude Code (any plan) for the sessions that call you | — |

## Setup (about 20 minutes)

**1. The assistant's Telegram account.** Put the spare SIM in any phone, install Telegram and sign up. Give it a name and photo you will recognise when it rings.

**2. Telegram API keys for that account.** On https://my.telegram.org, log in with the spare number → *API development tools* → create an app → note `api_id` and `api_hash` (`TG_API_ID`, `TG_API_HASH`).

**3. Log in once and get the session string** (on your own computer):
```bash
pip install telethon
python login.py
```
It prints `TG_SESSION`. Keep it secret: anyone with it controls that account.

**4. Introduce the two accounts.** From your own Telegram, send the assistant's account any message, and save it as a contact. Make sure your privacy setting *Calls* allows your contacts.

**5. Your Telegram ID.** Message `@userinfobot` from your own account. It replies with your numeric ID → `OWNER_TG_ID`.

**6. The voice agent.** In ElevenLabs → *Agents*, create an agent and set it up as described in [`agent/prompt.md`](agent/prompt.md) (first message, prompt, `end_call` tool). Its ID → `ELEVENLABS_AGENT_ID`. Create an API key with access to Agents → `ELEVENLABS_API_KEY`.

**7. A token for your sessions.** `python -c "import secrets; print(secrets.token_urlsafe(32))"` → `CALLER_TOKEN`.

**8. Deploy.** On Railway: *New service → GitHub repo* (your fork of this one). It builds from the `Dockerfile`. Set the variables from [`.env.example`](.env.example). Optional: a volume at `/data` keeps call history across deploys. Generate a public domain; that URL is your `CALLER_URL`.
Run **one replica only**: two copies on one Telegram session break each other (`railway.json` already sets this).
Any other Docker host works too: one container, port `$PORT`, health check `/health`.

**9. Give Claude Code the skill.** Copy the [`skill/`](skill) folder to `~/.claude/skills/call-me/` (all your projects), or to `.claude/skills/call-me/` in one repo. Then put the connection details where the script can find them:
```bash
mkdir -p ~/.config/ring-me
printf 'CALLER_URL=https://your-app.up.railway.app\nCALLER_TOKEN=...\n' > ~/.config/ring-me/env
```
For Claude Code on the web, set `CALLER_URL` and `CALLER_TOKEN` as environment variables in your cloud environment instead.

**10. Test.**
```bash
python3 skill/call_me.py "Test call" --question "Can you hear me?" --wait
```
Your phone rings. Answer it, say something, and the script prints your answer.

Now, in any Claude Code session: *"refactor the billing module, run the tests, and call me when you're done."*

## Optional extras

### Make it yours
`OWNER_NAME` (how it greets you), `ASSISTANT_NAME` (default Jessica), `TIMEZONE` (an IANA name like `Europe/Berlin`).

### Calling in
Create a second ElevenLabs agent as described in [`agent/inbox-prompt.md`](agent/inbox-prompt.md) (it has four client tools) and set `ELEVENLABS_INBOUND_AGENT_ID`. Now when you call the assistant's account it picks up. If sessions are waiting for you, it goes through them first.

### Notes repo
Make a fine-grained GitHub token with access to one repo only, permission *Contents: Read and write*. Set `GITHUB_TOKEN`, `NOTES_REPO=you/your-notes`, and optionally:
- `NOTES_DIR` (default `raw/voice-notes`): where call records go.
- `CONTEXT_FILES` (comma-separated paths, for example `wiki/hot.md,wiki/index.md`): the inbound agent reads these during your call so it can answer "where is X at?". Files named `index.md` are cut to short lines to keep the call fast.

### Background worker (`worker/`)
A second service that runs Claude Code on your own subscription. On a call you say "check X" or "update Y", and the inbound agent's `start_task` tool hands it over. The worker clones `WORK_REPO`, runs `claude -p` with a fixed tool list (read/edit files, git, the call-me script; **no free shell**), and Claude calls you back with the result. `open_session` instead opens a full claude.ai cloud session and texts you the link.

Deploy it as a second Railway service from the same repo with `RAILWAY_DOCKERFILE_PATH=worker/Dockerfile`, and set:

| Variable | |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | run `claude setup-token` on your computer |
| `GITHUB_TOKEN` | fine-grained, only the repos it may touch, Contents read/write |
| `WORK_REPO` | `you/repo` it clones for each task |
| `WORKER_TOKEN` | random; set the same value on the caller |
| `CALLER_URL`, `CALLER_TOKEN` | so Claude can call you |
| `OWNER_NAME`, `ASSISTANT_NAME` | optional, same as the caller |
| `WORKER_PUSH_DEFAULT_BRANCH=1` | optional; by default it only pushes feature branches |
| `WORKER_EXTRA_TOOLS` | optional, `;`-separated, e.g. `Bash(npm test:*);Bash(pytest:*)` |
| `WORKER_INSTRUCTIONS` | optional, extra rules added to its prompt |

On the caller set `WORKER_URL=http://<worker-service>.railway.internal:8080` and `WORKER_TOKEN`.

## Using it from a session

```bash
python3 call_me.py "Build finished, 3 tests fail" --question "Ship anyway?" --wait
python3 call_me.py "Prod site is down since 14:05" --urgent --details "..." --wait
python3 call_me.py "Quick question: A or B?" --text
python3 call_me.py "FYI: export finished" --notice
python3 call_me.py --status c_1a2b3c4d5e6f --wait
```
With `--wait` it prints the answer and the transcript. Exit code 0 = answered, 2 = no answer yet, 1 = error. The skill's [`SKILL.md`](skill/SKILL.md) tells Claude when to call and when not to.

## API

| | |
|---|---|
| `POST /calls` | `{"reason", "details", "question", "urgent", "mode": "call"\|"text"\|"notice", "session"}` → `{"id", "status"}` |
| `GET /calls/{id}` | status, `answer`, `summary`, `transcript`, `first_word_ms` |
| `POST /calls/{id}/cancel` | stop a queued or ringing call |
| `GET /health` | no auth; shows which extras are on (never their values) |

All but `/health` need `Authorization: Bearer $CALLER_TOKEN`.
Statuses: `queued → ringing → talking → answered`, or `ringing → missed → replied | no_answer` (after 24 h), or for texts `sent → replied | no_answer`. Also `failed`, `cancelled`, `delivered` (notices).

## Risks, honestly

- **Telegram can ban accounts it thinks are automated.** Use a spare number, never your main one. Calling one known contact keeps the risk low, but it is not zero.
- **What you say is processed by ElevenLabs and your LLM provider**, and ElevenLabs keeps conversation history per your account settings. Don't let sessions put secrets in `reason` or `details`; the skill tells Claude this too.
- **The voice agent knows only what the session sent.** It can't look things up mid-call. Deep back-and-forth still belongs in the chat.
- **The worker runs unattended on your Claude subscription.** It has no free shell, and by default it only pushes feature branches. Widen it only on purpose.
- `TG_SESSION`, `ELEVENLABS_API_KEY`, `CALLER_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` and `GITHUB_TOKEN` are secrets. Host variables only; never in git.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```
Tests run full calls against a fake ElevenLabs server, a fake Telegram phone, a fake GitHub and a fake Claude CLI.

## License

MIT
