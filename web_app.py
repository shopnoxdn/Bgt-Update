import os
import io
import zipfile
import json
import hashlib
import asyncio
import threading
import time
import uuid
import urllib.request as _urlreq
import re as _re
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort
from telethon import TelegramClient, errors, functions, types
from email_changer import change_email_for_number

# ── Background auto-email tasks ──────────────────────────────────────────────
auto_tasks: dict = {}   # task_id -> {status, logs, result, email}
join_tasks: dict = {}   # task_id -> {status, total, done, results}
bulk_email_tasks: dict = {}  # task_id -> {status, total, done, results}
bulk_logout_tasks: dict = {}  # task_id -> {status, total, done, results}
bulk_2fa_tasks: dict = {}    # task_id -> {status, total, done, results, action}

def _auto_email_thread(task_id: str, phone: str, raw_phone: str, mail_user: str):
    """Runs in a daemon thread with its own asyncio event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _auto_email_logic(task_id, phone, raw_phone, mail_user))
    finally:
        loop.close()

async def _auto_email_logic(task_id, phone, raw_phone, mail_user):
    """Thin wrapper: delegates the actual mail.tm/OTP work to the shared
    email_changer module (also used by the Telegram bot for post-login
    auto email changes), and reports progress into auto_tasks for polling."""
    def log(msg):
        auto_tasks[task_id]['logs'].append(msg)

    result = await change_email_for_number(
        phone, raw_phone, API_ID, API_HASH, SESSIONS_DIR, DATA_FILE,
        mail_user=mail_user, log=log)

    if result.get('email'):
        auto_tasks[task_id]['email'] = result['email']

    if result['success']:
        auto_tasks[task_id].update(status='done', result=result['message'], email=result['email'])
    else:
        auto_tasks[task_id].update(status='error', result=result['message'])
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'bgt-wallet-admin-2026-fixed-key')

@app.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# Telegram API for UserSession
API_ID = int(os.environ.get("TELEGRAM_API_ID", "31955122"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "4f3e7f6d8250dc14c21ae58642fcbcc9")

DATA_FILE = 'user_data.json'
COUNTRIES_FILE = 'countries_data.json'
SESSIONS_DIR = 'sessions'


def load_countries():
    if os.path.exists(COUNTRIES_FILE):
        try:
            with open(COUNTRIES_FILE, 'r') as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def save_countries(data):
    with open(COUNTRIES_FILE, 'w') as f:
        json.dump(data, f, indent=4)

if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# Global dictionary to store pending clients
pending_clients = {}

# Global dictionary to store pending email verifications
email_verification_sessions = {}

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def get_user_id_from_login_id(login_id, data):
    """Maps 15-char login ID back to Telegram user ID"""
    for user_id in data:
        expected_login_id = hashlib.md5(str(user_id).encode()).hexdigest()[:15].upper()
        if expected_login_id == login_id:
            return str(user_id)
    return None

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    login_id = request.form.get('user_id', '').strip().upper()
    data = load_data()
    
    # Check if the input is a 15-char MD5-based login ID
    user_id = get_user_id_from_login_id(login_id, data)
    
    # Fallback to direct user_id check (for backward compatibility/admin)
    if not user_id:
        # Check if the login_id matches the admin ID directly
        if login_id == '2876886938':
            user_id = '2876886938'
        elif login_id in data:
            user_id = login_id

    if user_id:
        session['user_id'] = user_id
        return jsonify({'success': True, 'user_id': user_id, 'redirect': '/dashboard'})
    
    return jsonify({'success': False, 'message': "Invalid ID"}), 401

@app.route('/request_otp', methods=['POST'])
async def request_otp():
    phone = request.json.get('phone', '').strip()
    if not phone:
        return jsonify({'success': False, 'message': 'Phone number required'}), 400
    
    session_path = os.path.join(SESSIONS_DIR, f"{phone}")
    client = TelegramClient(session_path, API_ID, API_HASH)
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
            sent_code = await client.send_code_request(phone)
            pending_clients[phone] = {
                'client': client,
                'phone_code_hash': sent_code.phone_code_hash
            }
            return jsonify({'success': True, 'message': 'OTP sent successfully'})
        else:
            # If already authorized, we still need to know which user_id this is for the session
            # For simplicity, we'll let the frontend handle the dashboard redirect
            await client.disconnect()
            return jsonify({'success': True, 'message': 'Already logged in', 'authorized': True})
    except Exception as e:
        if client.is_connected():
            await client.disconnect()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    phone = request.json.get('phone', '').strip()
    otp = request.json.get('otp', '').strip()
    user_id = request.json.get('user_id', '').strip() # Passed from frontend
    
    if phone not in pending_clients:
        return jsonify({'success': False, 'message': 'Session expired or not found'}), 400
    
    client_data = pending_clients[phone]
    client = client_data['client']
    phone_code_hash = client_data['phone_code_hash']
    
    try:
        await client.sign_in(phone, otp, phone_code_hash=phone_code_hash)
        # Login successful — but the phone that just verified via OTP proves
        # nothing about which dashboard account should be unlocked. Only grant
        # access to `user_id` if this exact phone number is actually one of
        # that account's own sold numbers; otherwise anyone with their own
        # phone could pass the OTP check and enter someone else's dashboard.
        del pending_clients[phone]
        await client.disconnect()

        data = load_data()
        digits_only = ''.join(c for c in phone if c.isdigit())
        account = data.get(user_id, {})
        owned_numbers = {
            ''.join(c for c in n.get('number', '') if c.isdigit())
            for n in account.get('processing_details', [])
        } | {''.join(c for c in n if c.isdigit()) for n in account.get('sold_numbers', [])}

        if user_id != '2876886938' and digits_only not in owned_numbers:
            return jsonify({'success': False,
                             'message': 'This phone number is not linked to that History ID.'}), 403

        # Now set the flask session
        session['user_id'] = user_id
        return jsonify({'success': True, 'message': 'Login successful'})
    except errors.SessionPasswordNeededError:
        return jsonify({'success': False, 'needs_password': True, 'message': 'Two-step verification enabled'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user_id = session['user_id']
    data = load_data()
    user_info = data.get(user_id, {})
    
    # Real data from user_data.json
    processing_details = user_info.get('processing_details', [])
    processed_numbers = []
    
    # Add from processing_details (the real source of truth now)
    for item in processing_details:
        status = item.get('status', 'Processing')
        # Filter: Only show if status is one of the valid ones (this handles the "only show when confirmed" logic)
        if status in ['Processing', 'Successful', 'Reject']:
            timestamp_str = item.get('timestamp', '')
            countdown = ""
            if status == 'Processing' and timestamp_str:
                try:
                    start_time = datetime.fromisoformat(timestamp_str)
                    now = datetime.now()
                    elapsed = now - start_time
                    total_allowed = 38 * 3600 # 38 hours
                    
                    remaining_seconds = total_allowed - elapsed.total_seconds()
                    
                    # Auto-extension logic: if 38 hours passed, add another 38 hours
                    while remaining_seconds < 0:
                        total_allowed += 38 * 3600
                        remaining_seconds = total_allowed - elapsed.total_seconds()
                    
                    hours = int(remaining_seconds // 3600)
                    minutes = int((remaining_seconds % 3600) // 60)
                    countdown = f"{hours}h {minutes}m"
                except:
                    countdown = "N/A"

            processed_numbers.append({
                'number': item.get('number', 'N/A'),
                'status': status,
                'price': f"{item.get('price', 0.0):.2f} USD",
                'country': item.get('country', 'N/A'),
                'date': item.get('timestamp', 'N/A').split('T')[0] if 'T' in item.get('timestamp', '') else item.get('timestamp', 'N/A'),
                'raw_timestamp': item.get('timestamp', ''),
                'countdown': countdown
            })
    
    processed_numbers.sort(key=lambda x: x['raw_timestamp'] if x['raw_timestamp'] else '', reverse=True)
    
    main_bal = user_info.get('main_balance_usdt', 0.0)
    hold_bal = user_info.get('hold_balance_usdt', 0.0)
    wd_bal = user_info.get('withdrawal_processing_balance', 0.0)
    
    balance = {
        'main': main_bal,
        'hold': hold_bal,
        'withdrawal': wd_bal,
        'total': main_bal + hold_bal + wd_bal
    }
    
    processing_count = sum(1 for n in processed_numbers if n['status'] == 'Processing')
    success_count = sum(1 for n in processed_numbers if n['status'] == 'Successful')
    reject_count = sum(1 for n in processed_numbers if n['status'] == 'Reject')
    
    accounts_sold = user_info.get('accounts_sold', 0)
    referral_count = len(user_info.get('referrals', []))
    referral_earnings = user_info.get('referral_earnings', 0.0)
    
    created_at = user_info.get('created_at', 'N/A')
    if 'T' in str(created_at):
        joined_date = created_at.split('T')[0]
    else:
        joined_date = str(created_at)
    
    last_activity = user_info.get('last_activity', 'N/A')
    if 'T' in str(last_activity):
        last_activity = last_activity.split('T')[0]
    
    return render_template('dashboard.html',
        numbers=processed_numbers,
        balance=balance,
        processing_count=processing_count,
        success_count=success_count,
        reject_count=reject_count,
        accounts_sold=accounts_sold,
        referral_count=referral_count,
        referral_earnings=referral_earnings,
        joined_date=joined_date,
        last_activity=last_activity
    )

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))

@app.route('/admin')
def admin_panel():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    data = load_data()
    stats = {
        'total_users': len(data),
        'processing': 0,
        'successful': 0,
        'rejected': 0,
        'total_balance': 0.0
    }
    for uid, info in data.items():
        stats['total_balance'] += info.get('main_balance_usdt', 0.0)
        for detail in info.get('processing_details', []):
            status = detail.get('status', '')
            if status == 'Processing':
                stats['processing'] += 1
            elif status == 'Successful':
                stats['successful'] += 1
            elif status == 'Reject':
                stats['rejected'] += 1
    
    message = request.args.get('message', '')
    return render_template('admin.html', stats=stats, message=message)

@app.route('/admin/countries', methods=['GET'])
def admin_countries():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    countries = load_countries()
    query = request.args.get('q', '').strip().lower()
    message = request.args.get('message', '')

    items = []
    for key, info in countries.items():
        name = info.get('name', key)
        if query and query not in name.lower() and query not in key.lower():
            continue
        items.append({
            'key': key,
            'name': name,
            'sell_price': info.get('sell_price', 0.0),
            'buy_price': info.get('buy_price', 0.0),
            'code': info.get('code', ''),
            'spam_off': info.get('spam_off', False),
        })
    items.sort(key=lambda x: x['name'].lower())

    file_exists = os.path.exists(COUNTRIES_FILE)
    return render_template(
        'admin_countries.html',
        items=items,
        query=query,
        message=message,
        total=len(countries),
        file_exists=file_exists,
    )


@app.route('/admin/countries/update', methods=['POST'])
def admin_countries_update():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    key = request.form.get('key', '').strip()
    sell_raw = request.form.get('sell_price', '').strip()
    buy_raw = request.form.get('buy_price', '').strip()
    code_raw = request.form.get('code', '').strip()
    query = request.form.get('q', '').strip()

    countries = load_countries()
    if key not in countries:
        return redirect(url_for('admin_countries', q=query, message=f'Country "{key}" not found.'))

    try:
        if sell_raw != '':
            countries[key]['sell_price'] = float(sell_raw)
        if buy_raw != '':
            countries[key]['buy_price'] = float(buy_raw)
    except ValueError:
        return redirect(url_for('admin_countries', q=query, message='Invalid price value.'))

    if code_raw:
        if not code_raw.startswith('+'):
            code_raw = '+' + code_raw.lstrip('+')
        countries[key]['code'] = code_raw

    save_countries(countries)
    name = countries[key].get('name', key)
    return redirect(url_for('admin_countries', q=query, message=f'Updated {name} successfully. Restart the bot to apply.'))


@app.route('/admin/countries/add', methods=['POST'])
def admin_countries_add():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip()
    sell_raw = request.form.get('sell_price', '').strip()

    if not name or not code or not sell_raw:
        return redirect(url_for('admin_countries', message='Please fill in name, country code and sell price.'))

    if not code.startswith('+'):
        code = '+' + code.lstrip('+')

    try:
        sell_price = float(sell_raw)
        if sell_price <= 0:
            raise ValueError()
    except ValueError:
        return redirect(url_for('admin_countries', message='Invalid sell price.'))

    import re as _re
    base = _re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    if not base:
        base = 'country'
    countries = load_countries()
    key = base
    suffix = 2
    while key in countries:
        key = f"{base}_{suffix}"
        suffix += 1

    buy_price = round(sell_price * 1.3, 2)
    countries[key] = {
        'name': name,
        'sell_price': sell_price,
        'buy_price': buy_price,
        'code': code,
    }
    save_countries(countries)
    return redirect(url_for('admin_countries', q=name, message=f'Added {name} ({code}). Restart the bot to apply.'))


@app.route('/admin/countries/toggle_spam', methods=['POST'])
def admin_countries_toggle_spam():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    key = request.form.get('key', '').strip()
    query = request.form.get('q', '').strip()
    countries = load_countries()
    if key not in countries:
        return redirect(url_for('admin_countries', q=query, message=f'Country not found.'))
    current = countries[key].get('spam_off', False)
    countries[key]['spam_off'] = not current
    save_countries(countries)
    name = countries[key].get('name', key)
    status = 'OFF (spam blocked)' if countries[key]['spam_off'] else 'ON (spam allowed)'
    return redirect(url_for('admin_countries', q=query, message=f'Spam purchase for {name} is now {status}.'))


@app.route('/admin/countries/delete', methods=['POST'])
def admin_countries_delete():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    key = request.form.get('key', '').strip()
    query = request.form.get('q', '').strip()
    countries = load_countries()
    if key in countries:
        name = countries[key].get('name', key)
        del countries[key]
        save_countries(countries)
        return redirect(url_for('admin_countries', q=query, message=f'Deleted {name}.'))
    return redirect(url_for('admin_countries', q=query, message='Country not found.'))


@app.route('/admin/download_countries')
def admin_download_countries():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    file_path = os.path.join(os.getcwd(), 'countries_data.json')
    if not os.path.exists(file_path):
        return redirect(url_for('admin_panel', message='countries_data.json file not found yet. Change a price from the bot first.'))

    return send_file(
        file_path,
        as_attachment=True,
        download_name='countries_data.json',
        mimetype='application/json'
    )

@app.route('/admin/search', methods=['POST'])
def admin_search():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    search_id = request.form.get('chat_id', '').strip()
    data = load_data()
    user_info = data.get(search_id, {})
    
    processed_numbers = []
    stats = {'processing': 0, 'successful': 0, 'reject': 0}
    
    if user_info:
        processing_details = user_info.get('processing_details', [])
        for item in processing_details:
            status = item.get('status', 'Processing')
            processed_numbers.append({
                'number': item.get('number', 'N/A'),
                'status': status,
                'price': f"{item.get('price', 0.0):.2f} USD",
                'country': item.get('country', 'N/A'),
                'date': item.get('timestamp', 'N/A').split('T')[0] if 'T' in item.get('timestamp', '') else item.get('timestamp', 'N/A')
            })
            
            if status == 'Processing':
                stats['processing'] += 1
            elif status == 'Successful':
                stats['successful'] += 1
            elif status == 'Reject':
                stats['reject'] += 1
    
    user_balance = {
        'main': user_info.get('main_balance_usdt', 0.0),
        'hold': user_info.get('hold_balance_usdt', 0.0)
    }
    return render_template('admin_results.html', numbers=processed_numbers, search_id=search_id, stats=stats, user_balance=user_balance)

@app.route('/admin/notify', methods=['POST'])
def admin_notify():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    notify_type = request.form.get('type', 'all')
    message = request.form.get('message', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    
    if message:
        queue = []
        if os.path.exists('broadcast_queue.json'):
            try:
                with open('broadcast_queue.json', 'r') as f:
                    queue = json.load(f)
                    if not isinstance(queue, list):
                        queue = []
            except:
                queue = []
        
        notification = {
            'type': notify_type,
            'message': message,
            'chat_id': chat_id if notify_type == 'custom' else None,
            'timestamp': datetime.now().isoformat()
        }
        queue.append(notification)
            
        with open('broadcast_queue.json', 'w') as f:
            json.dump(queue, f)
            
    return redirect(url_for('admin_panel'))

@app.route('/admin/set_link', methods=['POST'])
def admin_set_link():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    link = request.form.get('dashboard_link', '').strip()
    if link:
        settings = {}
        if os.path.exists('settings.json'):
            with open('settings.json', 'r') as f:
                settings = json.load(f)
        settings['dashboard_link'] = link
        with open('settings.json', 'w') as f:
            json.dump(settings, f)
            
    return redirect(url_for('admin_panel'))

@app.route('/admin/users')
def admin_users():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    data = load_data()
    users = []
    for uid, info in data.items():
        users.append({
            'chat_id': uid,
            'balance': info.get('main_balance_usdt', 0.0),
            'hold_balance': info.get('hold_balance_usdt', 0.0),
            'sold': info.get('accounts_sold', 0),
            'referrals': info.get('referral_count', 0)
        })
    users.sort(key=lambda x: x['balance'], reverse=True)
    return render_template('admin_list.html', title="User List", items=users, type='users')

@app.route('/admin/processing')
def admin_processing():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    data = load_data()
    items = []
    now = datetime.now()
    for uid, info in data.items():
        for detail in info.get('processing_details', []):
            if detail.get('status') == 'Processing':
                ts = detail.get('timestamp', '')
                elapsed_str = "N/A"
                if ts:
                    try:
                        elapsed = now - datetime.fromisoformat(ts)
                        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
                        minutes, seconds = divmod(remainder, 60)
                        elapsed_str = f"{hours}h {minutes}m {seconds}s"
                    except: pass
                items.append({
                    'chat_id': uid,
                    'number': detail.get('number'),
                    'country': detail.get('country'),
                    'time': elapsed_str,
                    'price': detail.get('price', 0.0)
                })
    return render_template('admin_list.html', title="Processing Numbers", items=items, type='processing')

@app.route('/admin/successful')
def admin_successful():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    data = load_data()
    items = []
    for uid, info in data.items():
        for detail in info.get('processing_details', []):
            if detail.get('status') == 'Successful':
                items.append({
                    'chat_id': uid,
                    'number': detail.get('number'),
                    'country': detail.get('country'),
                    'price': detail.get('price', 0.0),
                    'date': detail.get('timestamp', '').split('T')[0] if 'T' in detail.get('timestamp', '') else 'N/A'
                })
    return render_template('admin_list.html', title="Successful Numbers", items=items, type='successful')

@app.route('/admin/rejected')
def admin_rejected():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    data = load_data()
    items = []
    for uid, info in data.items():
        for detail in info.get('processing_details', []):
            if detail.get('status') == 'Reject':
                items.append({
                    'chat_id': uid,
                    'number': detail.get('number'),
                    'country': detail.get('country'),
                    'price': detail.get('price', 0.0),
                    'date': detail.get('timestamp', '').split('T')[0] if 'T' in detail.get('timestamp', '') else 'N/A'
                })
    return render_template('admin_list.html', title="Rejected Numbers", items=items, type='rejected')

@app.route('/admin/withdrawals')
def admin_withdrawals():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    data = load_data()
    items = []
    for uid, info in data.items():
        wd_processing = info.get('withdrawal_processing_balance', 0.0)
        if wd_processing > 0:
            items.append({
                'chat_id': uid,
                'method': 'USDT',
                'amount': wd_processing,
                'date': info.get('last_activity', 'N/A').split('T')[0] if 'T' in info.get('last_activity', '') else 'N/A',
                'status': 'Processing'
            })
    return render_template('admin_list.html', title="Withdrawal History", items=items, type='withdrawals')

@app.route('/admin/search_processing', methods=['POST'])
def admin_search_processing():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    search_id = request.form.get('chat_id', '').strip()
    data = load_data()
    user_info = data.get(search_id, {})
    
    items = []
    now = datetime.now()
    
    for detail in user_info.get('processing_details', []):
        if detail.get('status') == 'Processing':
            ts = detail.get('timestamp', '')
            elapsed_str = "N/A"
            hours_elapsed = 0
            date_str = "N/A"
            if ts:
                try:
                    start_time = datetime.fromisoformat(ts)
                    elapsed = now - start_time
                    total_seconds = int(elapsed.total_seconds())
                    hours, remainder = divmod(total_seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    elapsed_str = f"{hours}h {minutes}m {seconds}s"
                    hours_elapsed = hours
                    date_str = ts.split('T')[0] if 'T' in ts else ts
                except:
                    pass
            
            items.append({
                'number': detail.get('number', 'N/A'),
                'country': detail.get('country', 'N/A'),
                'price': detail.get('price', 0.0),
                'date': date_str,
                'elapsed': elapsed_str,
                'hours_elapsed': hours_elapsed
            })
    
    return render_template('admin_processing_search.html', items=items, search_id=search_id)

@app.route('/admin/check_balance', methods=['POST'])
def admin_check_balance():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    search_id = request.form.get('chat_id', '').strip()
    data = load_data()
    user_info = data.get(search_id, {})
    
    if not user_info:
        return render_template('admin_balance.html', found=False, search_id=search_id)
    
    main_bal = user_info.get('main_balance_usdt', 0.0)
    hold_bal = user_info.get('hold_balance_usdt', 0.0)
    wd_processing = user_info.get('withdrawal_processing_balance', 0.0)
    
    balances = {
        'main': main_bal,
        'hold': hold_bal,
        'withdrawal_processing': wd_processing,
        'total': main_bal + hold_bal + wd_processing
    }
    
    extra = {
        'accounts_sold': user_info.get('accounts_sold', 0),
        'referral_count': user_info.get('referral_count', 0),
        'referral_earnings': user_info.get('referral_earnings', 0.0),
        'last_activity': user_info.get('last_activity', 'N/A')
    }
    
    return render_template('admin_balance.html', found=True, search_id=search_id, balances=balances, extra=extra)

@app.route('/admin/reset_number', methods=['POST'])
def admin_reset_number():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    phone = request.form.get('phone_number', '').strip()
    if not phone:
        return redirect(url_for('admin_panel', message='Phone number is required'))
    
    data = load_data()
    found = False
    for uid, info in data.items():
        sold = info.get('sold_numbers', [])
        if phone in sold:
            info['sold_numbers'] = [n for n in sold if n != phone]
            found = True
        
        pd = info.get('processing_details', [])
        new_pd = [d for d in pd if d.get('number') != phone]
        if len(new_pd) != len(pd):
            info['processing_details'] = new_pd
            found = True
    
    if found:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
        return redirect(url_for('admin_panel', message=f'Number {phone} has been reset and can be re-sold'))
    
    return redirect(url_for('admin_panel', message=f'Number {phone} not found in any user data'))

@app.route('/admin/approve', methods=['POST'])
def admin_approve():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    chat_id = request.form.get('chat_id')
    number = request.form.get('number')
    action = request.form.get('action') # 'approve' or 'reject'
    
    data = load_data()
    if chat_id in data:
        user_info = data[chat_id]
        processing_details = user_info.get('processing_details', [])
        
        for item in processing_details:
            if item.get('number') == number and item.get('status') == 'Processing':
                if action == 'approve':
                    item['status'] = 'Successful'
                    # Update balance and counts
                    price = item.get('price', 0.0)
                    user_info['main_balance_usdt'] = user_info.get('main_balance_usdt', 0.0) + price
                    user_info['accounts_sold'] = user_info.get('accounts_sold', 0) + 1
                else:
                    item['status'] = 'Reject'
                break
        
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
            
    return redirect(request.referrer or url_for('admin_panel'))

@app.route('/admin/force_logout', methods=['POST'])
def admin_force_logout():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    
    phone = request.form.get('phone_number', '').strip()
    if not phone:
        return redirect(url_for('admin_panel', message='Phone number is required'))
    
    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    session_removed = False
    
    if os.path.exists(session_path):
        try:
            os.remove(session_path)
            session_removed = True
        except Exception as e:
            return redirect(url_for('admin_panel', message=f'Error removing session: {str(e)}'))
    
    data = load_data()
    number_blocked = False
    phone_variants = [phone]
    if phone.startswith('+'):
        phone_variants.append(phone[1:])
    else:
        phone_variants.append('+' + phone)
    
    for uid, info in data.items():
        sold = info.get('sold_numbers', [])
        already_sold = any(p in sold for p in phone_variants)
        if not already_sold:
            for variant in phone_variants:
                for detail in info.get('processing_details', []):
                    if detail.get('number', '').replace('+', '') == phone.replace('+', ''):
                        if variant not in sold:
                            sold.append(variant)
                            info['sold_numbers'] = sold
                            number_blocked = True
                        break
    
    if number_blocked:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    
    if session_removed and number_blocked:
        return redirect(url_for('admin_panel', message=f'{phone} logged out and blocked from re-selling.'))
    elif session_removed:
        return redirect(url_for('admin_panel', message=f'{phone} session removed. Number was already in sold list.'))
    elif number_blocked:
        return redirect(url_for('admin_panel', message=f'No session found, but {phone} has been blocked from re-selling.'))
    else:
        return redirect(url_for('admin_panel', message=f'No session found for {phone} and number already blocked.'))

def get_all_session_numbers():
    """Get all phone numbers that have session files, enriched with user data"""
    data = load_data()

    # Build lookup: normalized_phone -> {user_id, country, timestamp, status, price, email_changed}
    lookup = {}
    for uid, info in data.items():
        for d in info.get('processing_details', []):
            num = d.get('number', '').replace('+', '').strip()
            if num and num not in lookup:
                lookup[num] = {
                    'user_id': uid,
                    'country': d.get('country', 'N/A'),
                    'timestamp': d.get('timestamp', ''),
                    'status': d.get('status', ''),
                    'price': float(d['price']) if d.get('price') is not None else None,
                    'email_changed': d.get('email_changed', False),
                }

    results = []
    now = datetime.now()

    if os.path.exists(SESSIONS_DIR):
        for f in sorted(os.listdir(SESSIONS_DIR)):
            if not f.endswith('.session') or f.endswith('-journal'):
                continue
            session_name = f[:-8]
            # Strip sell_ prefix to get raw phone
            raw_phone = session_name.replace('sell_', '')
            display_phone = '+' + raw_phone if not raw_phone.startswith('+') else raw_phone

            info = lookup.get(raw_phone, {})

            # Duration since timestamp
            ts_str = info.get('timestamp', '')
            login_date = 'N/A'
            duration_str = 'N/A'
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str)
                    login_date = ts.strftime('%Y-%m-%d %H:%M')
                    diff = now - ts
                    total_s = int(diff.total_seconds())
                    days = total_s // 86400
                    hours = (total_s % 86400) // 3600
                    minutes = (total_s % 3600) // 60
                    if days > 0:
                        duration_str = f"{days}d {hours}h {minutes}m"
                    elif hours > 0:
                        duration_str = f"{hours}h {minutes}m"
                    else:
                        duration_str = f"{minutes}m"
                except Exception:
                    pass

            results.append({
                'session_name': session_name,
                'display_phone': display_phone,
                'country': info.get('country', 'N/A'),
                'user_id': info.get('user_id', 'N/A'),
                'login_date': login_date,
                'duration': duration_str,
                'price': info.get('price', None),
                'email_changed': info.get('email_changed', False),
                '_ts': ts_str,
            })

    return results


@app.route('/admin/active_numbers')
def admin_active_numbers():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))
    query = request.args.get('q', '').strip()
    message = request.args.get('message', '')
    all_numbers = get_all_session_numbers()
    # Sort by login timestamp ascending so oldest login = #1
    # Push missing timestamps to the end
    all_numbers.sort(key=lambda x: (not x.get('_ts'), x.get('_ts', '')))
    # Assign serial numbers based on full sorted list
    for i, n in enumerate(all_numbers, 1):
        n['serial'] = i
    if query:
        q = query.replace('+', '')
        filtered = [n for n in all_numbers if q in n['display_phone'].replace('+', '') or q.lower() in n['country'].lower()]
    else:
        filtered = all_numbers
    return render_template('admin_active_numbers.html', numbers=filtered, query=query, total=len(all_numbers), message=message)


@app.route('/admin/number/<path:phone>')
async def admin_number_detail(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return redirect(url_for('index'))

    session_path = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(session_path + '.session'):
        return redirect(url_for('admin_active_numbers', message=f'Session not found for {phone}'))

    client = TelegramClient(session_path, API_ID, API_HASH)
    sessions_list = []
    has_2fa = False
    me_info = {}
    error = None

    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            if me:
                me_info = {
                    'id': me.id,
                    'first_name': me.first_name or '',
                    'last_name': me.last_name or '',
                    'username': me.username or '',
                    'phone': me.phone or phone,
                }

            # Get authorized sessions
            try:
                auths = await client(functions.account.GetAuthorizationsRequest())
                for auth in auths.authorizations:
                    sessions_list.append({
                        'hash': auth.hash,
                        'device': getattr(auth, 'device_model', 'Unknown'),
                        'platform': getattr(auth, 'platform', ''),
                        'app_name': getattr(auth, 'app_name', ''),
                        'ip': getattr(auth, 'ip', ''),
                        'country': getattr(auth, 'country', ''),
                        'region': getattr(auth, 'region', ''),
                        'current': getattr(auth, 'current', False),
                        'date_created': auth.date_created.strftime('%Y-%m-%d %H:%M') if getattr(auth, 'date_created', None) else 'N/A',
                        'date_active': auth.date_active.strftime('%Y-%m-%d %H:%M') if getattr(auth, 'date_active', None) else 'N/A',
                    })
            except Exception as e:
                import logging
                logging.warning(f'[admin_number_detail] GetAuthorizations failed for {phone}: {e}')

            # Get 2FA status
            try:
                pwd = await client(functions.account.GetPasswordRequest())
                has_2fa = pwd.has_password
            except Exception as e:
                import logging
                logging.warning(f'[admin_number_detail] GetPassword failed for {phone}: {e}')
        else:
            error = 'Session expired or not authorized'
    except Exception as e:
        error = str(e)
    finally:
        if client.is_connected():
            await client.disconnect()

    return render_template('admin_number_detail.html',
        phone=phone,
        sessions=sessions_list,
        has_2fa=has_2fa,
        me=me_info,
        error=error
    )


@app.route('/admin/number/<path:phone>/terminate', methods=['POST'])
async def admin_terminate_session(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json()
    hash_val = data.get('hash') if data else None
    is_current = bool(data.get('current')) if data else False
    if hash_val is None:
        return jsonify({'success': False, 'message': 'Hash required'}), 400

    session_path = os.path.join(SESSIONS_DIR, phone)
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': False, 'message': 'Session not authorized'}), 400

        if is_current:
            # Telegram does not allow resetting the current authorization via
            # ResetAuthorizationRequest, so fully log out the bot's own session
            # instead. This disconnects the number from the bot entirely.
            try:
                await client.log_out()
            except Exception as logout_err:
                return jsonify({'success': False, 'message': f'Logout failed: {logout_err}'}), 500

            for ext in ('.session', '.session-journal'):
                fpath = session_path + ext
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

            return jsonify({
                'success': True,
                'message': 'Logged out from this phone. The number is now disconnected from the bot.',
                'redirect': url_for('admin_active_numbers', message=f'{phone} logged out and removed')
            })

        await client(functions.account.ResetAuthorizationRequest(hash=int(hash_val)))
        return jsonify({'success': True, 'message': 'Session terminated successfully'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


@app.route('/admin/number/<path:phone>/toggle_2fa', methods=['POST'])
async def admin_toggle_2fa(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json()
    action = data.get('action') if data else None
    password = (data.get('password', '') if data else '') or ''
    new_password = (data.get('new_password', '') if data else '') or ''
    hint = (data.get('hint', '') if data else '') or ''

    session_path = os.path.join(SESSIONS_DIR, phone)
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': False, 'message': 'Session not authorized'}), 400

        if action == 'disable':
            await client.edit_2fa(current_password=password, new_password='')
            return jsonify({'success': True, 'message': '2FA disabled successfully'})
        elif action == 'enable':
            await client.edit_2fa(new_password=new_password, hint=hint)
            return jsonify({'success': True, 'message': '2FA enabled successfully'})
        else:
            return jsonify({'success': False, 'message': 'Invalid action'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


@app.route('/admin/number/<path:phone>/quick_logout', methods=['POST'])
async def admin_quick_logout(phone):
    """Fully log out a session and remove it (used for N/A numbers from the list page)."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    session_path = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(session_path + '.session'):
        return jsonify({'success': False, 'message': 'Session file not found'}), 404

    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await client.connect()
        try:
            await client.log_out()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    for ext in ('.session', '.session-journal'):
        fpath = session_path + ext
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    return jsonify({'success': True, 'message': f'{phone} logged out and removed.'})


