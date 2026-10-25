# -*- coding: utf-8 -*-
"""ربات ساده‌ی فروش کانفیگ (سروش): شارژ کیف پول (کارت‌به‌کارت + تایید دستی)، حساب من، خرید کانفیگ،
و پنل ادمین کامل داخل خود بات (کاربران، کانفیگ‌ها، سفارش‌ها، مالی، پیام همگانی) — بدون نیاز به web_panel.py.
مقدارها از Environment خونده می‌شن؛ فقط آدرس و توکن دیتابیس Turso یه پیش‌فرض داخل کد داره (Environment جایگزینش می‌کنه)."""
import os
import re
import json
import time
import html
import threading
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ===========================================================================
# اجرای چند بات با همین کد
# ---------------------------------------------------------------------------
# متغیرهای هر بات با «عدد» آخرِ اسمشون جدا می‌شن:
#   BOT_TOKEN1, BOT_TOKEN2, ...          توکن بات ۱، ۲، ...
#   ADMIN_ID1, BOT_USERNAME1, CARD_NUMBER1, CARD_OWNER1, CHANNEL_LINK1 ...
# هر متغیری که عدد نداشته باشه (مثل TURSO_DATABASE_URL، TURSO_AUTH_TOKEN، CONFIG_KEY) برای همه‌ی
# بات‌ها مشترکه. همه‌ی بات‌ها روی یه دیتابیس کار می‌کنن و جدول‌های هر بات با پیشوند b<عدد>_ جداست.
# برای هر توکن یه پردازش جدا بالا میاد و اگه کرش کنه خودش
# دوباره بالا میاد. اگه فقط BOT_TOKEN (بدون عدد) باشه، یه بات معمولی اجرا می‌شه.
# ===========================================================================
import re as _re
import sys as _sys
import subprocess as _sp

# دیتابیس پیش‌فرض (اگه TURSO_DATABASE_URL / TURSO_AUTH_TOKEN تو Environment ست بشن، همون‌ها جایگزین می‌شن)
_DEFAULT_TURSO_URL = "libsql://people-bot-mikekamalzadeh-sys.aws-eu-west-1.turso.io"
_DEFAULT_TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTI5NTk4MjUsImlkIjoiMDFhMTc0YjktZWYwMS03ZWVmLTk0Y2YtYWM1YmFjMzI1Y2JmIiwia2lkIjoiNmltOXd5bGxUd2luNkVHWEJWNHVJUE01ZWNPM2JyZmJ3NzFqWTFIanFCTSIsInJpZCI6ImMwYmRlZjNkLTQ5ZDktNDBlOC1iNDBhLWM0M2ZjNGQxZjhhYiJ9.Px_-sbVDy08hiewnqKy3HJ0rR24u1PXPKDNRCNOzGF3muSubeKGaPpRxUtPaslJ87COj6paze91WFz6hrMtMAg"


def _multi_bot_launcher():
    if os.getenv("BOT_INSTANCE"):
        return
    nums = sorted(int(m.group(1)) for m in (_re.match(r"^BOT_TOKEN(\d+)$", k) for k in os.environ)
                  if m and os.environ.get("BOT_TOKEN" + m.group(1), "").strip())
    if not nums:
        return

    def _env_for(i):
        env = dict(os.environ)
        env["BOT_INSTANCE"] = str(i)
        for k, v in os.environ.items():       # NAME<i>  ->  NAME  (فقط برای بات شماره‌ی i)
            m = _re.match(r"^(.+?)(\d+)$", k)
            if m and int(m.group(2)) == i:
                env[m.group(1)] = v
        env["BOT_TOKEN"] = env["BOT_TOKEN"].strip()
        return env

    envs = {i: _env_for(i) for i in nums}
    seen = {}
    if len(nums) > 1 and any((e.get("DB_PREFIX") or "").strip() == "-" for e in envs.values()):
        raise SystemExit("وقتی چند بات هست، DB_PREFIX=- (بدون پیشوند) مجاز نیست؛ اطلاعات بات‌ها قاطی می‌شه.")
    for i, e in envs.items():                  # دیتابیس می‌تونه مشترک باشه، ولی پیشوند جدول‌های هر بات باید یکتا باشه
        url = (e.get("TURSO_DATABASE_URL") or _DEFAULT_TURSO_URL).strip()
        if not url or not (e.get("TURSO_AUTH_TOKEN") or _DEFAULT_TURSO_TOKEN).strip():
            raise SystemExit("TURSO_DATABASE_URL و TURSO_AUTH_TOKEN تنظیم نشدن (برای بات %d)." % i)
        key = (url, (e.get("DB_PREFIX") or "").strip() or "b%d_" % i)
        if key in seen:
            raise SystemExit("بات %d و بات %d روی یه دیتابیس پیشوند جدول یکسان دارن." % (seen[key], i))
        seen[key] = i
    toks = [e["BOT_TOKEN"] for e in envs.values()]
    if len(set(toks)) != len(toks):
        raise SystemExit("دو بات توکن یکسان دارن.")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.abspath(__file__)
    procs = {}

    def _spawn(i):
        env = dict(envs[i])
        if i != nums[0]:                       # فقط بات اول پورت سلامت‌سنجی رو می‌گیره
            env.setdefault("DISABLE_HEALTH_SERVER", "1")
        return _sp.Popen([_sys.executable, "-u", script], cwd=base_dir, env=env)

    for i in nums:
        procs[i] = _spawn(i)
        print("🚀 بات %d بالا اومد (pid %d)" % (i, procs[i].pid), flush=True)
        time.sleep(1.5)
    try:
        while True:
            time.sleep(5)
            for i, pr in list(procs.items()):
                if pr.poll() is not None:
                    print("⚠️ بات %d خارج شد (کد %s)؛ ۵ ثانیه بعد دوباره بالا میاد." % (i, pr.returncode), flush=True)
                    time.sleep(5)
                    procs[i] = _spawn(i)
    except KeyboardInterrupt:
        pass
    finally:
        for pr in procs.values():
            try:
                pr.terminate()
            except Exception:
                pass
    raise SystemExit(0)


_multi_bot_launcher()

import turso_serverless

# ---------------------------------------------------------------------------
# تنظیمات (همه از Environment؛ هیچ مقداری داخل کد نیست)
# ---------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("متغیر محیطی BOT_TOKEN (یا BOT_TOKEN1, BOT_TOKEN2, ...) تنظیم نشده.")
BASE_URL = "https://api.splus.ir/bot" + TOKEN
CONFIG_API = os.getenv("CONFIG_API", "").strip().rstrip('/')
CONFIG_KEY = os.getenv("CONFIG_KEY", "").strip()
ADMIN_ID = int((os.getenv("ADMIN_ID", "") or "0").strip() or 0)
TURSO_DATABASE_URL = (os.getenv("TURSO_DATABASE_URL") or _DEFAULT_TURSO_URL).strip()
TURSO_AUTH_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or _DEFAULT_TURSO_TOKEN).strip()
if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
    raise SystemExit("متغیرهای TURSO_DATABASE_URL و TURSO_AUTH_TOKEN تنظیم نشدن.")

DIVIDER = "┄" * 16
SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=30)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)
UPDATE_EXECUTOR = ThreadPoolExecutor(max_workers=8)
BG_EXECUTOR = ThreadPoolExecutor(max_workers=2)

user_steps = {}          # وضعیت مرحله‌ی هر کاربر (فقط تو حافظه)
_known_users = set()
_chat_locks = {}
_chat_locks_guard = threading.Lock()


def get_chat_lock(chat_id):
    with _chat_locks_guard:
        lk = _chat_locks.get(chat_id)
        if lk is None:
            lk = _chat_locks[chat_id] = threading.Lock()
        return lk


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
_db_local = threading.local()

# ---------------------------------------------------------------------------
# چند بات روی یه دیتابیس: هر بات جدول‌های مخصوص خودش رو داره (مثلاً b2_users، b2_orders)
# ---------------------------------------------------------------------------
# تمام SQLها از همین لایه رد می‌شن و اسم جدول‌ها قبل از اجرا پیشوند می‌گیره، پس بقیه‌ی
# کد دست‌نخورده می‌مونه. پیشوند:
#   - حالت یه باتی (بدون BOT_INSTANCE): بدون پیشوند (مثل قبل)
#   - حالت چندباته: b<شماره>_  ؛ با DB_PREFIX<شماره> قابل تغییره، و DB_PREFIX<شماره>=-  یعنی بدون پیشوند
#   - بات شماره‌ی ۱ اگه جدول‌های قدیمی (بدون پیشوند) تو دیتابیس باشن، همون‌ها رو ادامه می‌ده تا دیتای فعلی
#     جابه‌جا نشه.
_DB_TABLES = ('user_configs', 'users', 'topup_requests', 'settings')
_DB_TBL_RE = re.compile(r'(?<![\w.])(' + '|'.join(_DB_TABLES) + r')(?!\w)')
_DB_IDX_RE = re.compile(r'(?<![\w.])(idx_\w+)')
_DB_LIT_RE = re.compile(r"('(?:[^']|'')*')")
_db_prefix = None
_db_prefix_lock = threading.Lock()
_db_sql_cache = {}


def _db_pick_prefix(raw):
    inst = os.getenv('BOT_INSTANCE', '').strip()
    if not inst:
        return ''
    env_p = os.getenv('DB_PREFIX')
    if env_p is not None and env_p.strip():
        env_p = env_p.strip()
        return '' if env_p == '-' else re.sub(r'\W', '', env_p) + '_'
    pref = 'b%s_' % inst
    if inst == '1':   # دیتای قدیمیِ بات اول رو جابه‌جا نمی‌کنیم
        c = raw.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', ?)", (pref + 'users',))
        names = {r[0] for r in c.fetchall()}
        if 'users' in names and (pref + 'users') not in names:
            print('ℹ️ جدول‌های قدیمی پیدا شد؛ بات ۱ همون‌ها رو (بدون پیشوند) ادامه می‌ده.')
            return ''
    return pref


def _db_get_prefix(raw):
    global _db_prefix
    if _db_prefix is None:
        with _db_prefix_lock:
            if _db_prefix is None:
                _db_prefix = _db_pick_prefix(raw)
                print('🗄 پیشوند جدول‌های این بات:', _db_prefix or '(بدون پیشوند)')
    return _db_prefix


