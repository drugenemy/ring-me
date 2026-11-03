"""Settings that make ring-me work for anyone: names, time zone, notes repo, worker rules."""
import pytest

from caller.config import Settings
from caller.notes import render_record
from worker.app import PROMPT, Settings as WorkerSettings, allowed_tools

BASE_ENV = {"CALLER_TOKEN": "t" * 32, "TG_API_ID": "1", "TG_API_HASH": "h", "TG_SESSION": "s",
            "OWNER_TG_ID": "42", "ELEVENLABS_API_KEY": "k", "ELEVENLABS_AGENT_ID": "a"}


def test_defaults_need_no_personal_setup(monkeypatch):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.owner_tg_id == 42 and s.assistant_name == "Jessica" and s.timezone == "UTC"
    assert not s.notes_on and s.context_files == ()


def test_names_timezone_and_notes(monkeypatch):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("OWNER_NAME", "Sam")
    monkeypatch.setenv("ASSISTANT_NAME", "Ava")
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("NOTES_REPO", "sam/notes")
    monkeypatch.setenv("NOTES_DIR", "/inbox/calls/")
    monkeypatch.setenv("CONTEXT_FILES", "now.md, index.md,")
    s = Settings.from_env()
    assert (s.owner_name, s.assistant_name, s.notes_dir) == ("Sam", "Ava", "inbox/calls")
    assert s.notes_on and s.context_files == ("now.md", "index.md")
    assert s.tz.key == "Europe/Berlin"


def test_bad_timezone_fails_fast(monkeypatch):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus")
    with pytest.raises(SystemExit, match="TIMEZONE"):
        Settings.from_env()


def test_transcript_uses_configured_names():
    from datetime import datetime, timezone
    text = render_record(datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), "t", "call-out", "c_1", [],
                         [{"role": "claude", "text": "hi"}, {"role": "owner", "text": "yo"}], "Ava", "Sam")
    assert "**Ava:** hi" in text and "**Sam:** yo" in text and "(UTC)" in text


def test_worker_prompt_and_push_rule(monkeypatch):
    s = WorkerSettings(worker_token="w", caller_url="c", caller_token="t", repo_url="r", work_dir=None,
                       timeout=1, claude_bin="claude", port=0, owner_name="Sam", repo="sam/notes")
    text = s.prompt(PROMPT, "fix it", "")
    assert "Sam's background worker" in text and "sam/notes" in text and "feature branch" in text
    s2 = WorkerSettings(**{**s.__dict__, "push_default_branch": True, "instructions": "Use British spelling."})
    text2 = s2.prompt(PROMPT, "fix it", "")
    assert "default branch" in text2 and "Use British spelling." in text2
    monkeypatch.setenv("WORKER_EXTRA_TOOLS", "Bash(npm test:*); Bash(pytest:*)")
    assert allowed_tools()[-2:] == ["Bash(npm test:*)", "Bash(pytest:*)"]