@app.route('/admin/number/<path:phone>/auto_change_email', methods=['POST'])
def admin_auto_change_email(phone):
    """Starts background email-change task, returns task_id immediately."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    raw_phone = phone.replace('sell_', '').replace('+', '').strip()
    digits_only = ''.join(c for c in raw_phone if c.isdigit())
    last7 = digits_only[-7:] if len(digits_only) >= 7 else digits_only
    mail_user = last7
    task_id = uuid.uuid4().hex[:10]
    auto_tasks[task_id] = {
        'status': 'running',
        'logs': [f'🔢 Phone suffix: {mail_user}'],
        'result': None,
        'email': '',
    }

    t = threading.Thread(
        target=_auto_email_thread,
        args=(task_id, phone, raw_phone, mail_user),
        daemon=True
    )
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/admin/number/<path:phone>/auto_change_email_status/<task_id>')
def admin_auto_change_email_status(phone, task_id):
    """Frontend polls this every 2 s to get live logs + final status."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = auto_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


@app.route('/admin/number/<path:phone>/send_email_otp', methods=['POST'])
async def admin_send_email_otp(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json()
    new_email = (data.get('email', '') if data else '').strip()
    if not new_email:
        return jsonify({'success': False, 'message': 'Email required'}), 400

    session_path = os.path.join(SESSIONS_DIR, phone)
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': False, 'message': 'Session not authorized'}), 400

        result = await client(functions.account.SendVerifyEmailCodeRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            email=new_email
        ))
        email_verification_sessions[phone] = {
            'email': new_email,
            'code_length': getattr(result, 'code_length', 6)
        }
        return jsonify({'success': True, 'message': f'OTP sent to {new_email}', 'code_length': getattr(result, 'code_length', 6)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


@app.route('/admin/number/<path:phone>/verify_email_otp', methods=['POST'])
async def admin_verify_email_otp(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json()
    code = (data.get('code', '') if data else '').strip()
    if not code:
        return jsonify({'success': False, 'message': 'OTP code required'}), 400

    pending = email_verification_sessions.get(phone)
    if not pending:
        return jsonify({'success': False, 'message': 'No pending verification. Send OTP first.'}), 400

    session_path = os.path.join(SESSIONS_DIR, phone)
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': False, 'message': 'Session not authorized'}), 400

        await client(functions.account.VerifyEmailRequest(
            purpose=types.EmailVerifyPurposeLoginChange(),
            verification=types.EmailVerificationCode(code=code)
        ))
        del email_verification_sessions[phone]

        # Mark email_changed in user_data.json for this phone number
        raw_phone = phone.replace('sell_', '').replace('+', '').strip()
        user_data_all = load_data()
        for uid, info in user_data_all.items():
            for detail in info.get('processing_details', []):
                if detail.get('number', '').replace('+', '').strip() == raw_phone:
                    detail['email_changed'] = True
                    break
        with open(DATA_FILE, 'w') as fw:
            json.dump(user_data_all, fw, indent=4)

        return jsonify({'success': True, 'message': f'Email changed to {pending["email"]} successfully!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


@app.route('/admin/number/<path:phone>/get_code')
async def admin_get_code(phone):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    session_path = os.path.join(SESSIONS_DIR, phone)
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': False, 'message': 'Session not authorized'}), 400

        import re as _re
        # 777000 is Telegram's official service account that sends OTPs
        messages = await client.get_messages(777000, limit=20)
        msgs = []
        for msg in messages:
            text = msg.message or ''
            # Extract the numeric code (5-6 digits typically). Telegram sends this
            # message in the user's own app language, so we can't rely on matching
            # English phrases like "login code" — detect the code itself instead,
            # which is language-independent. Only messages that actually contain a
            # code are returned; anything else (no code) is dropped entirely.
            code_match = _re.search(r'(?<!\d)(\d{5,6})(?!\d)', text)
            code = code_match.group(1) if code_match else None
            if not code:
                continue
            msgs.append({
                'code': code,
                'date': msg.date.strftime('%Y-%m-%d %H:%M:%S') if msg.date else 'N/A'
            })

        return jsonify({'success': True, 'messages': msgs})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


# ── Join Channel ─────────────────────────────────────────────────────────────

def _join_channel_thread(task_id: str, channel_link: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_join_channel_logic(task_id, channel_link))
    finally:
        loop.close()


async def _join_channel_logic(task_id: str, channel_link: str):
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    sessions = []
    if os.path.exists(SESSIONS_DIR):
        for f in sorted(os.listdir(SESSIONS_DIR)):
            if f.endswith('.session') and not f.endswith('-journal'):
                sessions.append(f[:-8])

    join_tasks[task_id]['total'] = len(sessions)

    for session_name in sessions:
        session_path = os.path.join(SESSIONS_DIR, session_name)
        raw_phone = session_name.replace('sell_', '')
        display_phone = '+' + raw_phone if not raw_phone.startswith('+') else raw_phone
        client = TelegramClient(session_path, API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                join_tasks[task_id]['results'].append(
                    {'phone': display_phone, 'status': 'error', 'msg': 'Session not authorized'})
            else:
                link = channel_link.strip()
                # Detect private invite link: t.me/+HASH or t.me/joinchat/HASH
                import re as _re2
                private_match = _re2.search(r't\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)', link)
                if private_match:
                    invite_hash = private_match.group(1)
                    await client(ImportChatInviteRequest(invite_hash))
                else:
                    # Public channel: extract username
                    username = link.rstrip('/').split('/')[-1].lstrip('@')
                    entity = await client.get_entity(username)
                    await client(JoinChannelRequest(entity))
                join_tasks[task_id]['results'].append(
                    {'phone': display_phone, 'status': 'success', 'msg': 'Joined ✅'})
        except errors.UserAlreadyParticipantError:
            join_tasks[task_id]['results'].append(
                {'phone': display_phone, 'status': 'already', 'msg': 'Already a member'})
        except Exception as e:
            join_tasks[task_id]['results'].append(
                {'phone': display_phone, 'status': 'error', 'msg': str(e)[:120]})
        finally:
            if client.is_connected():
                await client.disconnect()
        join_tasks[task_id]['done'] += 1

    join_tasks[task_id]['status'] = 'done'


@app.route('/admin/join_channel', methods=['POST'])
def admin_join_channel():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.get_json()
    channel_link = (data.get('channel', '') if data else '').strip()
    if not channel_link:
        return jsonify({'success': False, 'message': 'Channel link required'}), 400

    task_id = uuid.uuid4().hex[:10]
    join_tasks[task_id] = {'status': 'running', 'total': 0, 'done': 0, 'results': []}
    t = threading.Thread(target=_join_channel_thread, args=(task_id, channel_link), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/admin/join_channel_status/<task_id>')
def admin_join_channel_status(task_id):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = join_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


# ── Bulk Auto Email Change ────────────────────────────────────────────────────

def _bulk_email_thread(task_id: str, phones: list):
    """Runs auto email change sequentially for each phone in its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_bulk_email_logic(task_id, phones))
    finally:
        loop.close()


async def _bulk_email_logic(task_id: str, phones: list):
    bulk_email_tasks[task_id]['total'] = len(phones)
    for phone in phones:
        raw_phone = phone.replace('sell_', '').replace('+', '').strip()
        digits_only = ''.join(c for c in raw_phone if c.isdigit())
        last7 = digits_only[-7:] if len(digits_only) >= 7 else digits_only
        mail_user = last7
        sub_task_id = uuid.uuid4().hex[:10]
        auto_tasks[sub_task_id] = {
            'status': 'running',
            'logs': [f'🔢 Phone suffix: {mail_user}'],
            'result': None,
            'email': '',
        }
        display_phone = '+' + raw_phone if not raw_phone.startswith('+') else raw_phone
        bulk_email_tasks[task_id]['results'].append({
            'phone': display_phone,
            'sub_task_id': sub_task_id,
            'status': 'running',
            'email': '',
            'msg': '',
        })
        idx = len(bulk_email_tasks[task_id]['results']) - 1

        # Run the actual email change logic
        await _auto_email_logic(sub_task_id, phone, raw_phone, mail_user)

        # Collect result
        st = auto_tasks[sub_task_id]
        bulk_email_tasks[task_id]['results'][idx]['status'] = st['status']
        bulk_email_tasks[task_id]['results'][idx]['email'] = st.get('email', '')
        bulk_email_tasks[task_id]['results'][idx]['msg'] = st.get('result') or (st['logs'][-1] if st['logs'] else '')
        bulk_email_tasks[task_id]['done'] += 1

    bulk_email_tasks[task_id]['status'] = 'done'


@app.route('/admin/bulk_auto_email', methods=['POST'])
def admin_bulk_auto_email():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.get_json()
    phones = _sanitize_session_names(data.get('phones', []) if data else [])
    if not phones:
        return jsonify({'success': False, 'message': 'No valid phones selected'}), 400

    task_id = uuid.uuid4().hex[:10]
    bulk_email_tasks[task_id] = {'status': 'running', 'total': len(phones), 'done': 0, 'results': []}
    t = threading.Thread(target=_bulk_email_thread, args=(task_id, phones), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/admin/bulk_auto_email_status/<task_id>')
def admin_bulk_auto_email_status(task_id):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = bulk_email_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


# ── Bulk Logout ───────────────────────────────────────────────────────────────

async def _logout_single_number(phone: str):
    """Fully log out a session and remove its files. Returns (success, message)."""
    session_path = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(session_path + '.session'):
        return False, 'Session file not found'

    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await client.connect()
        try:
            await client.log_out()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    for ext in ('.session', '.session-journal'):
        fpath = session_path + ext
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass

    return True, 'Logged out and removed'


def _bulk_logout_thread(task_id: str, phones: list):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_bulk_logout_logic(task_id, phones))
    finally:
        loop.close()


async def _bulk_logout_logic(task_id: str, phones: list):
    bulk_logout_tasks[task_id]['total'] = len(phones)
    for phone in phones:
        raw_phone = phone.replace('sell_', '').replace('+', '').strip()
        display_phone = '+' + raw_phone if not raw_phone.startswith('+') else raw_phone
        try:
            ok, msg = await _logout_single_number(phone)
        except Exception as e:
            ok, msg = False, str(e)
        bulk_logout_tasks[task_id]['results'].append({
            'phone': display_phone,
            'session_name': phone,
            'status': 'done' if ok else 'error',
            'msg': msg,
        })
        bulk_logout_tasks[task_id]['done'] += 1

    bulk_logout_tasks[task_id]['status'] = 'done'


def _sanitize_session_names(names: list) -> list:
    """Only allow session names that correspond to an actual session file, to
    reject path traversal or bogus values from client input."""
    safe = []
    for n in names or []:
        if not isinstance(n, str) or '/' in n or '\\' in n or '..' in n:
            continue
        if os.path.exists(os.path.join(SESSIONS_DIR, n + '.session')):
            safe.append(n)
    return safe


@app.route('/admin/bulk_logout', methods=['POST'])
def admin_bulk_logout():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.get_json()
    phones = _sanitize_session_names(data.get('phones', []) if data else [])
    if not phones:
        return jsonify({'success': False, 'message': 'No valid phones selected'}), 400

    task_id = uuid.uuid4().hex[:10]
    bulk_logout_tasks[task_id] = {'status': 'running', 'total': len(phones), 'done': 0, 'results': []}
    t = threading.Thread(target=_bulk_logout_thread, args=(task_id, phones), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/admin/bulk_logout_status/<task_id>')
def admin_bulk_logout_status(task_id):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = bulk_logout_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


def _bulk_2fa_thread(task_id: str, phones: list, action: str, password: str, new_password: str, hint: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_bulk_2fa(task_id, phones, action, password, new_password, hint))
    finally:
        loop.close()


async def _async_bulk_2fa(task_id: str, phones: list, action: str, password: str, new_password: str, hint: str):
    bulk_2fa_tasks[task_id]['total'] = len(phones)
    for name in phones:
        raw_phone = name.replace('sell_', '').replace('+', '').strip()
        display_phone = '+' + raw_phone if not raw_phone.startswith('+') else raw_phone
        entry = {
            'phone': display_phone,
            'session_name': name,
            'status': 'running',
            'msg': 'Processing…',
        }
        bulk_2fa_tasks[task_id]['results'].append(entry)

        session_path = os.path.join(SESSIONS_DIR, name)
        client = TelegramClient(session_path, API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                entry.update(status='error', msg='Session not authorized')
            elif action == 'disable':
                await client.edit_2fa(current_password=password, new_password='')
                entry.update(status='done', msg='✅ 2FA disabled')
            elif action == 'enable':
                await client.edit_2fa(new_password=new_password, hint=hint)
                entry.update(status='done', msg='✅ 2FA enabled')
            else:
                entry.update(status='error', msg='Invalid action')
        except Exception as exc:
            entry.update(status='error', msg=f'❌ {exc}')
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        bulk_2fa_tasks[task_id]['done'] += 1

    bulk_2fa_tasks[task_id]['status'] = 'done'


@app.route('/admin/bulk_2fa', methods=['POST'])
def admin_bulk_2fa():
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    data = request.get_json() or {}
    phones = _sanitize_session_names(data.get('phones', []))
    action = data.get('action', '')
    password = data.get('password', '')
    new_password = data.get('new_password', '')
    hint = data.get('hint', '')

    if not phones:
        return jsonify({'success': False, 'message': 'No valid phones selected'}), 400
    if action not in ('enable', 'disable'):
        return jsonify({'success': False, 'message': 'Invalid action'}), 400
    if action == 'disable' and not password:
        return jsonify({'success': False, 'message': 'Current password required to disable 2FA'}), 400
    if action == 'enable' and not new_password:
        return jsonify({'success': False, 'message': 'New password required to enable 2FA'}), 400

    task_id = uuid.uuid4().hex[:10]
    bulk_2fa_tasks[task_id] = {'status': 'running', 'total': len(phones), 'done': 0, 'results': [], 'action': action}
    t = threading.Thread(target=_bulk_2fa_thread, args=(task_id, phones, action, password, new_password, hint), daemon=True)
    t.start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/admin/bulk_2fa_status/<task_id>')
def admin_bulk_2fa_status(task_id):
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = bulk_2fa_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


@app.route('/admin/session_count/<path:phone>')
async def admin_session_count(phone):
    """Return the number of active Telegram authorizations for a session."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'count': 0}), 401

    # Validate: must have an actual session file
    session_path = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(session_path + '.session'):
        return jsonify({'success': False, 'count': 0, 'message': 'Session file not found'}), 404

    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return jsonify({'success': True, 'count': 0, 'authorized': False})
        auths = await client(functions.account.GetAuthorizationsRequest())
        count = len(auths.authorizations)
        return jsonify({'success': True, 'count': count, 'authorized': True})
    except Exception as e:
        return jsonify({'success': False, 'count': 0, 'message': str(e)}), 500
    finally:
        if client.is_connected():
            await client.disconnect()


# verify_session_tasks: task_id -> {status, total, done, results}
verify_session_tasks: dict = {}


def _is_safe_session_filename(fname: str) -> bool:
    """Return True only if fname is a plain .session filename with no path tricks."""
    fname = os.path.basename(fname)
    return (
        fname.endswith('.session')
        and not fname.endswith('-journal')
        and '..' not in fname
        and '/' not in fname
        and '\\' not in fname
        and len(fname) > len('.session')
    )


def _save_session_bytes(fname: str, data: bytes, saved: list, errors: list):
    fname = os.path.basename(fname)
    dest = os.path.join(SESSIONS_DIR, fname)
    try:
        with open(dest, 'wb') as fh:
            fh.write(data)
        saved.append(fname)
    except Exception as exc:
        errors.append(f'{fname}: {exc}')


def _extract_zip_sessions(file_storage, saved: list, errors: list):
    """Read an uploaded ZIP and extract every valid .session entry from it."""
    try:
        raw = file_storage.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            found = False
            for zi in zf.infolist():
                bname = os.path.basename(zi.filename)
                if not bname:
                    continue  # directory entry
                if _is_safe_session_filename(bname):
                    _save_session_bytes(bname, zf.read(zi.filename), saved, errors)
                    found = True
                # silently skip non-.session entries inside the ZIP
            if not found:
                errors.append(f'{file_storage.filename}: ZIP contains no .session files')
    except zipfile.BadZipFile:
        errors.append(f'{file_storage.filename}: not a valid ZIP file')
    except Exception as exc:
        errors.append(f'{file_storage.filename}: {exc}')


def _run_verify_sessions(task_id: str, session_names: list):
    """Background thread: connect to each session and check authorization."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_verify_sessions(task_id, session_names))
    finally:
        loop.close()


async def _async_verify_sessions(task_id: str, session_names: list):
    for name in session_names:
        result_entry = {
            'session_name': name,
            'phone': name.replace('sell_', '+') if not name.startswith('+') else name,
            'status': 'checking',
            'authorized': None,
            'msg': 'Checking…',
        }
        verify_session_tasks[task_id]['results'].append(result_entry)

        session_path = os.path.join(SESSIONS_DIR, name)
        client = TelegramClient(session_path, API_ID, API_HASH)
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            if authorized:
                me = await client.get_me()
                phone_str = ('+' + me.phone) if me and me.phone else name
                result_entry.update(
                    status='ok',
                    authorized=True,
                    phone=phone_str,
                    msg='✅ Authorized',
                )
            else:
                result_entry.update(
                    status='error',
                    authorized=False,
                    msg='❌ Session expired / not authorized',
                )
        except Exception as exc:
            result_entry.update(
                status='error',
                authorized=False,
                msg=f'❌ {exc}',
            )
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        verify_session_tasks[task_id]['done'] += 1

    verify_session_tasks[task_id]['status'] = 'done'


@app.route('/admin/upload_session', methods=['POST'])
def admin_upload_session():
    """Upload .session files (individually or inside a ZIP) into the sessions directory."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    files = request.files.getlist('session_files')
    if not files:
        return jsonify({'success': False, 'message': 'No files provided'}), 400

    saved = []
    errors = []
    for f in files:
        fname = os.path.basename(f.filename or '')
        if fname.endswith('.zip'):
            _extract_zip_sessions(f, saved, errors)
        elif _is_safe_session_filename(fname):
            data = f.read()
            _save_session_bytes(fname, data, saved, errors)
        else:
            errors.append(f'{fname}: only .session or .zip files are accepted')

    # Kick off background verification for freshly saved sessions
    task_id = None
    if saved:
        # Strip .session suffix to get session names
        session_names = [s[:-8] for s in saved if s.endswith('.session')]
        if session_names:
            task_id = uuid.uuid4().hex[:10]
            verify_session_tasks[task_id] = {
                'status': 'running',
                'total': len(session_names),
                'done': 0,
                'results': [],
            }
            t = threading.Thread(
                target=_run_verify_sessions,
                args=(task_id, session_names),
                daemon=True,
            )
            t.start()

    return jsonify({
        'success': len(saved) > 0,
        'saved': saved,
        'errors': errors,
        'task_id': task_id,
        'message': f'{len(saved)} file(s) uploaded' + (f'; {len(errors)} error(s)' if errors else ''),
    })


@app.route('/admin/verify_sessions_status/<task_id>')
def admin_verify_sessions_status(task_id):
    """Poll for real-time verification results after uploading sessions."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    task = verify_session_tasks.get(task_id)
    if not task:
        return jsonify({'success': False, 'message': 'Task not found'}), 404
    return jsonify(task)


@app.route('/admin/download_sessions', methods=['POST'])
def admin_download_sessions():
    """Return a ZIP containing the .session files for the given session names."""
    if 'user_id' not in session or session['user_id'] != '2876886938':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.get_json()
    phones = _sanitize_session_names(data.get('phones', []) if data else [])
    if not phones:
        return jsonify({'success': False, 'message': 'No valid session names provided'}), 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in phones:
            src = os.path.join(SESSIONS_DIR, name + '.session')
            if os.path.exists(src):
                zf.write(src, arcname=name + '.session')
    buf.seek(0)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_name = f'sessions_{timestamp}.zip'
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_name
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