_DB_REF_RE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([A-Za-z_]\w*)', re.I)


def _db_assert_isolated(sql, prefix):
    """سپر ایمنی: هر کوئری فقط اجازه‌ی دست‌زدن به جدول‌های همین بات (با پیشوند خودش) رو داره؛
    اگه جایی اسم جدولِ بدون پیشوند (یا مال بات دیگه) بیاد، اجرا نمی‌شه تا اطلاعات بات‌ها قاطی نشه."""
    for seg in _DB_LIT_RE.split(sql)[::2]:
        for name in _DB_REF_RE.findall(seg):
            if name.upper() == 'SET' or name == 'sqlite_master' or name.startswith(prefix):
                continue
            raise RuntimeError('دسترسی به جدول خارج از بات (%s) مجاز نیست' % name)


def _db_rewrite(sql):
    p = _db_prefix
    if not p or not isinstance(sql, str):
        return sql
    out = _db_sql_cache.get(sql)
    if out is None:
        parts = _DB_LIT_RE.split(sql)
        for i in range(0, len(parts), 2):    # فقط بیرون از رشته‌های «...»
            seg = _DB_TBL_RE.sub(lambda m: p + m.group(1), parts[i])
            parts[i] = _DB_IDX_RE.sub(lambda m: p + m.group(1), seg)
        out = ''.join(parts)
        _db_assert_isolated(out, p)
        if len(_db_sql_cache) < 2000:
            _db_sql_cache[sql] = out
    return out


class _PrefixCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, *a, **k):
        self._cur.execute(_db_rewrite(sql), *a, **k)
        return self

    def executemany(self, sql, *a, **k):
        self._cur.executemany(_db_rewrite(sql), *a, **k)
        return self

    def executescript(self, sql, *a, **k):
        self._cur.executescript(_db_rewrite(sql), *a, **k)
        return self

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PrefixConn:
    def __init__(self, raw):
        self._raw = raw

    def cursor(self, *a, **k):
        return _PrefixCursor(self._raw.cursor(*a, **k))

    def execute(self, sql, *a, **k):
        return self.cursor().execute(sql, *a, **k)

    def close(self):   # close() فراخوانی‌های قدیمی رو بی‌اثر می‌کنیم
        pass

    def __getattr__(self, name):
        return getattr(self._raw, name)


def get_conn():
    conn = getattr(_db_local, 'conn', None)
    if conn is None:
        raw = turso_serverless.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        _db_get_prefix(raw)
        conn = _PrefixConn(raw)
        _db_local.conn = conn
    return conn

def reset_conn():
    _db_local.conn = None


def with_db_retry(func):
    """اگه stream اتصال Turso به‌خاطر بی‌کاری منقضی شده باشه (خطای 404 'stream not
    found')، اتصال رو ریست می‌کنه و همون عملیات رو یک بار دیگه اجرا می‌کنه؛ برای
    هر خطای دیگه، خطا رو دوباره پرتاب می‌کنه تا رفتار قبلی حفظ بشه."""
    def wrapper(*args, **kwargs):
        _t0 = time.time()
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            if 'stream not found' in msg or 'stream_expired' in msg or ('404' in msg and 'stream' in msg):
                print('♻️ اتصال دیتابیس (stream) منقضی شده بود؛ اتصال تازه ساخته و درخواست دوباره اجرا شد.')
                reset_conn()
                return func(*args, **kwargs)
            raise
        finally:
            _dt = time.time() - _t0
            if _dt > 1.0:   # تشخیص کندی: لتنسی دیتابیس راه دور (Turso) اینجا دیده می‌شه
                print(f'[slow-db] {func.__name__} {_dt:.1f}s')
    return wrapper


@with_db_retry
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, wallet INTEGER DEFAULT 0)')
    c.execute('''CREATE TABLE IF NOT EXISTS user_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sub_url TEXT, config_id TEXT, gb REAL,
        label TEXT, days INTEGER, price INTEGER, type TEXT, status TEXT DEFAULT 'active',
        created_at TEXT, expires_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS topup_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
        status TEXT DEFAULT "pending", created_at TEXT)''')
    c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    for col in ('ALTER TABLE users ADD COLUMN username TEXT', 'ALTER TABLE users ADD COLUMN first_name TEXT',
                'ALTER TABLE users ADD COLUMN joined_at TEXT',
                "ALTER TABLE topup_requests ADD COLUMN method TEXT DEFAULT 'card'",
                'ALTER TABLE topup_requests ADD COLUMN photo_id TEXT'):
        try:
            c.execute(col)
        except Exception:
            pass  # ستون از قبل هست
    conn.commit()


def _claim(c, conn, sql, params):
    """UPDATE اتمیک: True فقط اگه یه ردیف واقعاً عوض شده باشه."""
    try:
        c.execute(sql + ' RETURNING 1', params)
        won = c.fetchone() is not None
    except Exception as e:
        if 'stream' in str(e).lower():
            raise
        c.execute(sql, params)
        won = (c.rowcount or 0) > 0
    conn.commit()
    return won


@with_db_retry
def upsert_user(user_id, username, first_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, wallet, joined_at) VALUES (?, 0, ?)',
              (user_id, datetime.now().isoformat()))
    c.execute('UPDATE users SET username=?, first_name=? WHERE user_id=?', (username, first_name, user_id))
    conn.commit()


@with_db_retry
def get_wallet(user_id):
    c = get_conn().cursor()
    c.execute('SELECT wallet FROM users WHERE user_id=?', (user_id,))
    row = c.fetchone()
    return int(row[0] or 0) if row else 0


@with_db_retry
def add_wallet(user_id, amount):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, wallet) VALUES (?, 0)', (user_id,))
    c.execute('UPDATE users SET wallet = wallet + ? WHERE user_id=?', (amount, user_id))
    conn.commit()


@with_db_retry
def take_wallet(user_id, amount):
    """کم‌کردن اتمیک از کیف پول؛ فقط وقتی موجودی کافی باشه True."""
    conn = get_conn()
    return _claim(conn.cursor(), conn, 'UPDATE users SET wallet = wallet - ? WHERE user_id=? AND wallet >= ?',
                  (amount, user_id, amount))


