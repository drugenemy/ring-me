# Background Claude worker: Claude Code CLI + git + a tiny Python API.
FROM node:22-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir "aiohttp>=3.9,<4"
RUN useradd -m worker
WORKDIR /app
COPY worker/ /app/worker/
COPY skill/call_me.py /app/skill/call_me.py
RUN chmod +x /app/worker/askpass.sh
# Runs as a normal user, with a fixed tool list (see ALLOWED_TOOLS in app.py).
USER worker
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["python", "-m", "worker.app"]
