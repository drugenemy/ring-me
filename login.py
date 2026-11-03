"""Log in the spare Telegram account (the assistant) once and print its session string.

Run on your own computer (not in a cloud session):
    pip install telethon
    python login.py

It asks for the API ID/hash, the phone number of the spare SIM, the code Telegram
sends, and the 2FA password if set. Copy the printed string into Railway as
TG_SESSION. Treat it like a password: anyone with it controls that account.
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    api_id = int(input("TG_API_ID: ").strip())
    api_hash = input("TG_API_HASH: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # prompts for phone, code, 2FA password
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} (id {me.id}).")
    print("\nTG_SESSION (keep secret):\n")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