@with_db_retry
def save_config(user_id, sub_url, config_id, gb, label, days, price, ctype):
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now()
    c.execute('''INSERT INTO user_configs (user_id, sub_url, config_id, gb, label, days, price, type, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (user_id, sub_url, config_id, gb, label, days, price, ctype, now.isoformat(),
               (now + timedelta(days=days)).isoformat()))
    conn.commit()


@with_db_retry
def list_configs(user_id):
    c = get_conn().cursor()
    c.execute('SELECT id, label, gb, days FROM user_configs WHERE user_id=? ORDER BY id DESC', (user_id,))
    return c.fetchall()


@with_db_retry
def get_config_row(user_id, row_id):
    c = get_conn().cursor()
    c.execute('SELECT id, sub_url, config_id, gb, label, days, price, expires_at FROM user_configs '
              'WHERE user_id=? AND id=?', (user_id, row_id))
    return c.fetchone()


@with_db_retry
def purchase_totals(user_id):
    c = get_conn().cursor()
    c.execute('SELECT COUNT(*), COALESCE(SUM(gb), 0), COALESCE(SUM(price), 0) FROM user_configs WHERE user_id=?',
              (user_id,))
    r = c.fetchone()
    return int(r[0] or 0), float(r[1] or 0), int(r[2] or 0)


@with_db_retry
def create_topup(user_id, amount, photo_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO topup_requests (user_id, amount, status, created_at, method, photo_id) "
              "VALUES (?, ?, 'pending', ?, 'card', ?)", (user_id, amount, datetime.now().isoformat(), photo_id))
    conn.commit()
    return c.lastrowid


@with_db_retry
def get_topup(req_id):
    c = get_conn().cursor()
    c.execute('SELECT id, user_id, amount, status FROM topup_requests WHERE id=?', (req_id,))
    return c.fetchone()


@with_db_retry
def claim_topup(req_id, new_status):
    """فقط اگه هنوز pending باشه وضعیت عوض می‌شه (جلوگیری از تایید/رد دوباره)."""
    conn = get_conn()
    return _claim(conn.cursor(), conn, "UPDATE topup_requests SET status=? WHERE id=? AND status='pending'",
                  (new_status, req_id))


@with_db_retry
def count_pending(user_id):
    c = get_conn().cursor()
    c.execute("SELECT COUNT(*) FROM topup_requests WHERE user_id=? AND status='pending'", (user_id,))
    return int(c.fetchone()[0] or 0)


@with_db_retry
def admin_stats():
    c = get_conn().cursor()
    c.execute('SELECT COUNT(*), COALESCE(SUM(wallet), 0) FROM users')
    users, wallets = c.fetchone()
    c.execute('SELECT COUNT(*), COALESCE(SUM(price), 0) FROM user_configs')
    cfgs, sales = c.fetchone()
    c.execute("SELECT COUNT(*) FROM topup_requests WHERE status='pending'")
    pending = c.fetchone()[0]
    return int(users or 0), int(wallets or 0), int(cfgs or 0), int(sales or 0), int(pending or 0)


@with_db_retry
def list_pending(limit=10):
    c = get_conn().cursor()
    c.execute("SELECT id, user_id, amount, photo_id FROM topup_requests WHERE status='pending' "
              "ORDER BY id LIMIT ?", (limit,))
    return c.fetchall()


# --- تنظیمات (جدول settings؛ از «پنل ادمین» تو خود بات عوض می‌شه) ---
_settings_cache, _settings_ts = {}, 0.0
_settings_lock = threading.Lock()


@with_db_retry
def _fetch_settings():
    c = get_conn().cursor()
    c.execute('SELECT key, value FROM settings')
    return {r[0]: r[1] for r in c.fetchall()}


def get_setting(key, default=''):
    global _settings_cache, _settings_ts
    if time.time() - _settings_ts > 20:
        with _settings_lock:
            if time.time() - _settings_ts > 20:
                try:
                    _settings_cache, _settings_ts = _fetch_settings(), time.time()
                except Exception as e:
                    print('⚠️ خوندن تنظیمات ناموفق بود:', e)
                    _settings_ts = time.time() - 10
    v = _settings_cache.get(key)
    return default if v is None or str(v).strip() == '' else str(v).strip()


@with_db_retry
def set_setting(key, value):
    conn = get_conn()
    conn.cursor().execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                          'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    conn.commit()
    _settings_cache[key] = value


def _int(v, d):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def card_number():
    return get_setting('card_number', os.getenv('CARD_NUMBER', ''))


def card_owner():
    return get_setting('card_owner', os.getenv('CARD_OWNER', ''))


def min_topup():
    return _int(get_setting('min_topup', os.getenv('MIN_TOPUP', '10000')), 10000)


def price_per_gb():
    return _int(get_setting('price_per_gb', os.getenv('PRICE_PER_GB', '3500')), 3500)


def config_proto():
    return get_setting('proto', os.getenv('CONFIG_PROTO', 'both'))


def topup_multiplier():
    try:
        return float(get_setting('topup_multiplier', '1'))
    except ValueError:
        return 1.0


def default_gb():
    return max(1, _int(get_setting('default_gb', '10'), 10))


def default_days():
    return max(1, _int(get_setting('default_days', '30'), 30))


def gb_step():
    return max(1, _int(get_setting('gb_step', '1'), 1))


def day_step():
    return max(1, _int(get_setting('day_step', '5'), 5))


# (کلید، عنوان، نوع) — همین‌ها تو پنل ادمین قابل‌ویرایشن
SETTING_META = (
    ('card_number', '💳 شماره کارت', 'text'),
    ('card_owner', '👤 نام صاحب کارت', 'text'),
    ('price_per_gb', '💵 قیمت هر گیگ (تومان)', 'int'),
    ('min_topup', '🔻 حداقل شارژ (تومان)', 'int'),
    ('topup_multiplier', '✖️ ضریب شارژ (۱ = بدون تغییر)', 'float'),
    ('default_gb', '📶 حجم پیش‌فرض (گیگ)', 'int'),
    ('default_days', '⏳ مدت پیش‌فرض (روز)', 'int'),
    ('gb_step', '↕️ گام تغییر حجم (گیگ)', 'int'),
    ('day_step', '↕️ گام تغییر مدت (روز)', 'int'),
    ('proto', '🔌 نوع کانفیگ', 'choice'),
    ('maintenance_text', '🛠 پیام حالت تعمیر', 'text'),
)
SETTING_GETTERS = {'card_number': card_number, 'card_owner': card_owner, 'price_per_gb': price_per_gb,
                   'min_topup': min_topup, 'topup_multiplier': topup_multiplier, 'default_gb': default_gb,
                   'default_days': default_days, 'gb_step': gb_step, 'day_step': day_step, 'proto': config_proto,
                   'maintenance_text': lambda: get_setting('maintenance_text', '🛠 ربات موقتاً در حال بروزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.')}
PROTO_CHOICES = ('both', 'xray', 'wireguard', 'openvpn', 'dns')


# ---------------------------------------------------------------------------
# API سروش (مثل تلگرام)
# ---------------------------------------------------------------------------
def api(method, payload, timeout=15):
    try:
        return SESSION.post(BASE_URL + '/' + method, json=payload, timeout=timeout).json()
    except Exception as e:
        print('⚠️ خطای API', method, ':', e)
        return {'ok': False}


def send_message(chat_id, text, reply_markup=None):
    p = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return api('sendMessage', p)


def edit_message(chat_id, message_id, text, reply_markup=None):
    p = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return api('editMessageText', p)


def send_photo(chat_id, photo_id, caption=None, reply_markup=None):
    p = {'chat_id': chat_id, 'photo': photo_id, 'parse_mode': 'HTML'}
    if caption:
        p['caption'] = caption
    if reply_markup:
        p['reply_markup'] = json.dumps(reply_markup)
    return api('sendPhoto', p, timeout=20)


def answer_callback(cb_id):
    BG_EXECUTOR.submit(api, 'answerCallbackQuery', {'callback_query_id': cb_id}, 5)


def get_updates(offset=None):
    params = {'timeout': 25}
    if offset:
        params['offset'] = offset
    try:
        data = SESSION.get(BASE_URL + '/getUpdates', params=params, timeout=35).json()
        if not data.get('ok'):
            print('⚠️ خطای getUpdates:', data)
        return data
    except Exception as e:
        print('⚠️ استثنا در getUpdates:', e)
        return {'ok': False, 'result': []}


# ---------------------------------------------------------------------------
# API پنل کانفیگ
# ---------------------------------------------------------------------------
def make_config(gb, label, days, proto):
    try:
        res = SESSION.post(CONFIG_API, headers={'Authorization': 'Bearer ' + CONFIG_KEY},
                           json={'gb': gb, 'label': label, 'expiryDays': days, 'proto': proto}, timeout=20)
        if res.status_code in (200, 201):
            data = res.json()
            return {'success': True, 'sub_url': data.get('subUrl'),
                    'config_id': data.get('id') or data.get('uuid') or data.get('username')}
        print('⚠️ خطای ساخت کانفیگ (کد %s):' % res.status_code, res.text[:500])
    except Exception as e:
        print('⚠️ استثنا در ساخت کانفیگ:', e)
    return {'success': False}


def config_usage_gb(config_id):
    """مصرف کانفیگ (گیگ) از پنل؛ اگه نشد None."""
    if not config_id:
        return None
    try:
        res = SESSION.get(CONFIG_API + '/' + str(config_id), headers={'Authorization': 'Bearer ' + CONFIG_KEY},
                          timeout=15)
        if res.status_code not in (200, 201):
            return None
        data = res.json()
        if isinstance(data, dict) and isinstance(data.get('configs'), list) and data['configs']:
            data = data['configs'][0]
        used = data.get('usedGb')
        return float(used) if used is not None else None
    except Exception as e:
        print('⚠️ خطا در گرفتن مصرف کانفیگ:', e)
        return None


def set_config_note(config_id, note):
    if not config_id:
        return
    try:
        SESSION.put('%s/%s/note' % (CONFIG_API, config_id),
                    headers={'Authorization': 'Bearer ' + CONFIG_KEY, 'Content-Type': 'application/json'},
                    json={'note': note}, timeout=10)
    except Exception as e:
        print('⚠️ ثبت یادداشت کانفیگ ناموفق بود:', e)


# ---------------------------------------------------------------------------
# ابزارهای کمکی و متن‌ها
# ---------------------------------------------------------------------------
_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def parse_int(text):
    s = (text or '').translate(_DIGITS)
    s = re.sub(r'[,\s٬،]', '', s)
    return int(s) if re.fullmatch(r'\d{1,12}', s) else None


def fmt(n):
    return '{:,}'.format(int(n))


def esc(s):
    return html.escape(str(s or ''), quote=False)


def days_left(expires_at):
    try:
        return max(0, (datetime.fromisoformat(expires_at) - datetime.now()).days)
    except Exception:
        return None


def usage_bar(used, total, n=10):
    filled = 0 if not total else min(n, int(round(n * used / total)))
    return '▓' * filled + '░' * (n - filled)


def menu_kb(uid=None):
    rows = [
        [{'text': '💳 شارژ کیف پول', 'callback_data': 'topup'}],
        [{'text': '👤 حساب من', 'callback_data': 'account'}],
        [{'text': '🛒 خرید کانفیگ', 'callback_data': 'buy'}],
    ]
    if ADMIN_ID and uid == ADMIN_ID:
        rows.append([{'text': '🛠 پنل ادمین', 'callback_data': 'ad:panel'}])
    return {'inline_keyboard': rows}


def back_kb():
    return {'inline_keyboard': [[{'text': '🏠 منوی اصلی', 'callback_data': 'menu'}]]}


def cancel_kb():
    return {'inline_keyboard': [[{'text': '❌ انصراف', 'callback_data': 'menu'}]]}


def send_menu(chat_id, note=None):
    user_steps.pop(chat_id, None)
    text = (note + '\n\n' if note else '') + '👋 به ربات خوش اومدی!\nیکی از گزینه‌های زیر رو انتخاب کن:'
    send_message(chat_id, text, menu_kb(chat_id))


# ---------------------------------------------------------------------------
# ۱) شارژ کیف پول (کارت‌به‌کارت + تایید دستی ادمین)
# ---------------------------------------------------------------------------
def topup_start(chat_id):
    if not card_number():
        send_message(chat_id, '⚠️ شارژ کیف پول فعلاً در دسترس نیست.', back_kb())
        return
    user_steps[chat_id] = {'step': 'topup_amount'}
    send_message(chat_id, '💳 <b>شارژ کیف پول</b>\n\nمبلغ مورد نظرت رو به <b>تومان</b> و فقط با عدد بفرست.\n'
                          'حداقل شارژ: <b>%s</b> تومان' % fmt(min_topup()), cancel_kb())


def topup_got_amount(chat_id, text):
    amount = parse_int(text)
    if amount is None or amount < min_topup():
        send_message(chat_id, '⚠️ یه عدد معتبر (حداقل %s تومان) بفرست.' % fmt(min_topup()), cancel_kb())
        return
    user_steps[chat_id] = {'step': 'topup_receipt', 'amount': amount}
    owner = card_owner()
    send_message(chat_id,
                 '💳 مبلغ <b>%s</b> تومان رو به کارت زیر واریز کن:\n\n<code>%s</code>\n%s\n%s\n\n'
                 '📸 بعد از واریز، <b>عکس رسید</b> رو همین‌جا بفرست تا ادمین تایید کنه.'
                 % (fmt(amount), esc(card_number()), ('به نام <b>%s</b>' % esc(owner)) if owner else '', DIVIDER),
                 cancel_kb())


def topup_got_receipt(chat_id, frm, msg):
    st = user_steps.get(chat_id) or {}
    photos = msg.get('photo') or []
    if not photos:
        send_message(chat_id, '📸 لطفاً رسید رو به‌صورت <b>عکس</b> بفرست.', cancel_kb())
        return
    if not ADMIN_ID:
        send_message(chat_id, '⚠️ شارژ فعلاً در دسترس نیست.', back_kb())
        return
    if count_pending(chat_id) >= 3:
        send_message(chat_id, '⏳ چند درخواست در انتظار تایید داری؛ لطفاً صبر کن تا بررسی بشن.', back_kb())
        user_steps.pop(chat_id, None)
        return
    photo_id = photos[-1].get('file_id')
    amount = st['amount']
    req_id = create_topup(chat_id, amount, photo_id)
    user_steps.pop(chat_id, None)
    uname = ('@' + frm['username']) if frm.get('username') else '-'
    caption = ('🧾 <b>درخواست شارژ #%s</b>\n👤 %s %s\n🆔 <code>%s</code>\n💰 مبلغ: <b>%s</b> تومان'
               % (req_id, esc(frm.get('first_name')), esc(uname), chat_id, fmt(amount)))
    kb = {'inline_keyboard': [[{'text': '✅ تایید', 'callback_data': 'ap:%s' % req_id},
                               {'text': '❌ رد', 'callback_data': 'rj:%s' % req_id}]]}
    r = send_photo(ADMIN_ID, photo_id, caption, kb)
    if not r.get('ok'):
        r = send_message(ADMIN_ID, caption + '\n(ارسال عکس ناموفق بود)', kb)
    if not r.get('ok'):
        claim_topup(req_id, 'failed')
        send_message(chat_id, '⚠️ ارسال رسید به ادمین ناموفق بود، دوباره امتحان کن.', back_kb())
        return
    send_message(chat_id, '✅ رسیدت برای ادمین ارسال شد. بعد از تایید، کیف پولت شارژ می‌شه و بهت خبر می‌دم.',
                 back_kb())


def admin_decide(chat_id, req_id, approve):
    req = get_topup(req_id)
    if not req:
        send_message(chat_id, '⚠️ درخواست پیدا نشد.')
        return
    if not claim_topup(req_id, 'approved' if approve else 'rejected'):
        send_message(chat_id, 'ℹ️ درخواست #%s قبلاً بررسی شده.' % req_id)
        return
    user_id, amount = req[1], req[2]
    if approve:
        credited = int(round(amount * topup_multiplier()))
        add_wallet(user_id, credited)
        send_message(user_id, '✅ شارژ تایید شد!\n💰 <b>%s</b> تومان به کیف پولت اضافه شد.' % fmt(credited), menu_kb(user_id))
        send_message(chat_id, '✅ درخواست #%s تایید شد (%s تومان).' % (req_id, fmt(credited)))
    else:
        send_message(user_id, '❌ درخواست شارژ #%s رد شد. اگه فکر می‌کنی اشتباهه، به پشتیبانی پیام بده.' % req_id,
                     menu_kb(user_id))
        send_message(chat_id, '❌ درخواست #%s رد شد.' % req_id)


# ---------------------------------------------------------------------------
# ۲) حساب من
# ---------------------------------------------------------------------------
def show_account(chat_id):
    count, gb, price = purchase_totals(chat_id)
    text = ('👤 <b>حساب من</b>\n%s\n🆔 شناسه: <code>%s</code>\n💰 موجودی: <b>%s</b> تومان\n'
            '📦 تعداد کانفیگ: <b>%s</b>\n📶 مجموع حجم خریداری‌شده: <b>%.1f</b> گیگ\n💸 مجموع خرید: <b>%s</b> تومان'
            % (DIVIDER, chat_id, fmt(get_wallet(chat_id)), count, gb, fmt(price)))
    rows = [[{'text': '📄 %s (%g گیگ)' % (r[1] or 'کانفیگ', r[2]), 'callback_data': 'cfg:%s' % r[0]}]
            for r in list_configs(chat_id)[:20]]
    rows.append([{'text': '🏠 منوی اصلی', 'callback_data': 'menu'}])
    send_message(chat_id, text, {'inline_keyboard': rows})


def show_config(chat_id, row_id):
    row = get_config_row(chat_id, row_id)
    if not row:
        send_message(chat_id, '⚠️ کانفیگ پیدا نشد.', back_kb())
        return
    _, sub_url, config_id, gb, label, days, price, expires_at = row
    used = config_usage_gb(config_id)
    left = days_left(expires_at)
    lines = ['📄 <b>%s</b>' % esc(label or 'کانفیگ'), DIVIDER, '📶 حجم: <b>%g</b> گیگ' % gb]
    if used is not None:
        lines.append('📊 مصرف: %s <b>%.2f</b> از %g گیگ' % (usage_bar(used, gb), used, gb))
    if left is not None:
        lines.append('⏳ مانده: <b>%s</b> روز' % left)
    lines += ['', '🔗 لینک اشتراک:', '<code>%s</code>' % esc(sub_url)]
    send_message(chat_id, '\n'.join(lines),
                 {'inline_keyboard': [[{'text': '⬅️ حساب من', 'callback_data': 'account'}]]})


# ---------------------------------------------------------------------------
# ۳) خرید کانفیگ
# ---------------------------------------------------------------------------
def qty_screen(chat_id, st):
    price = st['gb'] * price_per_gb()
    text = ('🛒 <b>خرید کانفیگ</b>\n%s\n💵 قیمت هر گیگ: <b>%s</b> تومان\n💰 موجودی تو: <b>%s</b> تومان\n%s\n'
            '💳 مبلغ کل: <b>%s</b> تومان' % (DIVIDER, fmt(price_per_gb()), fmt(get_wallet(chat_id)), DIVIDER, fmt(price)))
    kb = {'inline_keyboard': [
        [{'text': '➖', 'callback_data': 'q:gb-'}, {'text': '📶 %s گیگ' % st['gb'], 'callback_data': 'noop'},
         {'text': '➕', 'callback_data': 'q:gb+'}],
        [{'text': '➖', 'callback_data': 'q:d-'}, {'text': '⏳ %s روز' % st['days'], 'callback_data': 'noop'},
         {'text': '➕', 'callback_data': 'q:d+'}],
        [{'text': '✅ ادامه', 'callback_data': 'q:ok'}],
        [{'text': '❌ انصراف', 'callback_data': 'menu'}],
    ]}
    return text, kb


def buy_start(chat_id):
    if not CONFIG_API or not CONFIG_KEY:
        send_message(chat_id, '⚠️ خرید فعلاً در دسترس نیست.', back_kb())
        return
    st = {'step': 'buy_qty', 'gb': default_gb(), 'days': default_days()}
    user_steps[chat_id] = st
    text, kb = qty_screen(chat_id, st)
    send_message(chat_id, text, kb)


def buy_qty_change(chat_id, message_id, action):
    st = user_steps.get(chat_id) or {}
    if st.get('step') != 'buy_qty':
        return
    if action == 'gb-':
        st['gb'] = max(1, st['gb'] - gb_step())
    elif action == 'gb+':
        st['gb'] = min(1000, st['gb'] + gb_step())
    elif action == 'd-':
        st['days'] = max(1, st['days'] - day_step())
    elif action == 'd+':
        st['days'] = min(365, st['days'] + day_step())
    text, kb = qty_screen(chat_id, st)
    edit_message(chat_id, message_id, text, kb)


def buy_ask_label(chat_id):
    st = user_steps.get(chat_id) or {}
    if st.get('step') != 'buy_qty':
        return
    st['step'] = 'buy_label'
    send_message(chat_id, '✏️ یه اسم برای کانفیگ بفرست (یا «رد کردن» رو بزن):',
                 {'inline_keyboard': [[{'text': '⏭ رد کردن', 'callback_data': 'skip_label'}],
                                      [{'text': '❌ انصراف', 'callback_data': 'menu'}]]})


def buy_invoice(chat_id, label=None):
    st = user_steps.get(chat_id) or {}
    if st.get('step') != 'buy_label':
        return
    count = purchase_totals(chat_id)[0]
    st['label'] = (label or '').strip()[:40] or 'کانفیگ %s' % (count + 1)
    st['price'] = st['gb'] * price_per_gb()
    st['step'] = 'buy_confirm'
    send_message(chat_id,
                 '🧾 <b>فاکتور خرید</b>\n%s\n🏷 نام: <b>%s</b>\n📶 حجم: <b>%s</b> گیگ\n⏳ مدت: <b>%s</b> روز\n%s\n'
                 '💳 مبلغ: <b>%s</b> تومان\n💰 موجودی: <b>%s</b> تومان'
                 % (DIVIDER, esc(st['label']), st['gb'], st['days'], DIVIDER, fmt(st['price']),
                    fmt(get_wallet(chat_id))),
                 {'inline_keyboard': [[{'text': '✅ تایید و پرداخت', 'callback_data': 'buy_yes'},
                                       {'text': '❌ انصراف', 'callback_data': 'menu'}]]})


def buy_confirm(chat_id, frm):
    st = user_steps.get(chat_id) or {}
    if st.get('step') != 'buy_confirm':
        send_menu(chat_id)
        return
    user_steps.pop(chat_id, None)            # جلوگیری از پرداخت دوباره با کلیک تکراری
    price, gb, days, label = st['price'], st['gb'], st['days'], st['label']
    if not take_wallet(chat_id, price):
        short = price - get_wallet(chat_id)
        send_message(chat_id, '⚠️ موجودیت کافی نیست؛ <b>%s</b> تومان کم داری.' % fmt(max(short, 0)),
                     {'inline_keyboard': [[{'text': '💳 شارژ کیف پول', 'callback_data': 'topup'}],
                                          [{'text': '🏠 منوی اصلی', 'callback_data': 'menu'}]]})
        return
    proto = config_proto()
    res = make_config(gb, label, days, proto)
    if not res['success'] or not res.get('sub_url'):
        add_wallet(chat_id, price)           # برگردوندن پول
        send_message(chat_id, '⚠️ ساخت کانفیگ انجام نشد و مبلغ به کیف پولت برگشت. کمی بعد دوباره امتحان کن.',
                     back_kb())
        return
    try:
        save_config(chat_id, res['sub_url'], res['config_id'], gb, label, days, price, proto)
    except Exception as e:
        print('⚠️ ذخیره‌ی کانفیگ تو دیتابیس ناموفق بود:', e)
        if ADMIN_ID:
            send_message(ADMIN_ID, '⚠️ کانفیگ برای %s ساخته شد ولی ذخیره نشد:\n<code>%s</code>'
                         % (chat_id, esc(res['sub_url'])))
    uname = ('@' + frm['username']) if frm.get('username') else 'بدون یوزرنیم'
    BG_EXECUTOR.submit(set_config_note, res['config_id'], '%s | %s | %s تومان' % (chat_id, uname, fmt(price)))
    send_message(chat_id, '✅ <b>خریدت انجام شد!</b>\n%s\n🏷 %s\n📶 %s گیگ | ⏳ %s روز\n\n🔗 لینک اشتراک:\n<code>%s</code>'
                 % (DIVIDER, esc(label), gb, days, esc(res['sub_url'])), back_kb())


# ---------------------------------------------------------------------------
# پنل ادمین (داخل خود بات؛ فقط برای ADMIN_ID)
# ---------------------------------------------------------------------------
def is_admin(chat_id):
    return bool(ADMIN_ID) and chat_id == ADMIN_ID


def admin_panel(chat_id):
    user_steps.pop(chat_id, None)
    users, wallets, cfgs, sales, pending = admin_stats()
    text = ('🛠 <b>پنل ادمین</b>\n%s\n👥 کاربران: <b>%s</b>\n💰 مجموع موجودی کیف‌پول‌ها: <b>%s</b> تومان\n'
            '📦 کانفیگ فروخته‌شده: <b>%s</b>\n💸 مجموع فروش: <b>%s</b> تومان\n⏳ شارژ در انتظار تایید: <b>%s</b>'
            % (DIVIDER, fmt(users), fmt(wallets), fmt(cfgs), fmt(sales), pending))
    send_message(chat_id, text, {'inline_keyboard': [
        [_btn('⚙️ تنظیمات (کارت، قیمت و ...)', 'ad:set')],
        [_btn('👥 کاربران', 'ad:users:0'), _btn('📦 سفارش‌ها', 'ad:orders:0')],
        [_btn('📊 گزارش مالی', 'ad:fin'), _btn('📋 شارژهای در انتظار (%s)' % pending, 'ad:pending')],
        [_btn('📢 پیام همگانی', 'ad:bc')],
        [_btn('💰 تغییر موجودی کاربر', 'ad:wallet')],
        [_btn('🔧 حالت تعمیر: %s' % ('روشن 🔴' if maintenance_on() else 'خاموش 🟢'), 'ad:mt')],
        [_btn('🏠 منوی اصلی', 'menu')],
    ]})


def admin_settings(chat_id, note=None):
    user_steps.pop(chat_id, None)
    lines = ['⚙️ <b>تنظیمات</b>', DIVIDER]
    rows = []
    for key, title, _kind in SETTING_META:
        val = SETTING_GETTERS[key]()
        lines.append('%s: <b>%s</b>' % (title, esc(val) if str(val) != '' else '—'))
        rows.append([{'text': '✏️ ' + title, 'callback_data': 'ad:edit:' + key}])
    rows.append([{'text': '⬅️ پنل ادمین', 'callback_data': 'ad:panel'}])
    send_message(chat_id, (note + '\n\n' if note else '') + '\n'.join(lines), {'inline_keyboard': rows})


def admin_edit_start(chat_id, key):
    meta = {k: (t, kind) for k, t, kind in SETTING_META}
    if key not in meta:
        return
    title, kind = meta[key]
    if kind == 'choice':
        rows = [[{'text': ('✅ ' if c == config_proto() else '') + c, 'callback_data': 'ad:val:%s:%s' % (key, c)}]
                for c in PROTO_CHOICES]
        rows.append([{'text': '⬅️ تنظیمات', 'callback_data': 'ad:set'}])
        send_message(chat_id, '%s\nیکی رو انتخاب کن:' % title, {'inline_keyboard': rows})
        return
    user_steps[chat_id] = {'step': 'admin_edit', 'key': key}
    cur = SETTING_GETTERS[key]()
    send_message(chat_id, '✏️ %s\nمقدار فعلی: <b>%s</b>\n\nمقدار جدید رو بفرست (برای پاک‌کردن مقدار: <code>-</code>)'
                 % (title, esc(cur) if str(cur) != '' else '—'),
                 {'inline_keyboard': [[{'text': '❌ انصراف', 'callback_data': 'ad:set'}]]})


def admin_got_value(chat_id, text):
    key = (user_steps.get(chat_id) or {}).get('key')
    kind = {k: kd for k, _t, kd in SETTING_META}.get(key)
    t = (text or '').strip().translate(_DIGITS)
    if kind is None or not t:
        return
    value, err = t, None
    if t == '-':
        value = ''
    elif kind == 'int':
        n = parse_int(t)
        value, err = (str(n), None) if n else (None, 'یه عدد صحیح بزرگ‌تر از صفر بفرست.')
    elif kind == 'float':
        try:
            f = float(t)
            value, err = (('%g' % f), None) if 0 < f <= 100 else (None, 'عددی بین 0 و 100 بفرست.')
        except ValueError:
            value, err = None, 'یه عدد بفرست (مثلاً 1 یا 1.5).'
    elif key == 'card_number':
        digits = re.sub(r'[\s-]', '', t)
        value, err = (digits, None) if digits.isdigit() and 12 <= len(digits) <= 19 else (None, 'شماره کارت معتبر بفرست.')
    else:
        value = t[:60]
    if err:
        send_message(chat_id, '⚠️ ' + err)
        return
    set_setting(key, value)
    admin_settings(chat_id, '✅ ذخیره شد.')


def admin_pending(chat_id):
    rows = list_pending()
    if not rows:
        send_message(chat_id, '✅ شارژ در انتظاری نیست.',
                     {'inline_keyboard': [[{'text': '⬅️ پنل ادمین', 'callback_data': 'ad:panel'}]]})
        return
    for req_id, user_id, amount, photo_id in rows:
        caption = '🧾 <b>درخواست شارژ #%s</b>\n🆔 <code>%s</code>\n💰 مبلغ: <b>%s</b> تومان' % (req_id, user_id, fmt(amount))
        kb = {'inline_keyboard': [[{'text': '✅ تایید', 'callback_data': 'ap:%s' % req_id},
                                   {'text': '❌ رد', 'callback_data': 'rj:%s' % req_id}]]}
        if not (photo_id and send_photo(chat_id, photo_id, caption, kb).get('ok')):
            send_message(chat_id, caption, kb)


def admin_wallet_start(chat_id):
    user_steps[chat_id] = {'step': 'admin_wallet_id'}
    send_message(chat_id, '💰 آیدی عددی کاربر رو بفرست:',
                 {'inline_keyboard': [[{'text': '❌ انصراف', 'callback_data': 'ad:panel'}]]})


def admin_wallet_got_id(chat_id, text):
    uid = parse_int(text)
    if uid is None:
        send_message(chat_id, '⚠️ آیدی عددی معتبر بفرست.')
        return
    user_steps[chat_id] = {'step': 'admin_wallet_amount', 'uid': uid}
    send_message(chat_id, '👤 کاربر <code>%s</code> — موجودی فعلی: <b>%s</b> تومان\n\nمبلغ تغییر رو بفرست '
                 '(مثلاً <code>50000</code> برای افزودن، <code>-20000</code> برای کم‌کردن):' % (uid, fmt(get_wallet(uid))),
                 {'inline_keyboard': [[{'text': '❌ انصراف', 'callback_data': 'ad:panel'}]]})


def admin_wallet_got_amount(chat_id, text):
    uid = (user_steps.get(chat_id) or {}).get('uid')
    t = re.sub(r'[,\s٬،]', '', (text or '').translate(_DIGITS))
    if uid is None or not re.fullmatch(r'-?\d{1,12}', t) or int(t) == 0:
        send_message(chat_id, '⚠️ یه عدد معتبر بفرست (منفی = کم‌کردن).')
        return
    amount = int(t)
    if amount < 0 and get_wallet(uid) + amount < 0:
        send_message(chat_id, '⚠️ موجودی کاربر کمتر از این مبلغه (%s تومان).' % fmt(get_wallet(uid)))
        return
    add_wallet(uid, amount)
    user_steps.pop(chat_id, None)
    send_message(chat_id, '✅ موجودی کاربر <code>%s</code> الان <b>%s</b> تومانه.' % (uid, fmt(get_wallet(uid))),
                 {'inline_keyboard': [[{'text': '⬅️ پنل ادمین', 'callback_data': 'ad:panel'}]]})
    send_message(uid, '💰 موجودی کیف پولت توسط ادمین %s تومان %s.'
                 % (fmt(abs(amount)), 'افزایش یافت' if amount > 0 else 'کاهش یافت'))


def admin_callback(chat_id, arg):
    p = arg.split(':')
    a = p[0]
    n = parse_int(p[1]) if len(p) > 1 else None      # آرگومان عددیِ رایج (آیدی/آفست)
    if a == 'panel':
        admin_panel(chat_id)
    elif a == 'set':
        admin_settings(chat_id)
    elif a == 'edit':
        admin_edit_start(chat_id, arg[5:])
    elif a == 'val':
        _, key, val = (arg.split(':', 2) + ['', ''])[:3]
        if key == 'proto' and val in PROTO_CHOICES:
            set_setting(key, val)
            admin_settings(chat_id, '✅ ذخیره شد.')
    elif a == 'pending':
        admin_pending(chat_id)
    elif a == 'wallet':
        admin_wallet_start(chat_id)
    elif a == 'users':
        admin_users(chat_id, n or 0)
    elif a == 'usearch':
        user_steps[chat_id] = {'step': 'admin_usearch'}
        send_message(chat_id, '🔎 آیدی عددی یا @یوزرنیم کاربر رو بفرست:', _kb([_btn('❌ انصراف', 'ad:users:0')]))
    elif a == 'u' and n is not None:
        admin_user_detail(chat_id, n)
    elif a == 'uw' and n is not None:
        admin_wallet_got_id(chat_id, str(n))
    elif a == 'um' and n is not None:
        user_steps[chat_id] = {'step': 'admin_msg', 'uid': n}
        send_message(chat_id, '✉️ متن پیام برای کاربر <code>%s</code> رو بفرست:' % n, _kb([_btn('❌ انصراف', 'ad:u:%s' % n)]))
    elif a == 'uc' and n is not None:
        admin_user_configs(chat_id, n)
    elif a == 'ug' and n is not None:
        user_steps[chat_id] = {'step': 'admin_gift', 'uid': n}
        send_message(chat_id, '🎁 کانفیگ هدیه برای <code>%s</code>\n\nاین شکلی بفرست: <code>گیگ روز [نام]</code>\n'
                     'مثلاً <code>10 30 هدیه</code>\n(از موجودی کاربر کم نمی‌شه؛ قیمتش صفر ثبت می‌شه)' % n,
                     _kb([_btn('❌ انصراف', 'ad:u:%s' % n)]))
    elif a == 'c' and n is not None:
        admin_cfg_detail(chat_id, n)
    elif a == 'cx' and n is not None:
        user_steps[chat_id] = {'step': 'admin_extend', 'rid': n}
        send_message(chat_id, '➕ مقدار تمدید رو بفرست: <code>گیگ روز</code>\nمثلاً <code>5 10</code> (فقط روز: <code>0 10</code>)',
                     _kb([_btn('❌ انصراف', 'ad:c:%s' % n)]))
    elif a == 'cn' and n is not None:
        user_steps[chat_id] = {'step': 'admin_rename', 'rid': n}
        send_message(chat_id, '✏️ اسم جدید کانفیگ رو بفرست:', _kb([_btn('❌ انصراف', 'ad:c:%s' % n)]))
    elif a == 'cp' and n is not None:
        admin_cfg_toggle(chat_id, n)
    elif a == 'cr' and n is not None:
        admin_cfg_rotate(chat_id, n)
    elif a == 'cd' and n is not None:
        admin_cfg_delete_ask(chat_id, n)
    elif a == 'cdy' and n is not None and len(p) >= 4:
        admin_cfg_delete_do(chat_id, n, p[2] == '1', p[3] == '1')
    elif a == 'orders':
        admin_orders(chat_id, n or 0)
    elif a == 'fin':
        admin_finance(chat_id)
    elif a == 'tops':
        admin_topups(chat_id)
    elif a == 'bc':
        admin_bcast_start(chat_id)
    elif a == 'bcok':
        admin_bcast_go(chat_id)
    elif a == 'mt':
        admin_toggle_maintenance(chat_id)


def admin_step(chat_id, step, text, msg):
    """متن/عکسی که ادمین تو یکی از مرحله‌های پنل می‌فرسته."""
    if step == 'admin_edit':
        admin_got_value(chat_id, text)
    elif step == 'admin_wallet_id':
        admin_wallet_got_id(chat_id, text)
    elif step == 'admin_wallet_amount':
        admin_wallet_got_amount(chat_id, text)
    elif step == 'admin_usearch':
        admin_user_search(chat_id, text)
    elif step == 'admin_msg':
        admin_msg_got(chat_id, text)
    elif step == 'admin_gift':
        admin_gift_got(chat_id, text)
    elif step == 'admin_extend':
        admin_cfg_extend_got(chat_id, text)
    elif step == 'admin_rename':
        admin_cfg_rename_got(chat_id, text)
    elif step == 'admin_bcast':
        admin_bcast_got(chat_id, text)



# ---------------------------------------------------------------------------
# امکانات جدید پنل ادمین (کاربران، کانفیگ‌ها، سفارش‌ها، مالی، پیام همگانی، تعمیر)
# ---------------------------------------------------------------------------
import secrets

USERS_PAGE = 8
ORDERS_PAGE = 10
BROADCAST = {'running': False, 'total': 0, 'sent': 0, 'failed': 0}


@with_db_retry
def db_all(sql, params=()):
    c = get_conn().cursor()
    c.execute(sql, params)
    return c.fetchall()


@with_db_retry
def db_exec(sql, params=()):
    conn = get_conn()
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    return c.lastrowid


def _num(s):
    try:
        return float(str(s).translate(_DIGITS).replace('٫', '.').replace(',', '.'))
    except (TypeError, ValueError):
        return None


def _kb(*rows):
    return {'inline_keyboard': [list(r) for r in rows]}


def _btn(text, data):
    return {'text': text, 'callback_data': data}


def _back_panel():
    return _kb([_btn('⬅️ پنل ادمین', 'ad:panel')])


def cfg_call(method, suffix, config_id, body=None):
    if not (CONFIG_API and CONFIG_KEY):
        return {'success': False, 'error': 'CONFIG_API / CONFIG_KEY تنظیم نشده'}
    try:
        res = SESSION.request(method, '%s/%s%s' % (CONFIG_API, config_id, suffix),
                              headers={'Authorization': 'Bearer ' + CONFIG_KEY}, json=body, timeout=20)
        try:
            data = res.json()
        except Exception:
            data = {}
        if res.status_code in (200, 201, 204):
            return {'success': True, 'data': data if isinstance(data, dict) else {}}
        return {'success': False, 'error': (res.text or str(res.status_code))[:200]}
    except Exception as e:
        return {'success': False, 'error': str(e)[:200]}


def maintenance_on():
    return get_setting('maintenance', '0') == '1'


def maintenance_text():
    return get_setting('maintenance_text', '🛠 ربات موقتاً در حال بروزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.')


# ---------------------------- کاربران ----------------------------
def admin_users(chat_id, offset=0):
    offset = max(0, offset)
    total = db_all('SELECT COUNT(*) FROM users')[0][0]
    rows = db_all('SELECT user_id, username, first_name, wallet FROM users '
                  'ORDER BY joined_at DESC, user_id DESC LIMIT ? OFFSET ?', (USERS_PAGE, offset))
    kb = []
    for uid, uname, fname, wallet in rows:
        name = (fname or '').strip() or (('@' + uname) if uname else str(uid))
        kb.append([_btn('👤 %s · %s ت' % (name[:18], fmt(wallet or 0)), 'ad:u:%s' % uid)])
    nav = []
    if offset > 0:
        nav.append(_btn('‹ قبل', 'ad:users:%s' % max(0, offset - USERS_PAGE)))
    if offset + USERS_PAGE < total:
        nav.append(_btn('بعد ›', 'ad:users:%s' % (offset + USERS_PAGE)))
    if nav:
        kb.append(nav)
    kb.append([_btn('🔎 جستجو (آیدی یا @یوزرنیم)', 'ad:usearch')])
    kb.append([_btn('⬅️ پنل ادمین', 'ad:panel')])
    send_message(chat_id, '👥 <b>کاربران</b> (%s نفر)\n%s' % (fmt(total), DIVIDER), {'inline_keyboard': kb})


def admin_user_search(chat_id, text):
    t = (text or '').strip().translate(_DIGITS).lstrip('@')
    if not t:
        return
    uid = parse_int(t)
    row = None
    if uid is not None:
        r = db_all('SELECT user_id FROM users WHERE user_id=?', (uid,))
        row = r[0] if r else None
    if row is None:
        r = db_all('SELECT user_id FROM users WHERE LOWER(username)=?', (t.lower(),))
        row = r[0] if r else None
    if row is None:
        send_message(chat_id, '⚠️ کاربری پیدا نشد. دوباره بفرست یا انصراف بزن.',
                     _kb([_btn('❌ انصراف', 'ad:users:0')]))
        return
    user_steps.pop(chat_id, None)
    admin_user_detail(chat_id, row[0])


def admin_user_detail(chat_id, uid, note=None):
    user_steps.pop(chat_id, None)
    r = db_all('SELECT user_id, username, first_name, wallet, joined_at FROM users WHERE user_id=?', (uid,))
    if not r:
        send_message(chat_id, '⚠️ کاربر پیدا نشد.', _back_panel())
        return
    _, uname, fname, wallet, joined = r[0]
    count, gb, price = purchase_totals(uid)
    text = ('%s👤 <b>%s</b> %s\n%s\n🆔 <code>%s</code>\n💰 موجودی: <b>%s</b> تومان\n📦 کانفیگ‌ها: <b>%s</b> | %.1f گیگ\n'
            '💸 مجموع خرید: <b>%s</b> تومان\n📅 عضویت: %s'
            % ((note + '\n\n') if note else '', esc(fname or '-'), esc(('@' + uname) if uname else ''), DIVIDER, uid,
               fmt(wallet or 0), count, gb, fmt(price), esc((joined or '-')[:10])))
    send_message(chat_id, text, _kb(
        [_btn('💰 تغییر موجودی', 'ad:uw:%s' % uid), _btn('✉️ پیام به کاربر', 'ad:um:%s' % uid)],
        [_btn('📦 کانفیگ‌ها', 'ad:uc:%s' % uid), _btn('🎁 کانفیگ هدیه', 'ad:ug:%s' % uid)],
        [_btn('⬅️ کاربران', 'ad:users:0')]))


# ---------------------------- کانفیگ‌ها ----------------------------
def admin_user_configs(chat_id, uid):
    rows = db_all('SELECT id, label, gb, status FROM user_configs WHERE user_id=? ORDER BY id DESC LIMIT 30', (uid,))
    kb = [[_btn('%s %s (%g گیگ)' % ('⏸' if (r[3] or 'active') == 'paused' else '📄', (r[1] or 'کانفیگ')[:20], r[2]),
                'ad:c:%s' % r[0])] for r in rows]
    kb.append([_btn('🎁 ساخت کانفیگ هدیه', 'ad:ug:%s' % uid)])
    kb.append([_btn('⬅️ کاربر', 'ad:u:%s' % uid)])
    send_message(chat_id, '📦 <b>کانفیگ‌های کاربر</b> <code>%s</code>%s' % (uid, '' if rows else '\n\nکانفیگی ندارد.'),
                 {'inline_keyboard': kb})


def _cfg_row(rid):
    r = db_all('SELECT id, user_id, sub_url, config_id, gb, label, days, price, type, status, expires_at '
               'FROM user_configs WHERE id=?', (rid,))
    return r[0] if r else None


def admin_cfg_detail(chat_id, rid, note=None):
    user_steps.pop(chat_id, None)
    row = _cfg_row(rid)
    if not row:
        send_message(chat_id, '⚠️ کانفیگ پیدا نشد.', _back_panel())
        return
    _, uid, sub_url, config_id, gb, label, days, price, ctype, status, expires_at = row
    used = config_usage_gb(config_id)
    left = days_left(expires_at)
    paused = (status or 'active') == 'paused'
    lines = ['%s📄 <b>%s</b> %s' % ((note + '\n\n') if note else '', esc(label or 'کانفیگ'), '⏸ متوقف' if paused else ''),
             DIVIDER, '👤 کاربر: <code>%s</code>' % uid, '🔌 نوع: %s' % esc(ctype or '-'),
             '📶 حجم: <b>%g</b> گیگ' % (gb or 0), '💸 پرداختی: <b>%s</b> تومان' % fmt(price or 0)]
    if used is not None:
        lines.append('📊 مصرف: %s <b>%.2f</b>' % (usage_bar(used, gb or 0), used))
    if left is not None:
        lines.append('⏳ مانده: <b>%s</b> روز' % left)
    lines += ['', '🔗 <code>%s</code>' % esc(sub_url)]
    send_message(chat_id, '\n'.join(lines), _kb(
        [_btn('➕ تمدید', 'ad:cx:%s' % rid), _btn('✏️ تغییر نام', 'ad:cn:%s' % rid)],
        [_btn('▶️ ازسرگیری' if paused else '⏸ توقف', 'ad:cp:%s' % rid), _btn('🔄 تغییر لینک', 'ad:cr:%s' % rid)],
        [_btn('🗑 حذف', 'ad:cd:%s' % rid)],
        [_btn('⬅️ کانفیگ‌های کاربر', 'ad:uc:%s' % uid)]))


def admin_cfg_toggle(chat_id, rid):
    row = _cfg_row(rid)
    if not row:
        return
    paused = (row[9] or 'active') == 'paused'
    r = cfg_call('POST', '/' + ('resume' if paused else 'pause'), row[3])
    if not r['success']:
        send_message(chat_id, '⚠️ عملیات روی پنل ناموفق: <code>%s</code>' % esc(r['error']))
        return
    db_exec("UPDATE user_configs SET status=? WHERE id=?", ('active' if paused else 'paused', rid))
    admin_cfg_detail(chat_id, rid, '✅ ازسرگرفته شد.' if paused else '✅ متوقف شد.')


def admin_cfg_rotate(chat_id, rid):
    row = _cfg_row(rid)
    if not row:
        return
    r = cfg_call('POST', '/rotate-link', row[3])
    if not r['success']:
        send_message(chat_id, '⚠️ تغییر لینک ناموفق: <code>%s</code>' % esc(r['error']))
        return
    d = r['data']
    new_url = d.get('sub_url') or d.get('subUrl') or d.get('url')
    if not new_url:
        admin_cfg_detail(chat_id, rid, 'ℹ️ لینک تغییر کرد ولی پنل لینک جدید رو برنگردوند.')
        return
    db_exec('UPDATE user_configs SET sub_url=? WHERE id=?', (new_url, rid))
    send_message(row[1], '🔄 لینک کانفیگ «%s» تغییر کرد:\n<code>%s</code>' % (esc(row[5]), esc(new_url)))
    admin_cfg_detail(chat_id, rid, '✅ لینک عوض شد و برای کاربر ارسال شد.')


def admin_cfg_extend_got(chat_id, text):
    rid = (user_steps.get(chat_id) or {}).get('rid')
    parts = [p for p in re.split(r'[\s,،]+', (text or '').strip()) if p]
    gb, days = (_num(parts[0]), _num(parts[1])) if len(parts) == 2 else (None, None)
    if gb is None or days is None or gb < 0 or days < 0 or (gb == 0 and days == 0) or gb > 100000 or days > 3650:
        send_message(chat_id, '⚠️ دو عدد بفرست: <code>گیگ روز</code> (مثلاً <code>5 10</code>؛ هر دو نمی‌تونن صفر باشن).')
        return
    row = _cfg_row(rid)
    if not row:
        return
    days = int(days)
    r = cfg_call('POST', '/extend', row[3], {'gb': gb, 'days': days})
    if not r['success']:
        send_message(chat_id, '⚠️ تمدید روی پنل ناموفق: <code>%s</code>' % esc(r['error']))
        return
    try:
        base = datetime.fromisoformat(row[10])
    except Exception:
        base = datetime.now()
    db_exec('UPDATE user_configs SET gb=?, days=?, expires_at=? WHERE id=?',
            ((row[4] or 0) + gb, (row[6] or 0) + days, (base + timedelta(days=days)).isoformat(), rid))
    admin_cfg_detail(chat_id, rid, '✅ تمدید شد: +%g گیگ، +%s روز.' % (gb, days))


def admin_cfg_rename_got(chat_id, text):
    rid = (user_steps.get(chat_id) or {}).get('rid')
    new = re.sub(r'[<>&]', '', (text or '')).strip()[:40]
    row = _cfg_row(rid)
    if not new or not row:
        send_message(chat_id, '⚠️ یه اسم معتبر بفرست.')
        return
    r = cfg_call('PUT', '/label', row[3], {'label': new})
    if not r['success']:
        send_message(chat_id, '⚠️ تغییر نام روی پنل ناموفق: <code>%s</code>' % esc(r['error']))
        return
    db_exec('UPDATE user_configs SET label=? WHERE id=?', (new, rid))
    admin_cfg_detail(chat_id, rid, '✅ نام تغییر کرد.')


def admin_cfg_delete_ask(chat_id, rid):
    row = _cfg_row(rid)
    if not row:
        return
    send_message(chat_id, '🗑 کانفیگ «%s» حذف بشه؟ غیرقابل بازگشته.\nپرداختی کاربر: <b>%s</b> تومان'
                 % (esc(row[5]), fmt(row[7] or 0)), _kb(
        [_btn('🗑 حذف (بدون بازگشت وجه)', 'ad:cdy:%s:0:0' % rid)],
        [_btn('🗑 حذف + برگشت کامل مبلغ', 'ad:cdy:%s:1:0' % rid)],
        [_btn('❌ انصراف', 'ad:c:%s' % rid)]))


def admin_cfg_delete_do(chat_id, rid, refund, force):
    row = _cfg_row(rid)
    if not row:
        send_message(chat_id, 'ℹ️ کانفیگ قبلاً حذف شده.', _back_panel())
        return
    r = cfg_call('DELETE', '', row[3])
    if not r['success'] and not force:
        send_message(chat_id, '⚠️ حذف روی پنل ناموفق: <code>%s</code>' % esc(r['error']),
                     _kb([_btn('🗑 حذف اجباری فقط از دیتابیس', 'ad:cdy:%s:%s:1' % (rid, refund))],
                         [_btn('⬅️ برگشت', 'ad:c:%s' % rid)]))
        return
    db_exec('DELETE FROM user_configs WHERE id=?', (rid,))
    msg = '✅ کانفیگ حذف شد.'
    if refund and (row[7] or 0) > 0:
        add_wallet(row[1], int(row[7]))
        msg += ' %s تومان به کیف پول کاربر برگشت.' % fmt(row[7])
        send_message(row[1], '💰 کانفیگ «%s» حذف شد و %s تومان به کیف پولت برگشت.' % (esc(row[5]), fmt(row[7])))
    send_message(chat_id, msg, _kb([_btn('⬅️ کانفیگ‌های کاربر', 'ad:uc:%s' % row[1])]))


def admin_gift_got(chat_id, text):
    uid = (user_steps.get(chat_id) or {}).get('uid')
    parts = [p for p in re.split(r'\s+', (text or '').strip()) if p]
    gb, days = (_num(parts[0]), _num(parts[1])) if len(parts) >= 2 else (None, None)
    if gb is None or days is None or not (0 < gb <= 100000) or not (1 <= days <= 3650):
        send_message(chat_id, '⚠️ این شکلی بفرست: <code>گیگ روز [نام]</code> (مثلاً <code>10 30 هدیه</code>).')
        return
    days = int(days)
    label = re.sub(r'[<>&]', '', ' '.join(parts[2:])).strip()[:40] or 'GIFT-' + secrets.token_hex(3).upper()
    user_steps.pop(chat_id, None)
    proto = config_proto()
    res = make_config(gb, label, days, proto)
    if not res['success'] or not res.get('sub_url'):
        send_message(chat_id, '⚠️ ساخت کانفیگ ناموفق بود (CONFIG_API / CONFIG_KEY رو چک کن).', _back_panel())
        return
    save_config(uid, res['sub_url'], res['config_id'], gb, label, days, 0, proto)
    BG_EXECUTOR.submit(set_config_note, res['config_id'], 'هدیه/دستی از پنل ادمین بات')
    send_message(uid, '🎁 <b>یک کانفیگ برات ساخته شد</b>\n📶 %g گیگ | ⏳ %s روز\n🔗 <code>%s</code>'
                 % (gb, days, esc(res['sub_url'])))
    send_message(chat_id, '✅ کانفیگ «%s» برای <code>%s</code> ساخته و ارسال شد.' % (esc(label), uid),
                 _kb([_btn('⬅️ کاربر', 'ad:u:%s' % uid)]))


def admin_msg_got(chat_id, text):
    uid = (user_steps.get(chat_id) or {}).get('uid')
    if not (text or '').strip() or uid is None:
        return
    user_steps.pop(chat_id, None)
    r = send_message(uid, text.strip())
    send_message(chat_id, '✅ پیام ارسال شد.' if r.get('ok') else '⚠️ ارسال نشد (شاید کاربر بات رو بلاک کرده).',
                 _kb([_btn('⬅️ کاربر', 'ad:u:%s' % uid)]))


# ---------------------------- سفارش‌ها، مالی ----------------------------
def admin_orders(chat_id, offset=0):
    offset = max(0, offset)
    tot = db_all('SELECT COUNT(*), COALESCE(SUM(price),0), COALESCE(SUM(gb),0) FROM user_configs')[0]
    rows = db_all('SELECT id, user_id, label, gb, days, price, created_at FROM user_configs '
                  'ORDER BY id DESC LIMIT ? OFFSET ?', (ORDERS_PAGE, offset))
    lines = ['📦 <b>سفارش‌ها</b>', DIVIDER, 'تعداد: <b>%s</b> | فروش: <b>%s</b> تومان | حجم: <b>%.1f</b> گیگ'
             % (fmt(tot[0]), fmt(tot[1]), float(tot[2])), DIVIDER]
    for rid, uid, label, gb, days, price, created in rows:
        lines.append('#%s · <code>%s</code> · %s · %g گیگ/%sروز · %s ت · %s'
                     % (rid, uid, esc((label or '')[:16]), gb or 0, days, fmt(price or 0), esc((created or '')[:10])))
    nav = []
    if offset > 0:
        nav.append(_btn('‹ قبل', 'ad:orders:%s' % max(0, offset - ORDERS_PAGE)))
    if offset + ORDERS_PAGE < tot[0]:
        nav.append(_btn('بعد ›', 'ad:orders:%s' % (offset + ORDERS_PAGE)))
    kb = ([nav] if nav else []) + [[_btn('⬅️ پنل ادمین', 'ad:panel')]]
    send_message(chat_id, '\n'.join(lines), {'inline_keyboard': kb})


def admin_finance(chat_id):
    now = datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def span(since):
        s = since.isoformat()
        t = db_all("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM topup_requests WHERE status='approved' AND created_at >= ?", (s,))[0]
        o = db_all('SELECT COALESCE(SUM(price),0), COUNT(*) FROM user_configs WHERE price > 0 AND created_at >= ?', (s,))[0]
        return int(t[0]), int(t[1]), int(o[0]), int(o[1])
    lines = ['📊 <b>گزارش مالی</b>', DIVIDER]
    for title, since in (('امروز', today0), ('۷ روز اخیر', today0 - timedelta(days=6)), ('۳۰ روز اخیر', today0 - timedelta(days=29))):
        ta, tn, sa, sn = span(since)
        lines.append('<b>%s</b>\n   💳 شارژ: %s ت (%s بار)\n   🛒 فروش: %s ت (%s سفارش)' % (title, fmt(ta), tn, fmt(sa), sn))
    days = [(today0 - timedelta(days=i)) for i in range(6, -1, -1)]
    since = days[0].isoformat()
    per = {d.date().isoformat(): 0 for d in days}
    for price, created in db_all('SELECT price, created_at FROM user_configs WHERE price > 0 AND created_at >= ?', (since,)):
        k = (created or '')[:10]
        if k in per:
            per[k] += int(price or 0)
    mx = max(per.values()) or 1
    lines += [DIVIDER, '🛒 <b>فروش ۷ روز اخیر</b>']
    for k, v in per.items():
        lines.append('<code>%s</code> %s %s' % (k[5:], usage_bar(v, mx), fmt(v)))
    send_message(chat_id, '\n'.join(lines), _kb([_btn('🧾 آخرین شارژها', 'ad:tops')], [_btn('⬅️ پنل ادمین', 'ad:panel')]))


def admin_topups(chat_id):
    rows = db_all('SELECT id, user_id, amount, status, created_at FROM topup_requests ORDER BY id DESC LIMIT 15')
    icons = {'pending': '⏳', 'approved': '✅', 'rejected': '❌', 'failed': '⚠️'}
    lines = ['🧾 <b>آخرین شارژها</b>', DIVIDER] + [
        '%s #%s · <code>%s</code> · %s ت · %s' % (icons.get(r[3], '•'), r[0], r[1], fmt(r[2] or 0), esc((r[4] or '')[:10]))
        for r in rows]
    if not rows:
        lines.append('—')
    send_message(chat_id, '\n'.join(lines), _kb([_btn('📋 در انتظار تایید', 'ad:pending')], [_btn('⬅️ پنل ادمین', 'ad:panel')]))


# ---------------------------- پیام همگانی ----------------------------
def broadcast_worker(admin_chat, text, ids):
    BROADCAST.update(running=True, total=len(ids), sent=0, failed=0)
    for uid in ids:
        try:
            ok = bool(send_message(uid, text).get('ok'))
        except Exception:
            ok = False
        BROADCAST['sent' if ok else 'failed'] += 1
        time.sleep(0.05)
    BROADCAST['running'] = False
    send_message(admin_chat, '✅ ارسال همگانی تموم شد.\nموفق: <b>%s</b> | ناموفق: <b>%s</b>' % (BROADCAST['sent'], BROADCAST['failed']),
                 _back_panel())


def admin_bcast_start(chat_id):
    if BROADCAST['running']:
        send_message(chat_id, '⏳ یه ارسال همگانی هنوز در حال انجامه (%s از %s).'
                     % (BROADCAST['sent'] + BROADCAST['failed'], BROADCAST['total']), _back_panel())
        return
    user_steps[chat_id] = {'step': 'admin_bcast'}
    send_message(chat_id, '📢 متن پیام همگانی رو بفرست (HTML مجازه: &lt;b&gt; &lt;i&gt; &lt;code&gt;):',
                 _kb([_btn('❌ انصراف', 'ad:panel')]))


def admin_bcast_got(chat_id, text):
    text = (text or '').strip()
    if not text:
        return
    n = db_all('SELECT COUNT(*) FROM users')[0][0]
    user_steps[chat_id] = {'step': 'admin_bcast_confirm', 'text': text}
    send_message(chat_id, '👁 <b>پیش‌نمایش</b>\n%s\n%s\n%s\n\nبرای <b>%s</b> کاربر ارسال بشه؟' % (DIVIDER, text, DIVIDER, fmt(n)),
                 _kb([_btn('✅ ارسال', 'ad:bcok'), _btn('❌ انصراف', 'ad:panel')]))


def admin_bcast_go(chat_id):
    st = user_steps.get(chat_id) or {}
    if st.get('step') != 'admin_bcast_confirm' or BROADCAST['running']:
        return
    user_steps.pop(chat_id, None)
    ids = [r[0] for r in db_all('SELECT user_id FROM users')]
    threading.Thread(target=broadcast_worker, args=(chat_id, st['text'], ids), daemon=True).start()
    send_message(chat_id, '🚀 ارسال برای %s کاربر شروع شد؛ بعد از تموم‌شدن بهت خبر می‌دم.' % fmt(len(ids)), _back_panel())


def admin_toggle_maintenance(chat_id):
    set_setting('maintenance', '0' if maintenance_on() else '1')
    admin_panel(chat_id)


# ---------------------------------------------------------------------------
# پردازش آپدیت‌ها
# ---------------------------------------------------------------------------
def ensure_user(chat_id, frm):
    if chat_id not in _known_users:
        upsert_user(chat_id, frm.get('username'), frm.get('first_name'))
        _known_users.add(chat_id)


def handle_message(msg):
    chat_id = (msg.get('chat') or {}).get('id')
    frm = msg.get('from') or {}
    if chat_id is None:
        return
    ensure_user(chat_id, frm)
    if not is_admin(chat_id) and maintenance_on():
        send_message(chat_id, maintenance_text())
        return
    text = (msg.get('text') or '').strip()
    if text.startswith('/'):
        cmd, _, arg = text.partition(' ')
        cmd = cmd.split('@')[0].lower()
        if cmd in ('/start', '/menu'):
            send_menu(chat_id)
            return
        if cmd == '/admin' and is_admin(chat_id):
            admin_panel(chat_id)
            return
    step = (user_steps.get(chat_id) or {}).get('step')
    if step == 'topup_amount':
        topup_got_amount(chat_id, text)
    elif step == 'topup_receipt':
        topup_got_receipt(chat_id, frm, msg)
    elif step == 'buy_label' and text:
        buy_invoice(chat_id, text)
    elif step and step.startswith('admin_') and is_admin(chat_id):
        admin_step(chat_id, step, text, msg)
    else:
        send_menu(chat_id)


def handle_callback(cb):
    answer_callback(cb.get('id'))
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    data = cb.get('data') or ''
    frm = cb.get('from') or {}
    if chat_id is None:
        return
    ensure_user(chat_id, frm)
    if not is_admin(chat_id) and maintenance_on():
        send_message(chat_id, maintenance_text())
        return
    if data.startswith('ad:'):
        if is_admin(chat_id):
            admin_callback(chat_id, data[3:])
        return
    if data[:3] in ('ap:', 'rj:'):
        if is_admin(chat_id):
            req_id = parse_int(data.split(':', 1)[1]) if ':' in data else None
            if req_id is not None:
                admin_decide(chat_id, req_id, data.startswith('ap'))
        return
    if data == 'menu':
        send_menu(chat_id)
    elif data == 'topup':
        topup_start(chat_id)
    elif data == 'account':
        user_steps.pop(chat_id, None)
        show_account(chat_id)
    elif data.startswith('cfg:'):
        rid = parse_int(data[4:])
        if rid is not None:
            show_config(chat_id, rid)
    elif data == 'buy':
        buy_start(chat_id)
    elif data.startswith('q:'):
        if data == 'q:ok':
            buy_ask_label(chat_id)
        else:
            buy_qty_change(chat_id, msg.get('message_id'), data[2:])
    elif data == 'skip_label':
        buy_invoice(chat_id, None)
    elif data == 'buy_yes':
        buy_confirm(chat_id, frm)


def safe_process_update(update):
    cb = update.get('callback_query')
    msg = update.get('message')
    chat_id = ((cb or {}).get('message') or {}).get('chat', {}).get('id') if cb else (msg or {}).get('chat', {}).get('id')
    try:
        with (get_chat_lock(chat_id) if chat_id is not None else threading.Lock()):
            if cb:
                handle_callback(cb)
            elif msg:
                handle_message(msg)
    except Exception as e:
        print('⚠️ خطا در پردازش آپدیت:', repr(e))
        reset_conn()
        if chat_id is not None:
            send_message(chat_id, '⚠️ مشکلی پیش اومد، دوباره امتحان کن.', back_kb())


def start_health_server(port):
    """سرور HTTP مینیمال («ok») برای سرویس‌هایی که پورت زنده می‌خوان."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), _H)
    except OSError as e:
        print("⚠️ health server بالا نیومد:", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    init_db()
    api('deleteWebhook', {'drop_pending_updates': False}, 10)
    if not ADMIN_ID:
        print('⚠️ ADMIN_ID تنظیم نشده؛ تایید شارژ و دستورهای ادمین کار نمی‌کنن.')
    if os.getenv("DISABLE_HEALTH_SERVER") != "1":
        start_health_server(int(os.getenv("PORT", "8080")))
    print('✅ ربات اجرا شد.')
    offset = 0
    while True:
        try:
            started = time.time()
            data = get_updates(offset + 1 if offset else None)
            if data.get('ok') and data.get('result'):
                for update in data['result']:
                    offset = update['update_id']
                    UPDATE_EXECUTOR.submit(safe_process_update, update)
            elif time.time() - started < 3:
                time.sleep(1 if not data.get('ok') else 0.2)
        except Exception as e:
            print('⚠️ خطای غیرمنتظره در حلقه اصلی:', e)
            time.sleep(2)


if __name__ == '__main__':
    main()
