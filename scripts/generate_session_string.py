"""Interactive one-time helper to generate a Telethon StringSession for headless GitHub Actions."""

import asyncio
import os


async def main() -> None:
    print("=================================================================")
    print("Telethon StringSession Generator for GitHub Actions")
    print("=================================================================")
    print("This script logs into Telegram once and prints your session string.")
    print("You will paste this session string into GitHub Repository Secrets.")
    print("=================================================================\n")

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("Error: Telethon is required. Run: pip install telethon")
        return

    api_id_str = os.getenv("TELEGRAM_API_ID") or input("Enter your TELEGRAM_API_ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("Enter your TELEGRAM_API_HASH: ").strip()

    try:
        api_id = int(api_id_str)
    except ValueError:
        print("Error: API ID must be a valid integer.")
        return

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()

    session_string = client.session.save()
    print("\n" + "=" * 65)
    print("SUCCESS! Here is your TELEGRAM_SESSION_STRING:")
    print("=" * 65)
    print(session_string)
    print("=" * 65)
    print("\nNext steps:")
    print("1. Go to your GitHub repository -> Settings -> Secrets and variables -> Actions")
    print("2. Click 'New repository secret'")
    print("3. Name: TELEGRAM_SESSION_STRING")
    print("4. Value: (paste the string above)")
    print("5. Keep this string private - never commit it to public git!")
    print("=" * 65)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
