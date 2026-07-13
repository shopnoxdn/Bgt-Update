"""Shared logic for auto-changing a Telegram account's login email.

Provider: custom domain (MAIL_DOMAIN, e.g. sopnox.store) whose mail is
routed (catch-all) to a single Gmail inbox. The address used for each
account is exactly the last 7 digits of the phone number, e.g.
"1234567@sopnox.store" — no random suffix. Codes are read from the Gmail
inbox over IMAP using GMAIL_ADDRESS / GMAIL_APP_PASSWORD.

Key design decisions
────────────────────
* Accepts an optional pre-connected `client` (Telethon).  When the bot
  already has the account's session open it passes that client in; the
  function uses it directly and does NOT disconnect it when done.
  Without a client the function opens its own and does disconnect.
* FloodWaitError from SendVerifyEmailCodeRequest is caught; the function
  waits the required seconds (up to 10 min) then retries once.
* The midway-resend is removed to avoid triggering a second FloodWait.
  A single send + polling window is reliable enough with this provider.
"""

import os
import re as _re
import imaplib
import email as _email
import asyncio
import json
from telethon import TelegramClient, functions, types, errors

MAIL_DOMAIN         = os.environ.get('MAIL_DOMAIN', 'sopnox.store')
GMAIL_ADDRESS       = os.environ.get('GMAIL_ADDRESS', '')
GMAIL_APP_PASSWORD  = os.environ.get('GMAIL_APP_PASSWORD', '')
IMAP_HOST           = 'imap.gmail.com'


# ── OTP extraction ───────────────────────────────────────────────────────────

def _extract_code(text):
    """Return first standalone 5-7 digit number (the OTP)."""
    if not text:
        return None
    m = _re.search(r'(?<!\d)(\d{5,7})(?!\d)', text)
    return m.group(1) if m else None


def _strip_html(html):
    return _re.sub(r'<[^>]+>', ' ', html or '')


def _decode_header(value):
    try:
        parts = _email.header.decode_header(value or '')
        out = []
        for text, enc in parts:
            if isinstance(text, bytes):
                out.append(text.decode(enc or 'utf-8', errors='ignore'))
            else:
                out.append(text)
        return ''.join(out)
    except Exception:
        return value or ''


def _get_body_text(msg):
    """Extract plain-text (falling back to stripped HTML) body from an email.message.Message."""
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == 'attachment':
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='ignore')
            except Exception:
                continue
            if ctype == 'text/plain':
                texts.append(text)
            elif ctype == 'text/html':
                texts.append(_strip_html(text))
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or 'utf-8'
            text = payload.decode(charset, errors='ignore') if payload else (msg.get_payload() or '')
            if msg.get_content_type() == 'text/html':
                text = _strip_html(text)
            texts.append(text)
        except Exception:
            pass
    return '\n'.join(texts)


# ── Provider: custom domain (catch-all) → shared Gmail inbox via IMAP ───────

class _SopnoxInbox:
    """Reads verification codes for `<name>@<MAIL_DOMAIN>` out of a shared
    Gmail inbox that the domain's catch-all forwards all mail to."""

    def __init__(self, name: str):
        self.name    = name
        self.address = f'{name}@{MAIL_DOMAIN}'

    def check(self):
        if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
            return None
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(IMAP_HOST)
            conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            conn.select('INBOX')

            # Search for mail addressed to this specific alias, newest first.
            status, data = conn.search(None, 'TO', f'"{self.address}"')
            if status != 'OK':
                return None
            ids = data[0].split()
            if not ids:
                return None

            for msg_id in reversed(ids[-10:]):
                try:
                    status, msg_data = conn.fetch(msg_id, '(RFC822)')
                    if status != 'OK' or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = _email.message_from_bytes(raw)

                    subject = _decode_header(msg.get('Subject', ''))
                    code = _extract_code(subject)
                    if code:
                        return code

                    body = _get_body_text(msg)
                    code = _extract_code(body)
                    if code:
                        return code
                except Exception:
                    continue
        except Exception:
            return None
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
        return None


# ── Main entry point ─────────────────────────────────────────────────────────

