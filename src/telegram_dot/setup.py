"""Run locally by the account owner, never by an untrusted agent."""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
from telethon import TelegramClient

async def setup():
    os.umask(0o077)
    root = Path(os.environ.get("DOT_ROOT", "./runtime")).expanduser().resolve()
    folder = root / "telegram-session"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (folder / "work.session").exists():
        raise SystemExit("Session already exists; refusing to overwrite it.")
    api_id = int(input("Telegram API ID from my.telegram.org: "))
    api_hash = getpass.getpass("Telegram API hash (hidden): ")
    private = root / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    password = None
    if not (private / "pairing_key").exists():
        password = getpass.getpass("Create a separate MCP pairing password (at least 20 characters): ")
        if len(password) < 20:
            raise SystemExit("Password too short. No Telegram session was created. Run setup again.")
    client = TelegramClient(str(folder / "work"), api_id, api_hash)
    try:
        await client.start()
        me = await client.get_me()
        (folder / "api.json").write_text(json.dumps({"api_id": api_id, "api_hash": api_hash}))
        if password is not None:
            (private / "pairing_key").write_text(password)
        print("Set DOT_OWNER_ID=" + str(me.id))
        print("Setup complete. Keep runtime private; never commit it.")
    finally:
        await client.disconnect()

def main():
    parser = argparse.ArgumentParser(description="Create a private Telegram session locally. Never share the generated runtime directory.")
    parser.add_argument("--root", help="Private runtime directory (default: DOT_ROOT or ./runtime)")
    args = parser.parse_args()
    if args.root:
        os.environ["DOT_ROOT"] = args.root
    asyncio.run(setup())

