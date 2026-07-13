# TG-Lion Project

## Project Overview
This project is a Telegram Bot and Web Dashboard for account trading and balance management.

### Features
- Sell Telegram accounts with admin approval.
- Buy accounts (work in progress).
- Balance management and withdrawals.
- Referral system.
- Web dashboard with real Telegram OTP login.
- Admin panel to manage users and processing numbers.

## Running the project
- Two workflows: `Web App Dashboard` runs `python web_app.py` (Flask, port 5000) and `Telegram Bot` runs `python main.py` (python-telegram-bot + Telethon polling).
- Dependencies (flask, flask-sqlalchemy, gunicorn, psycopg2-binary, telethon, python-telegram-bot, etc.) are installed via `requirements.txt`/`pyproject.toml`.
- Telegram credentials (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `BOT_TOKEN`, `ADMIN_CHAT_ID`, 2FA passwords) currently fall back to hardcoded defaults in `main.py` if the matching env vars aren't set — move these to secrets before going to production.
- Data persistence is currently JSON files (`user_data.json`, `countries_data.json`, `withdrawal_settings.json`, `broadcast_queue.json`) rather than a database, despite `flask-sqlalchemy`/`psycopg2-binary` being installed.

## Web App Login Update (Feb 2026)
- Implemented real Telegram OTP login for the web dashboard.
- Users must now provide their History ID followed by their Telegram phone number.
- The system connects to Telegram via Telethon, sends a real OTP, and creates a session upon successful verification.
- This ensures that only the actual owner of the Telegram account can access the dashboard.
- Added `telethon` and `flask[async]` dependencies.
- Created `sessions/` directory to store Telethon session files.