async def change_email_for_number(
        phone, raw_phone, api_id, api_hash,
        sessions_dir, data_file,
        mail_user=None,
        log=None,
        max_wait_attempts=36,
        sleep_secs=5,
        existing_client=None):
    """Auto-changes a Telegram account's login email.

    Parameters
    ----------
    existing_client : telethon.TelegramClient, optional
        A pre-connected, authorised client for this account.  When supplied
        the function uses it directly and NEVER disconnects it.
        When None the function opens (and later closes) its own client.

    Returns
    -------
    dict  {'success': bool, 'message': str, 'email': str | None}
    """

    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    # ── Build the email name: exactly the last 7 digits, nothing else ────────
    digits = ''.join(c for c in raw_phone if c.isdigit())
    name   = digits[-7:] if len(digits) >= 7 else digits

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {'success': False,
                'message': 'GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets not configured.',
                'email': None}

    inbox   = _SopnoxInbox(name)
    address = inbox.address
    _log(f'📧 Email: {address}')

    # ── Prepare Telethon client ───────────────────────────────────────────────
    owns_client = existing_client is None
    if owns_client:
        session_path = os.path.join(sessions_dir, phone)
        client = TelegramClient(session_path, api_id, api_hash)
    else:
        client = existing_client

    try:
        if owns_client:
            await client.connect()
            if not await client.is_user_authorized():
                return {'success': False,
                        'message': 'Session not authorized',
                        'email': address}

        # ── Send verification email (with FloodWait handling) ─────────────────
        async def _send_code():
            await client(functions.account.SendVerifyEmailCodeRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                email=address
            ))

        _log(f'📤 Sending OTP to {address}…')
        try:
            await _send_code()
        except errors.FloodWaitError as e:
            wait = e.seconds
            if wait > 600:          # > 10 min — give up
                return {'success': False,
                        'message': f'Telegram rate limit: {wait}s wait required. Try later.',
                        'email': address}
            _log(f'⏱ Telegram rate limit — waiting {wait}s before retry…')
            await asyncio.sleep(wait + 2)
            try:
                await _send_code()
            except Exception as e2:
                return {'success': False,
                        'message': f'OTP send failed after flood wait: {e2}',
                        'email': address}
        except errors.RPCError as e:
            # Map known Telegram errors to human-readable messages
            err = str(e)
            if 'EMAIL_UNCONFIRMED' in err:
                msg = ('পূর্বের email verify pending আছে — '
                       'কিছুক্ষণ পর retry করুন।')
            elif 'EMAIL_INVALID' in err:
                msg = 'Email address invalid।'
            elif 'EMAIL_VERIFY_EXPIRED' in err:
                msg = 'Verification code expired। আবার চেষ্টা করুন।'
            elif 'PHONE_NOT_OCCUPIED' in err:
                msg = 'Account টি আর active নেই।'
            elif 'AUTH_KEY_UNREGISTERED' in err or 'SESSION_REVOKED' in err:
                msg = 'Session invalid — account logout হয়ে গেছে।'
            elif 'USER_DEACTIVATED' in err:
                msg = 'Account banned/deactivated।'
            elif 'EMAIL_LOGIN_NOT_SUPPORTED' in err or 'FEATURE_DISABLED' in err:
                msg = 'এই account-এ email login feature সাপোর্ট করে না।'
            else:
                msg = f'Telegram error: {err}'
            return {'success': False, 'message': msg, 'email': address}
        except Exception as e:
            return {'success': False,
                    'message': f'OTP send failed: {e}',
                    'email': address}

        # ── Poll inbox ────────────────────────────────────────────────────────
        _log('⏳ OTP sent! Scanning inbox…')
        otp_code = None

        for attempt in range(max_wait_attempts):
            await asyncio.sleep(sleep_secs)
            _log(f'🔍 Checking inbox… ({attempt + 1}/{max_wait_attempts})')

            try:
                otp_code = await asyncio.to_thread(inbox.check)
            except Exception as e:
                _log(f'⚠️ Inbox check error: {e}')

            if otp_code:
                _log(f'✅ OTP found: {otp_code}')
                break

            _log('📬 No code yet…')

        if not otp_code:
            return {
                'success': False,
                'message': (f'OTP not received within '
                            f'{max_wait_attempts * sleep_secs}s. '
                            f'Try manual method.'),
                'email': address
            }

        # ── Verify with Telegram ──────────────────────────────────────────────
        _log('🔐 Verifying with Telegram…')
        try:
            await client(functions.account.VerifyEmailRequest(
                purpose=types.EmailVerifyPurposeLoginChange(),
                verification=types.EmailVerificationCode(code=otp_code)
            ))
        except Exception as e:
            return {'success': False,
                    'message': f'Verification failed: {e}',
                    'email': address}

        # ── Persist flag in user_data.json ────────────────────────────────────
        try:
            user_data_all = (json.load(open(data_file))
                             if os.path.exists(data_file) else {})
            for uid, info in user_data_all.items():
                for detail in info.get('processing_details', []):
                    if detail.get('number', '').replace('+', '').strip() == raw_phone:
                        detail['email_changed'] = True
                        detail['changed_email']  = address
                        break
            with open(data_file, 'w') as fw:
                json.dump(user_data_all, fw, indent=4)
        except Exception as e:
            _log(f'⚠️ Could not persist email_changed flag: {e}')

        _log(f'🎉 Done! Email changed to {address}')
        return {'success': True,
                'message': f'Email changed to {address}!',
                'email': address}

    except Exception as e:
        return {'success': False, 'message': str(e), 'email': address}
    finally:
        # Only disconnect if we opened the client ourselves
        if owns_client and client.is_connected():
            await client.disconnect()
