#!/usr/bin/env python3
"""Pre-flight environment and secrets validator for GitHub Actions and local runs."""

from __future__ import annotations

import os
import sys


def main() -> int:
    print("=" * 60)
    print("Quran Video Pipeline: Configuration & Secrets Validation")
    print("=" * 60)

    missing: list[str] = []

    # 1. ASR API Key
    asr_key = os.environ.get("ASR_API_KEY", "").strip()
    if not asr_key:
        print("::error title=Missing Secret::Secret 'ASR_API_KEY' is missing or empty! Please add your Groq API key in Settings -> Secrets and variables -> Actions.")
        missing.append("ASR_API_KEY")
    else:
        print(f"[OK] ASR_API_KEY is present (length: {len(asr_key)} chars).")

    # 2. Telegram API ID
    tg_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    if not tg_id:
        print("::error title=Missing Secret::Secret 'TELEGRAM_API_ID' is missing or empty! Please add your Telegram API ID in Settings -> Secrets and variables -> Actions.")
        missing.append("TELEGRAM_API_ID")
    else:
        print(f"[OK] TELEGRAM_API_ID is present.")

    # 3. Telegram API Hash
    tg_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not tg_hash:
        print("::error title=Missing Secret::Secret 'TELEGRAM_API_HASH' is missing or empty! Please add your Telegram API Hash in Settings -> Secrets and variables -> Actions.")
        missing.append("TELEGRAM_API_HASH")
    else:
        print(f"[OK] TELEGRAM_API_HASH is present.")

    # 4. Telegram Session String
    tg_session = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()
    if not tg_session:
        print(
            "::error title=Missing Secret::Secret 'TELEGRAM_SESSION_STRING' is missing or empty! "
            "In GitHub Actions, interactive login is impossible. Run 'python scripts/generate_session_string.py' "
            "on your computer to log in once, then paste the output string into Settings -> Secrets and variables -> Actions."
        )
        missing.append("TELEGRAM_SESSION_STRING")
    else:
        print(f"[OK] TELEGRAM_SESSION_STRING is present (length: {len(tg_session)} chars).")

    # 5. Target Channel
    channel = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    if not channel:
        print(
            "::error title=Channel Not Configured::TELEGRAM_CHANNEL_ID is not configured! "
            "Please set your channel handle (e.g. @moathemam) in Settings -> Secrets and variables -> Actions -> Variables, "
            "or enter it in the 'channel_id' input field when running the workflow."
        )
        missing.append("TELEGRAM_CHANNEL_ID")
    else:
        print(f"[OK] TELEGRAM_CHANNEL_ID is configured: '{channel}'.")

    # 6. Alignment Device
    align_device = os.environ.get("ALIGNMENT_DEVICE") or os.environ.get("CTC_ALIGNMENT_DEVICE", "cpu")
    print(f"[OK] Alignment device configured: '{align_device}'.")

    # Optional Telegram Alerts
    bot_token = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ALERT_TELEGRAM_CHAT_ID", "").strip()
    if bot_token and chat_id:
        print(f"[OK] Telegram Alerts configured (chat_id: {chat_id}).")
    else:
        print("[INFO] Telegram Alerts optional tokens not set (pipeline will use local logging).")

    print("=" * 60)

    if missing:
        print(f"::error::Missing {len(missing)} required secrets/variables: {', '.join(missing)}. Stopping workflow before execution.")
        return 1

    print("All required secrets and configurations are successfully verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
