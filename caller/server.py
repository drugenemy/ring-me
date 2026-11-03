"""HTTP API that Claude sessions use, plus the program entry point.

POST /calls          start a call or a text   -> {"id", "status"}
GET  /calls/{id}     state, answer, transcript
POST /calls/{id}/cancel
GET  /health         no auth; says if Telegram is connected
"""
import asyncio
import hmac
import logging
import signal

from aiohttp import web

from .store import Store

LIMITS = {"reason": 300, "details": 4000, "question": 500, "session": 120}


def make_app(token: str, store: Store, dialer, phone=None) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def health(_):
        ok = phone.connected if phone is not None else True
        s = getattr(dialer, "s", None)
        return web.json_response({
            "ok": ok,
            "busy": dialer.current is not None,
            # what is switched on (never the values)
            "calling_in": bool(s and s.elevenlabs_inbound_agent_id),
            "notes": bool(s and s.notes_on),
            "worker": bool(s and s.worker_url and s.worker_token),
        }, status=200 if ok else 503)

    async def create(request: web.Request):
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        fields = {}
        for name, limit in LIMITS.items():
            value = body.get(name, "")
            if not isinstance(value, str):
                return web.json_response({"error": f"{name} must be a string"}, status=400)
            value = value.strip()
            if len(value) > limit:
                return web.json_response({"error": f"{name} is longer than {limit} characters"}, status=400)
            fields[name] = value
        if not fields["reason"]:
            return web.json_response({"error": "reason is required"}, status=400)
        mode = body.get("mode", "call")
        if mode not in ("call", "text", "notice"):
            return web.json_response({"error": "mode must be 'call', 'text' or 'notice'"}, status=400)
        urgent = body.get("urgent", False)
        if not isinstance(urgent, bool):
            return web.json_response({"error": "urgent must be true or false"}, status=400)
        call = store.new(**fields, urgent=urgent, mode=mode)
        dialer.submit(call)
        return web.json_response({"id": call.id, "status": call.status}, status=201)

    async def get(request: web.Request):
        call = store.get(request.match_info["id"])
        if call is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(call.public())

    async def cancel(request: web.Request):
        call = store.get(request.match_info["id"])
        if call is None:
            return web.json_response({"error": "not found"}, status=404)
        dialer.cancel(call)
        return web.json_response(call.public())

    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/health", health),
        web.post("/calls", create),
        web.get("/calls/{id}", get),
        web.post("/calls/{id}/cancel", cancel),
    ])
    return app


async def main() -> None:
    from .config import Settings
    from .dialer import Dialer
    from .phone import Phone

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    store = Store(s.data_dir)
    phone = Phone(s.tg_api_id, s.tg_api_hash, s.tg_session, s.owner_tg_id)
    await phone.start()
    dialer = Dialer(s, phone, store)

    runner = web.AppRunner(make_app(s.caller_token, store, dialer, phone))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", s.port).start()
    logging.info("listening on :%s (calling in: %s, notes: %s, worker: %s)", s.port,
                 bool(s.elevenlabs_inbound_agent_id), s.notes_on, bool(s.worker_url and s.worker_token))

    # On redeploy Railway sends SIGTERM. Disconnect cleanly so the old and new
    # containers never hold the Telegram session at the same time for long.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    work = [asyncio.create_task(dialer.run()),
            asyncio.create_task(phone.client.run_until_disconnected())]
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait([*work, stopper], return_when=asyncio.FIRST_COMPLETED)
    logging.info("shutting down")
    await phone.hang_up()
    for task in work:
        task.cancel()
    await phone.client.disconnect()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
