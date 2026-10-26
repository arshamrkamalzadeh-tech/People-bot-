#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات مدیریت (Master) برای کنترل بات‌های فروشی روی سروش.

این بات به همون دیتابیس Turso وصل می‌شه و بات‌ها (b1_، b2_، ...) رو خودکار پیدا می‌کنه.
تنظیماتِ لایسنس هر بات (سقف حجم، توقف، انقضا، محدودیت‌ها، قابلیت‌ها، اعلان) تو جدول settings همون بات
نوشته می‌شه و خود بات‌ها همون لحظه (تا ~۲۰ ثانیه) اعمالش می‌کنن.
قیمت هر گیگ و شماره کارت عمداً از اینجا قابل تغییر نیست؛ ادمین هر بات خودش تو بات خودش عوض می‌کنه.

Environment لازم:
  MASTER_BOT_TOKEN   توکن همین بات مدیریت
  MASTER_ADMIN_IDS   آیدی عددی ادمین(ها)؛ چندتا رو با کاما جدا کن
اختیاری:
  TURSO_DATABASE_URL / TURSO_AUTH_TOKEN   (پیش‌فرض: همون دیتابیس داخل کد)
  BOT_TOKEN1..N و ADMIN_ID1..N            برای «پیام به ادمین بات» (از طریق خود اون بات)
  NOTIFY_RESELLERS=1                       هشدار خودکار کم‌شدن حجم/انقضا به ادمین هر بات (پیش‌فرض خاموش)
  LOW_QUOTA_GB=5                           آستانه‌ی هشدار «حجم کم»
  PORT                                     برای هاست‌هایی که پورت می‌خوان
"""
import os
import re
import json
import time
import threading
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor

import requests
import turso_serverless

_DEFAULT_TURSO_URL = "libsql://people-bot-mikekamalzadeh-sys.aws-eu-west-1.turso.io"
_DEFAULT_TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTI5NTk4MjUsImlkIjoiMDFhMTc0YjktZWYwMS03ZWVmLTk0Y2YtYWM1YmFjMzI1Y2JmIiwia2lkIjoiNmltOXd5bGxUd2luNkVHWEJWNHVJUE01ZWNPM2JyZmJ3NzFqWTFIanFCTSIsInJpZCI6ImMwYmRlZjNkLTQ5ZDktNDBlOC1iNDBhLWM0M2ZjNGQxZjhhYiJ9.Px_-sbVDy08hiewnqKy3HJ0rR24u1PXPKDNRCNOzGF3muSubeKGaPpRxUtPaslJ87COj6paze91WFz6hrMtMAg"

TOKEN = os.getenv("MASTER_BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("متغیر محیطی MASTER_BOT_TOKEN تنظیم نشده.")
BASE_URL = "https://api.splus.ir/bot" + TOKEN
ADMINS = {int(x) for x in re.findall(r"\d+", os.getenv("MASTER_ADMIN_IDS", ""))}
if not ADMINS:
    raise SystemExit("متغیر محیطی MASTER_ADMIN_IDS (آیدی عددی ادمین) تنظیم نشده.")
TURSO_URL = (os.getenv("TURSO_DATABASE_URL") or _DEFAULT_TURSO_URL).strip()
TURSO_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or _DEFAULT_TURSO_TOKEN).strip()
LOW_QUOTA_GB = float(os.getenv("LOW_QUOTA_GB", "5") or 5)
NOTIFY_RESELLERS = os.getenv("NOTIFY_RESELLERS", "0") == "1"

DIVIDER = "┄" * 16
SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=30))
UPDATE_EXECUTOR = ThreadPoolExecutor(max_workers=6)
POOL = ThreadPoolExecutor(max_workers=6)
user_steps = {}
_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
_local = threading.local()


def _conn():
    c = getattr(_local, 'conn', None)
    if c is None:
        c = turso_serverless.connect(TURSO_URL, auth_token=TURSO_TOKEN)
        _local.conn = c
    return c


def _run(sql, params, write):
    for attempt in (1, 2):
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute(sql, params)
            if write:
                conn.commit()
                return cur.lastrowid
            return cur.fetchall()
        except Exception as e:
            if attempt == 1 and 'stream' in str(e).lower():
                _local.conn = None          # اتصال منقضی شده؛ یه بار دوباره
                continue
            raise


def q(sql, params=()):
    return _run(sql, params, False)


def x(sql, params=()):
    return _run(sql, params, True)


def init_db():
    x('CREATE TABLE IF NOT EXISTS m_bots (prefix TEXT PRIMARY KEY, label TEXT, customer TEXT, note TEXT)')
    x('CREATE TABLE IF NOT EXISTS m_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, prefix TEXT, admin_id INTEGER, '
      'action TEXT, detail TEXT)')
    x('CREATE TABLE IF NOT EXISTS m_pay (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, prefix TEXT, amount INTEGER, '
      'gb REAL, note TEXT)')


# ---------------------------------------------------------------------------
# پیدا کردن بات‌ها و خوندن/نوشتن تنظیماتشون
# ---------------------------------------------------------------------------
_bots_cache = {'ts': 0.0, 'list': []}


def discover(force=False):
    """لیست پیشوند جدول‌های بات‌ها: ['', 'b1_', 'b2_', ...] ('' = بات قدیمیِ بدون پیشوند)."""
    if not force and time.time() - _bots_cache['ts'] < 30:
        return _bots_cache['list']
    names = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    for n in names:
        m = re.fullmatch(r'(b\d+_)settings', n)
        if m and (m.group(1) + 'users') in names:
            out.append(m.group(1))
    if {'settings', 'users', 'user_configs'} <= names and 'b1_' not in out:   # بات ۱ اگه پیشوند گرفته، جدول‌های قدیمی مرده‌ان
        out.append('')
    out.sort(key=lambda p: int(re.sub(r'\D', '', p) or 0))
    _bots_cache.update(ts=time.time(), list=out)
    return out


def pid_of(prefix):
    return prefix.rstrip('_') or 'old'


def prefix_of(pid):
    if pid == 'old':
        p = ''
    elif re.fullmatch(r'b\d+', pid or ''):
        p = pid + '_'
    else:
        return None
    return p if p in discover() else None


def bot_no(prefix):
    return int(re.sub(r'\D', '', prefix) or 1)


def bcfg(prefix):
    return {r[0]: r[1] for r in q('SELECT key, value FROM %ssettings' % prefix)}


def bset(prefix, key, value):
    x('INSERT INTO %ssettings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value'
      % prefix, (key, str(value)))


def bstats(prefix):
    p = prefix
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    r = q("SELECT (SELECT COUNT(*) FROM %(p)susers), (SELECT COALESCE(SUM(wallet),0) FROM %(p)susers), "
          "(SELECT COUNT(*) FROM %(p)suser_configs), (SELECT COALESCE(SUM(price),0) FROM %(p)suser_configs), "
          "(SELECT COUNT(*) FROM %(p)stopup_requests WHERE status='pending'), "
          "(SELECT COALESCE(SUM(price),0) FROM %(p)suser_configs WHERE created_at >= ?), "
          "(SELECT COALESCE(SUM(gb),0) FROM %(p)suser_configs)" % {'p': p}, (today0,))[0]
    return dict(users=int(r[0] or 0), wallets=int(r[1] or 0), configs=int(r[2] or 0), sales=int(r[3] or 0),
                pending=int(r[4] or 0), today=int(r[5] or 0), cfg_gb=float(r[6] or 0))


def fnum(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def meta_of(prefix):
    r = q('SELECT label, customer, note FROM m_bots WHERE prefix=?', (prefix,))
    lab, cust, note = (r[0] if r else (None, None, None))
    default = 'بات %s' % bot_no(prefix) + (' (قدیمی)' if prefix == '' else '')
    return {'label': lab or default, 'customer': cust or '', 'note': note or ''}


def info(prefix, stats=True):
    """همه‌چیز یه بات: تنظیمات + آمار + وضعیت لایسنس."""
    cfg, meta = bcfg(prefix), meta_of(prefix)
    st = bstats(prefix) if stats else dict(users=0, wallets=0, configs=0, sales=0, pending=0, today=0, cfg_gb=0.0)
    quota = fnum(cfg.get('lic_quota_gb')) if str(cfg.get('lic_quota_gb', '')).strip() != '' else None
    sold = fnum(cfg.get('lic_sold_gb'), 0.0)
    hb = int(fnum(cfg.get('lic_heartbeat'), 0) or 0)
    exp = (cfg.get('lic_expires') or '').strip()
    days_to_exp = None
    if exp:
        try:
            days_to_exp = (datetime.strptime(exp, '%Y-%m-%d').date() - date.today()).days
        except ValueError:
            exp = ''
    return dict(prefix=prefix, cfg=cfg, st=st, meta=meta, quota=quota, sold=sold,
                left=None if quota is None else max(quota - sold, 0.0), hb=hb,
                online=bool(hb) and time.time() - hb < 180, active=cfg.get('lic_active', '1') != '0',
                exp=exp, days_to_exp=days_to_exp, expired=days_to_exp is not None and days_to_exp < 0,
                username=cfg.get('lic_username', ''))


def safe_info(prefix):
    try:
        return info(prefix)
    except Exception as e:
        print('⚠️ خوندن اطلاعات بات %s ناموفق بود: %r' % (prefix or 'old', e))
        return None


def audit(prefix, admin_id, action, detail=''):
    x('INSERT INTO m_log (ts, prefix, admin_id, action, detail) VALUES (?, ?, ?, ?, ?)',
      (datetime.now().isoformat(timespec='seconds'), prefix, admin_id, action, str(detail)[:200]))


# ---------------------------------------------------------------------------
# ابزارهای نمایش و API سروش
# ---------------------------------------------------------------------------
def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def fmt(n):
    try:
        return '{:,}'.format(int(n))
    except (TypeError, ValueError):
        return str(n)


def gbf(v):
    return ('%.2f' % v).rstrip('0').rstrip('.') if v is not None else '♾'


def num(s):
    try:
        return float(str(s).translate(_DIGITS).replace('٫', '.').replace(',', '.').strip())
    except (TypeError, ValueError):
        return None


def api(method, payload, timeout=15):
    try:
        return SESSION.post(BASE_URL + '/' + method, json=payload, timeout=timeout).json()
    except Exception as e:
        print('⚠️ خطای API', method, ':', e)
        return {'ok': False}


def send(chat_id, text, kb=None):
    p = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if kb:
        p['reply_markup'] = json.dumps(kb)
    return api('sendMessage', p)


def show(chat_id, mid, text, kb=None):
    """اگه از دکمه اومده باشیم همون پیام رو ویرایش می‌کنه، وگرنه پیام جدید می‌فرسته."""
    if mid:
        p = {'chat_id': chat_id, 'message_id': mid, 'text': text, 'parse_mode': 'HTML'}
        if kb:
            p['reply_markup'] = json.dumps(kb)
        r = api('editMessageText', p)
        if r.get('ok') or 'not modified' in json.dumps(r).lower():
            return
    send(chat_id, text, kb)


def B(text, data):
    return {'text': text, 'callback_data': data}


def KB(*rows):
    return {'inline_keyboard': [list(r) for r in rows if r]}


def badge(i):
    if not i['active']:
        return '⛔'
    if i['expired']:
        return '⌛'
    return '🟢' if i['online'] else '🔴'


def status_line(i):
    parts = ['🟢 آنلاین' if i['online'] else ('🔴 آفلاین' if i['hb'] else '⚪️ هنوز اجرا نشده')]
    parts.append('✅ فعال' if i['active'] else '⛔ متوقف')
    if i['expired']:
        parts.append('⌛ منقضی')
    return ' | '.join(parts)


def quota_line(i):
    if i['quota'] is None:
        return '📶 سقف فروش: <b>نامحدود</b> | فروخته‌شده: <b>%s</b> گیگ' % gbf(i['sold'])
    return '📶 سقف: <b>%s</b> | فروخته: <b>%s</b> | باقی: <b>%s</b> گیگ' % (gbf(i['quota']), gbf(i['sold']), gbf(i['left']))


# ---------------------------------------------------------------------------
# صفحه‌ها
# ---------------------------------------------------------------------------
def scr_home():
    bots = discover(True)
    infos = [i for i in POOL.map(safe_info, bots) if i] if bots else []
    lines = ['🎛 <b>مرکز کنترل بات‌ها</b>', DIVIDER]
    kb = []
    for i in infos:
        cust = ' · %s' % esc(i['meta']['customer']) if i['meta']['customer'] else ''
        left = '♾' if i['left'] is None else '%s گیگ' % gbf(i['left'])
        lines.append('%s <b>%s</b>%s\n    👥 %s | 📶 باقی: %s' % (badge(i), esc(i['meta']['label']), cust, fmt(i['st']['users']), left))
        kb.append([B('%s %s' % (badge(i), i['meta']['label']), 'b:' + pid_of(i['prefix']))])
    if not infos:
        lines.append('هنوز هیچ باتی پیدا نشد. بات‌ها بعد از اولین اجرا خودکار اینجا میان.')
    kb += [[B('📊 آمار کل', 'all'), B('💵 درآمد', 'inc')], [B('🌐 عملیات گروهی', 'bulk'), B('📜 لاگ کل', 'lg:all')],
           [B('🔄 بروزرسانی', 'h')]]
    return '\n'.join(lines), KB(*kb)


def scr_bot(prefix):
    i = info(prefix)
    pid, s, m = pid_of(prefix), i['st'], i['meta']
    lines = ['🤖 <b>%s</b> %s' % (esc(m['label']), ('(@%s)' % esc(i['username'])) if i['username'] else ''), DIVIDER]
    if m['customer']:
        lines.append('👤 مشتری: <b>%s</b>' % esc(m['customer']))
    if m['note']:
        lines.append('📝 %s' % esc(m['note']))
    lines += ['وضعیت: %s' % status_line(i), DIVIDER,
              '👥 کاربران: <b>%s</b>' % fmt(s['users']),
              '💰 مجموع کیف‌پول‌ها: <b>%s</b> ت' % fmt(s['wallets']),
              '📦 کانفیگ فعال: <b>%s</b> (%s گیگ)' % (fmt(s['configs']), gbf(s['cfg_gb'])),
              '💸 فروش کل: <b>%s</b> ت | امروز: <b>%s</b> ت' % (fmt(s['sales']), fmt(s['today'])),
              '⏳ شارژ در انتظار: <b>%s</b>' % s['pending'], DIVIDER, quota_line(i)]
    if i['exp']:
        lines.append('⏳ اعتبار تا: <b>%s</b> (%s روز)' % (i['exp'], i['days_to_exp']))
    if i['hb']:
        lines.append('🫀 آخرین ضربان: %s ثانیه پیش' % int(time.time() - i['hb']))
    rows = [[B('📶 حجم و سقف', 'q:' + pid), B('⏳ اعتبار و وضعیت', 'l:' + pid)],
            [B('⚙️ محدودیت‌ها', 'lm:' + pid), B('🧩 قابلیت‌ها', 'f:' + pid)],
            [B('📣 اعلان به ادمین بات', 'n:' + pid), B('💵 پرداخت‌ها', 'p:' + pid)],
            [B('📜 لاگ', 'lg:' + pid), B('🏷 مشخصات', 'mm:' + pid)]]
    if creds(prefix):
        rows.append([B('📨 پیام به ادمین بات', 'dm:' + pid)])
    rows.append([B('🔄 بروزرسانی', 'b:' + pid), B('⬅️ خانه', 'h')])
    return '\n'.join(lines), KB(*rows)


def scr_quota(prefix):
    i, pid = info(prefix, False), pid_of(prefix)
    text = '📶 <b>حجم و سقف فروش — %s</b>\n%s\n%s\n\n' \
           'ℹ️ سقف = کل حجمی که این بات اجازه‌ی فروش داره. فروخته‌شده با خرید/هدیه/تمدید زیاد می‌شه و ' \
           'با حذف کانفیگ کم نمی‌شه.' % (esc(i['meta']['label']), DIVIDER, quota_line(i))
    rows = [[B('➕10', 'qa:%s:10'), B('➕50', 'qa:%s:50'), B('➕100', 'qa:%s:100')],
            [B('➖10', 'qa:%s:-10'), B('➖50', 'qa:%s:-50'), B('➖100', 'qa:%s:-100')]]
    rows = [[B(b['text'], b['callback_data'] % pid) for b in r] for r in rows]
    rows += [[B('✏️ مقدار دقیق / افزودن', 'qs:' + pid), B('💰 فروش حجم (ثبت پرداخت)', 'sl:' + pid)],
             [B('♾ نامحدود', 'cy:ul:' + pid)] if i['quota'] is not None else [],
             [B('🔁 همگام با کانفیگ‌ها', 'cf:sy:' + pid), B('🧹 ریست فروخته‌شده', 'cf:rs:' + pid)],
             [B('⬅️ بات', 'b:' + pid)]]
    return text, KB(*rows)


def scr_license(prefix):
    i, pid = info(prefix, False), pid_of(prefix)
    text = ['⏳ <b>اعتبار و وضعیت — %s</b>' % esc(i['meta']['label']), DIVIDER,
            'وضعیت: %s' % ('✅ فعال' if i['active'] else '⛔ متوقف'),
            'پیام توقف: %s' % (esc(i['cfg'].get('lic_msg')) if i['cfg'].get('lic_msg') else '(پیش‌فرض)'),
            'انقضا: %s' % (('<b>%s</b> (%s روز مانده)' % (i['exp'], i['days_to_exp'])) if i['exp'] else '<b>بدون انقضا</b>')]
    rows = [[B('✅ فعال‌سازی' if not i['active'] else '⛔ توقف بات', 'sp:' + pid), B('✏️ پیام توقف', 'sm:' + pid)],
            [B('➕30 روز', 'ea:%s:30' % pid), B('➕90 روز', 'ea:%s:90' % pid), B('➕365 روز', 'ea:%s:365' % pid)],
            [B('📅 تنظیم تاریخ', 'es:' + pid), B('♾ حذف انقضا', 'ec:' + pid)],
            [B('⬅️ بات', 'b:' + pid)]]
    return '\n'.join(text), KB(*rows)


LIMITS = (('min_gb', '📉 حداقل حجم هر خرید (گیگ)', '1'), ('max_gb', '📈 حداکثر حجم هر خرید (گیگ)', '1000'),
          ('max_days', '📆 حداکثر مدت هر کانفیگ (روز)', '365'), ('max_users', '👥 سقف تعداد کاربران (۰ = نامحدود)', ''))


def scr_limits(prefix):
    i, pid = info(prefix, False), pid_of(prefix)
    lines = ['⚙️ <b>محدودیت‌ها — %s</b>' % esc(i['meta']['label']), DIVIDER]
    rows = []
    for key, title, dflt in LIMITS:
        v = (i['cfg'].get('lic_' + key) or dflt) or 'نامحدود'
        lines.append('%s: <b>%s</b>' % (title, esc(v)))
        rows.append([B('✏️ ' + title.split(' (')[0][2:], 'ls:%s:%s' % (pid, key))])
    rows.append([B('⬅️ بات', 'b:' + pid)])
    return '\n'.join(lines), KB(*rows)


FLAGS = (('buy', '🛒 خرید کانفیگ'), ('topup', '💳 شارژ کیف پول'), ('broadcast', '📢 پیام همگانی (ادمین بات)'),
         ('gift', '🎁 کانفیگ هدیه (ادمین بات)'))


def scr_features(prefix):
    i, pid = info(prefix, False), pid_of(prefix)
    lines = ['🧩 <b>قابلیت‌ها — %s</b>' % esc(i['meta']['label']), DIVIDER, 'با هر دکمه روشن/خاموش می‌شه:']
    rows = [[B('%s %s' % ('✅' if i['cfg'].get('lic_' + f, '1') != '0' else '⛔', t), 'ft:%s:%s' % (pid, f))] for f, t in FLAGS]
    rows.append([B('⬅️ بات', 'b:' + pid)])
    return '\n'.join(lines), KB(*rows)


def scr_notice(prefix):
    i, pid = info(prefix, False), pid_of(prefix)
    n = i['cfg'].get('lic_notice', '')
    text = '📣 <b>اعلان به ادمین بات — %s</b>\n%s\nاین متن بالای «پنل ادمین» اون بات نشون داده می‌شه.\n\nاعلان فعلی: %s' \
           % (esc(i['meta']['label']), DIVIDER, esc(n) if n else '—')
    return text, KB([B('✏️ تنظیم اعلان', 'ns:' + pid), B('🗑 پاک‌کردن', 'nc:' + pid)], [B('⬅️ بات', 'b:' + pid)])


def scr_payments(prefix):
    pid = pid_of(prefix)
    rows = q('SELECT ts, amount, gb, note FROM m_pay WHERE prefix=? ORDER BY id DESC LIMIT 10', (prefix,))
    tot = q('SELECT COALESCE(SUM(amount),0), COALESCE(SUM(gb),0), COUNT(*) FROM m_pay WHERE prefix=?', (prefix,))[0]
    lines = ['💵 <b>پرداخت‌ها — %s</b>' % esc(meta_of(prefix)['label']), DIVIDER,
             'جمع: <b>%s</b> ت | حجم: <b>%s</b> گیگ | %s بار' % (fmt(tot[0]), gbf(tot[1]), tot[2]), DIVIDER]
    lines += ['%s · %s ت · %s گیگ %s' % (r[0][:10], fmt(r[1]), gbf(r[2]), esc(r[3] or '')) for r in rows] or ['—']
    return '\n'.join(lines), KB([B('💰 ثبت فروش حجم', 'sl:' + pid)], [B('⬅️ بات', 'b:' + pid)])


def scr_meta(prefix):
    pid, m = pid_of(prefix), meta_of(prefix)
    text = '🏷 <b>مشخصات بات</b>\n%s\nنام: %s\nمشتری: %s\nیادداشت: %s' % (
        DIVIDER, esc(m['label']), esc(m['customer'] or '—'), esc(m['note'] or '—'))
    return text, KB([B('✏️ نام', 'mt:%s:label' % pid), B('✏️ مشتری', 'mt:%s:customer' % pid)],
                    [B('✏️ یادداشت', 'mt:%s:note' % pid)], [B('⬅️ بات', 'b:' + pid)])


def scr_logs(prefix, back):
    if prefix is None:
        rows = q('SELECT ts, prefix, action, detail FROM m_log ORDER BY id DESC LIMIT 20')
    else:
        rows = q('SELECT ts, prefix, action, detail FROM m_log WHERE prefix=? ORDER BY id DESC LIMIT 20', (prefix,))
    lines = ['📜 <b>لاگ تغییرات</b>', DIVIDER]
    lines += ['%s · %s · %s %s' % (r[0][5:16].replace('T', ' '), 'بات %s' % bot_no(r[1]), esc(r[2]), esc(r[3] or ''))
              for r in rows] or ['—']
    return '\n'.join(lines), KB([B('⬅️ برگشت', back)])


def scr_all():
    infos = [i for i in POOL.map(safe_info, discover(True)) if i]
    inc = q('SELECT COALESCE(SUM(amount),0), COALESCE(SUM(gb),0) FROM m_pay')[0]
    tot = lambda f: sum(f(i) for i in infos)
    left = [i['left'] for i in infos if i['left'] is not None]
    lines = ['📊 <b>آمار کل (%s بات)</b>' % len(infos), DIVIDER,
             '👥 کل کاربران: <b>%s</b>' % fmt(tot(lambda i: i['st']['users'])),
             '💰 کل موجودی کیف‌پول‌ها: <b>%s</b> ت' % fmt(tot(lambda i: i['st']['wallets'])),
             '📦 کل کانفیگ‌ها: <b>%s</b>' % fmt(tot(lambda i: i['st']['configs'])),
             '💸 کل فروش بات‌ها: <b>%s</b> ت' % fmt(tot(lambda i: i['st']['sales'])),
             '📶 کل حجم فروخته‌شده: <b>%s</b> گیگ' % gbf(tot(lambda i: i['sold'])),
             '📶 مجموع باقی‌مانده (باتای محدود): <b>%s</b> گیگ' % gbf(sum(left)),
             '⏳ شارژ در انتظار: <b>%s</b>' % tot(lambda i: i['st']['pending']), DIVIDER,
             '💵 درآمد ثبت‌شده‌ی شما: <b>%s</b> ت (%s گیگ)' % (fmt(inc[0]), gbf(inc[1]))]
    return '\n'.join(lines), KB([B('⬅️ خانه', 'h')])


def scr_income():
    rows = q('SELECT prefix, COALESCE(SUM(amount),0), COALESCE(SUM(gb),0), COUNT(*) FROM m_pay GROUP BY prefix')
    lines = ['💵 <b>درآمد ثبت‌شده از فروش حجم</b>', DIVIDER]
    lines += ['%s: <b>%s</b> ت · %s گیگ · %s بار' % (esc(meta_of(r[0])['label']), fmt(r[1]), gbf(r[2]), r[3]) for r in rows] or ['هنوز پرداختی ثبت نشده.']
    return '\n'.join(lines), KB([B('⬅️ خانه', 'h')])


def scr_bulk():
    return ('🌐 <b>عملیات گروهی (روی همه‌ی بات‌ها)</b>', KB(
        [B('⛔ توقف همه', 'cf:sa:all'), B('✅ فعال‌سازی همه', 'cf:ra:all')],
        [B('📣 اعلان برای همه', 'bn'), B('🗑 پاک‌کردن اعلان همه', 'bnc')],
        [B('➕ افزودن حجم به همه', 'bq')], [B('⬅️ خانه', 'h')]))


# ---------------------------------------------------------------------------
# ارتباط با ادمین هر بات (اختیاری؛ از طریق توکن خود اون بات)
# ---------------------------------------------------------------------------
def creds(prefix):
    n = bot_no(prefix)
    tok, adm = os.getenv('BOT_TOKEN%d' % n, '').strip(), re.sub(r'\D', '', os.getenv('ADMIN_ID%d' % n, '') or '')
    return (tok, int(adm)) if tok and adm else None


def dm_reseller(prefix, text):
    c = creds(prefix)
    if not c:
        return False
    try:
        r = SESSION.post('https://api.splus.ir/bot%s/sendMessage' % c[0],
                         json={'chat_id': c[1], 'text': text, 'parse_mode': 'HTML'}, timeout=15).json()
        return bool(r.get('ok'))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# عملیات
# ---------------------------------------------------------------------------
def set_quota(prefix, admin_id, value, how):
    bset(prefix, 'lic_quota_gb', gbf(value) if value is not None else '')
    audit(prefix, admin_id, 'سقف حجم', '%s → %s' % (how, gbf(value) if value is not None else 'نامحدود'))


def quota_add(prefix, admin_id, delta):
    i = info(prefix, False)
    base = i['quota'] if i['quota'] is not None else i['sold']
    set_quota(prefix, admin_id, max(0.0, base + delta), '%+g' % delta)


def do_confirmed(act, pid, admin_id):
    if act in ('sa', 'ra'):
        for p in discover():
            bset(p, 'lic_active', '0' if act == 'sa' else '1')
            audit(p, admin_id, 'توقف گروهی' if act == 'sa' else 'فعال‌سازی گروهی')
        return '✅ برای همه‌ی بات‌ها انجام شد.'
    p = prefix_of(pid)
    if p is None:
        return '⚠️ بات پیدا نشد.'
    if act == 'rs':
        bset(p, 'lic_sold_gb', '0')
        audit(p, admin_id, 'ریست فروخته‌شده')
        return '✅ شمارنده‌ی فروخته‌شده صفر شد.'
    if act == 'sy':
        real = bstats(p)['cfg_gb']
        bset(p, 'lic_sold_gb', gbf(real))
        audit(p, admin_id, 'همگام‌سازی فروخته‌شده', gbf(real))
        return '✅ فروخته‌شده = %s گیگ (مجموع کانفیگ‌های فعلی).' % gbf(real)
    if act == 'ul':
        set_quota(p, admin_id, None, 'نامحدود')
        return '✅ سقف فروش برداشته شد (نامحدود).'
    return '⚠️ نامعتبر.'


CONFIRM_TEXT = {'sa': '⛔ همه‌ی بات‌ها متوقف بشن؟', 'ra': '✅ همه‌ی بات‌ها فعال بشن؟',
                'rs': '🧹 شمارنده‌ی «فروخته‌شده» صفر بشه؟ (ظرفیت باقی‌مانده برابر سقف می‌شه)',
                'sy': '🔁 «فروخته‌شده» برابر مجموع حجم کانفیگ‌های فعلی تنظیم بشه؟'}


# ---------------------------------------------------------------------------
# مرحله‌های متنی
# ---------------------------------------------------------------------------
def ask(chat_id, step, prompt, back, **kw):
    user_steps[chat_id] = dict(step=step, **kw)
    send(chat_id, prompt, KB([B('❌ انصراف', back)]))


def parse_date(t):
    t = (t or '').strip().translate(_DIGITS).replace('/', '-')
    try:
        return datetime.strptime(t, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        return None


def handle_step(chat_id, text):
    st = user_steps.get(chat_id) or {}
    step, pid = st.get('step'), st.get('pid')
    p = prefix_of(pid) if pid else None
    text = (text or '').strip()
    if not text:
        return
    done = lambda msg, back: (user_steps.pop(chat_id, None), send(chat_id, msg, KB([B('⬅️ برگشت', back)])))
    if step in ('setq', 'sell', 'lim', 'expdate', 'suspmsg', 'notice', 'meta', 'dm') and p is None:
        user_steps.pop(chat_id, None)
        return
    if step == 'setq':
        v = num(text)
        if v is None or abs(v) > 10**7:
            send(chat_id, '⚠️ یه عدد بفرست (مثلاً <code>500</code>، <code>+50</code> یا <code>-20</code>).')
            return
        if text.startswith(('+', '-')):
            quota_add(p, chat_id, v)
        else:
            set_quota(p, chat_id, max(0.0, v), 'تنظیم دقیق')
        done('✅ انجام شد.', 'q:' + pid)
    elif step == 'sell':
        parts = text.split(None, 2)
        gb, amt = (num(parts[0]), num(parts[1])) if len(parts) >= 2 else (None, None)
        if gb is None or amt is None or gb <= 0 or amt < 0:
            send(chat_id, '⚠️ این شکلی بفرست: <code>گیگ مبلغ [توضیح]</code> (مثلاً <code>500 1500000 هفته اول</code>).')
            return
        note = parts[2][:80] if len(parts) > 2 else ''
        quota_add(p, chat_id, gb)
        x('INSERT INTO m_pay (ts, prefix, amount, gb, note) VALUES (?, ?, ?, ?, ?)',
          (datetime.now().isoformat(timespec='seconds'), p, int(amt), gb, note))
        audit(p, chat_id, 'فروش حجم', '%s گیگ / %s ت' % (gbf(gb), fmt(amt)))
        done('✅ %s گیگ به سقف اضافه و پرداخت %s تومان ثبت شد.' % (gbf(gb), fmt(amt)), 'q:' + pid)
    elif step == 'lim':
        key = st['key']
        v = num(text)
        if v is None or v < 0 or v > 10**7:
            send(chat_id, '⚠️ یه عدد معتبر بفرست.')
            return
        if key == 'max_users' and v == 0:
            val = ''
        else:
            val = str(int(v))
            if key in ('min_gb', 'max_days') and int(v) < 1 or key == 'max_gb' and int(v) < 1:
                send(chat_id, '⚠️ عدد باید حداقل ۱ باشه.')
                return
        cfg = bcfg(p)
        lo = int(val) if key == 'min_gb' else int(cfg.get('lic_min_gb') or 1)
        hi = int(val) if key == 'max_gb' else int(cfg.get('lic_max_gb') or 1000)
        if lo > hi:
            send(chat_id, '⚠️ حداقل حجم نمی‌تونه از حداکثر بیشتر باشه.')
            return
        bset(p, 'lic_' + key, val)
        audit(p, chat_id, 'محدودیت ' + key, val or 'نامحدود')
        done('✅ ذخیره شد.', 'lm:' + pid)
    elif step == 'expdate':
        d = parse_date(text)
        if not d:
            send(chat_id, '⚠️ تاریخ میلادی رو این شکلی بفرست: <code>2026-12-31</code>')
            return
        bset(p, 'lic_expires', d)
        audit(p, chat_id, 'تاریخ انقضا', d)
        done('✅ اعتبار تا %s تنظیم شد.' % d, 'l:' + pid)
    elif step == 'suspmsg':
        bset(p, 'lic_msg', '' if text == '-' else text[:300])
        audit(p, chat_id, 'پیام توقف', text[:60])
        done('✅ ذخیره شد.', 'l:' + pid)
    elif step == 'notice':
        bset(p, 'lic_notice', text[:500])
        audit(p, chat_id, 'اعلان', text[:60])
        done('✅ اعلان ثبت شد؛ تو پنل ادمین اون بات نشون داده می‌شه.', 'n:' + pid)
    elif step == 'meta':
        x('INSERT INTO m_bots (prefix, %s) VALUES (?, ?) ON CONFLICT(prefix) DO UPDATE SET %s=excluded.%s'
          % ((st['field'],) * 3), (p, '' if text == '-' else text[:80]))
        done('✅ ذخیره شد.', 'mm:' + pid)
    elif step == 'dm':
        ok = dm_reseller(p, '📨 <b>پیام از پشتیبانی</b>\n%s\n%s' % (DIVIDER, esc(text)))
        audit(p, chat_id, 'پیام به ادمین بات', text[:60])
        done('✅ ارسال شد.' if ok else '⚠️ ارسال نشد (شاید ادمین بات رو استارت نکرده).', 'b:' + pid)
    elif step == 'bulk_notice':
        for b in discover():
            bset(b, 'lic_notice', text[:500])
            audit(b, chat_id, 'اعلان گروهی', text[:60])
        done('✅ اعلان برای همه‌ی بات‌ها ثبت شد.', 'bulk')
    elif step == 'bulk_addq':
        v = num(text)
        if v is None or v == 0 or abs(v) > 10**7:
            send(chat_id, '⚠️ یه عدد بفرست (مثلاً <code>50</code> یا <code>-10</code>).')
            return
        n = 0
        for b in discover():
            if info(b, False)['quota'] is not None:      # بات‌های نامحدود دست نمی‌خورن
                quota_add(b, chat_id, v)
                n += 1
        done('✅ %+g گیگ به سقف %s بات (محدودها) اضافه شد.' % (v, n), 'bulk')


# ---------------------------------------------------------------------------
# دکمه‌ها
# ---------------------------------------------------------------------------
def on_callback(chat_id, mid, data):
    d = data.split(':')
    a = d[0]
    pid = d[1] if len(d) > 1 else None
    SCREENS = {'q': scr_quota, 'l': scr_license, 'lm': scr_limits, 'f': scr_features, 'n': scr_notice,
               'p': scr_payments, 'mm': scr_meta, 'b': scr_bot}
    if a == 'h':
        user_steps.pop(chat_id, None)
        show(chat_id, mid, *scr_home())
    elif a == 'all':
        show(chat_id, mid, *scr_all())
    elif a == 'inc':
        show(chat_id, mid, *scr_income())
    elif a == 'bulk':
        user_steps.pop(chat_id, None)
        show(chat_id, mid, *scr_bulk())
    elif a == 'bn':
        ask(chat_id, 'bulk_notice', '📣 متن اعلان برای <b>همه‌ی بات‌ها</b> رو بفرست:', 'bulk')
    elif a == 'bnc':
        for b in discover():
            bset(b, 'lic_notice', '')
            audit(b, chat_id, 'پاک‌کردن اعلان گروهی')
        show(chat_id, mid, '✅ اعلان همه‌ی بات‌ها پاک شد.', KB([B('⬅️ برگشت', 'bulk')]))
    elif a == 'bq':
        ask(chat_id, 'bulk_addq', '➕ چند گیگ به سقف <b>همه‌ی بات‌های محدود</b> اضافه بشه؟ (عدد منفی = کم‌کردن)', 'bulk')
    elif a == 'lg' and pid == 'all':
        show(chat_id, mid, *scr_logs(None, 'h'))
    elif a == 'cf' and len(d) == 3:                      # تایید قبل از کار حساس
        txt = CONFIRM_TEXT.get(d[1])
        if txt:
            back = 'bulk' if d[2] == 'all' else 'q:' + d[2]
            show(chat_id, mid, txt, KB([B('✅ بله', 'cy:%s:%s' % (d[1], d[2])), B('❌ نه', back)]))
    elif a == 'cy' and len(d) == 3:
        res = do_confirmed(d[1], d[2], chat_id)
        back = 'bulk' if d[2] == 'all' else 'q:' + d[2]
        show(chat_id, mid, res, KB([B('⬅️ برگشت', back)]))
    else:
        p = prefix_of(pid) if pid else None
        if p is None:
            show(chat_id, mid, '⚠️ بات پیدا نشد.', KB([B('⬅️ خانه', 'h')]))
            return
        if a in SCREENS:
            user_steps.pop(chat_id, None)
            show(chat_id, mid, *SCREENS[a](p))
        elif a == 'lg':
            show(chat_id, mid, *scr_logs(p, 'b:' + pid))
        elif a == 'qa' and len(d) == 3 and num(d[2]) is not None:
            quota_add(p, chat_id, num(d[2]))
            show(chat_id, mid, *scr_quota(p))
        elif a == 'qs':
            ask(chat_id, 'setq', '✏️ عدد سقف رو بفرست.\n• عدد ساده = تنظیم دقیق (مثلاً <code>500</code>)\n'
                '• با علامت = افزودن/کم‌کردن (مثلاً <code>+50</code> یا <code>-20</code>)', 'q:' + pid, pid=pid)
        elif a == 'sl':
            ask(chat_id, 'sell', '💰 فروش حجم به این نماینده:\n<code>گیگ مبلغ [توضیح]</code>\n'
                'مثلاً <code>500 1500000 هفته اول</code>\n(حجم به سقف اضافه و پرداخت ثبت می‌شه)', 'q:' + pid, pid=pid)
        elif a == 'sp':
            on = bcfg(p).get('lic_active', '1') != '0'
            bset(p, 'lic_active', '0' if on else '1')
            audit(p, chat_id, 'توقف' if on else 'فعال‌سازی')
            show(chat_id, mid, *scr_license(p))
        elif a == 'sm':
            ask(chat_id, 'suspmsg', '✏️ پیامی که موقع توقف/به کاربرا نشون داده می‌شه رو بفرست (<code>-</code> = پیش‌فرض):',
                'l:' + pid, pid=pid)
        elif a == 'ea' and len(d) == 3 and d[2].isdigit():
            i = info(p)
            base = date.today()
            if i['exp'] and i['days_to_exp'] is not None and i['days_to_exp'] > 0:
                base = datetime.strptime(i['exp'], '%Y-%m-%d').date()
            new = (base + timedelta(days=int(d[2]))).isoformat()
            bset(p, 'lic_expires', new)
            audit(p, chat_id, 'تمدید اعتبار', '+%s روز → %s' % (d[2], new))
            show(chat_id, mid, *scr_license(p))
        elif a == 'es':
            ask(chat_id, 'expdate', '📅 تاریخ پایان اعتبار (میلادی) رو بفرست: <code>2026-12-31</code>', 'l:' + pid, pid=pid)
        elif a == 'ec':
            bset(p, 'lic_expires', '')
            audit(p, chat_id, 'حذف انقضا')
            show(chat_id, mid, *scr_license(p))
        elif a == 'ls' and len(d) == 3 and d[2] in {k for k, _, _ in LIMITS}:
            title = {k: t for k, t, _ in LIMITS}[d[2]]
            ask(chat_id, 'lim', '✏️ مقدار جدید برای «%s» رو بفرست:' % title, 'lm:' + pid, pid=pid, key=d[2])
        elif a == 'ft' and len(d) == 3 and d[2] in {k for k, _ in FLAGS}:
            on = bcfg(p).get('lic_' + d[2], '1') != '0'
            bset(p, 'lic_' + d[2], '0' if on else '1')
            audit(p, chat_id, 'قابلیت ' + d[2], 'خاموش' if on else 'روشن')
            show(chat_id, mid, *scr_features(p))
        elif a == 'ns':
            ask(chat_id, 'notice', '✏️ متن اعلان برای ادمین اون بات رو بفرست:', 'n:' + pid, pid=pid)
        elif a == 'nc':
            bset(p, 'lic_notice', '')
            audit(p, chat_id, 'پاک‌کردن اعلان')
            show(chat_id, mid, *scr_notice(p))
        elif a == 'mt' and len(d) == 3 and d[2] in ('label', 'customer', 'note'):
            ask(chat_id, 'meta', '✏️ مقدار جدید رو بفرست (<code>-</code> = پاک‌کردن):', 'mm:' + pid, pid=pid, field=d[2])
        elif a == 'dm' and creds(p):
            ask(chat_id, 'dm', '📨 پیامت رو بفرست؛ با خود اون بات برای ادمینش ارسال می‌شه:', 'b:' + pid, pid=pid)


def handle_update(update):
    cb, msg = update.get('callback_query'), update.get('message')
    if cb:
        api('answerCallbackQuery', {'callback_query_id': cb.get('id')}, 5)
        m = cb.get('message') or {}
        chat_id = (m.get('chat') or {}).get('id')
        if chat_id not in ADMINS:
            return
        on_callback(chat_id, m.get('message_id'), cb.get('data') or '')
    elif msg:
        chat_id = (msg.get('chat') or {}).get('id')
        if chat_id not in ADMINS:
            if chat_id is not None:
                send(chat_id, '⛔ دسترسی ندارید.')
            return
        text = (msg.get('text') or '').strip()
        if text.startswith('/'):
            user_steps.pop(chat_id, None)
            show(chat_id, None, *scr_home())
        elif user_steps.get(chat_id):
            handle_step(chat_id, text)
        else:
            show(chat_id, None, *scr_home())


def safe_update(update):
    try:
        handle_update(update)
    except Exception as e:
        print('⚠️ خطا در پردازش آپدیت:', repr(e))
        _local.conn = None


# ---------------------------------------------------------------------------
# هشدارهای خودکار
# ---------------------------------------------------------------------------
def conditions(i):
    """شرایط هشدار فعلیِ یه بات: {نوع: متن}."""
    out = {}
    if i['hb'] and time.time() - i['hb'] > 300:
        out['offline'] = '🔴 بات آفلاین شده (آخرین ضربان %s دقیقه پیش).' % int((time.time() - i['hb']) / 60)
    if i['quota'] is not None:
        if i['left'] <= 0:
            out['empty'] = '📶 ظرفیت فروش تموم شد.'
        elif i['left'] <= LOW_QUOTA_GB:
            out['low'] = '📶 ظرفیت فروش کمه: %s گیگ باقی مونده.' % gbf(i['left'])
    if i['days_to_exp'] is not None:
        if i['days_to_exp'] < 0:
            out['expired'] = '⌛ اعتبار بات تموم شده.'
        elif i['days_to_exp'] <= 3:
            out['expiring'] = '⏳ اعتبار بات %s روز دیگه تموم می‌شه.' % i['days_to_exp']
    return out


def monitor_loop():
    time.sleep(20)
    fired = {}
    while True:
        try:
            for p in discover(True):
                i = info(p)
                cond = conditions(i)
                name = i['meta']['label']
                for kind, txt in cond.items():
                    k = (p, kind)
                    if k not in fired or (kind != 'offline' and time.time() - fired[k] > 6 * 3600):
                        fired[k] = time.time()
                        for adm in ADMINS:
                            send(adm, '🚨 <b>%s</b>\n%s' % (esc(name), txt), KB([B('🔧 باز کردن', 'b:' + pid_of(p))]))
                        if NOTIFY_RESELLERS and kind in ('low', 'empty', 'expiring', 'expired'):
                            dm_reseller(p, '📣 %s\nبرای تمدید/افزایش حجم با پشتیبانی تماس بگیر.' % txt)
                for k in [k for k in fired if k[0] == p and k[1] not in cond]:
                    if k[1] == 'offline':
                        for adm in ADMINS:
                            send(adm, '🟢 <b>%s</b> دوباره آنلاین شد.' % esc(name))
                    fired.pop(k, None)
        except Exception as e:
            print('⚠️ خطای مانیتور:', repr(e))
            _local.conn = None
        time.sleep(300)


def start_health_server(port):
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
    if os.getenv("DISABLE_HEALTH_SERVER") != "1":
        start_health_server(int(os.getenv("PORT", "8080")))
    threading.Thread(target=monitor_loop, daemon=True).start()
    print('✅ ربات مدیریت اجرا شد. بات‌های پیداشده:', [pid_of(p) for p in discover(True)])
    offset = 0
    while True:
        try:
            started = time.time()
            params = {'timeout': 25}
            if offset:
                params['offset'] = offset + 1
            data = SESSION.get(BASE_URL + '/getUpdates', params=params, timeout=35).json()
            if data.get('ok') and data.get('result'):
                for u in data['result']:
                    offset = u['update_id']
                    UPDATE_EXECUTOR.submit(safe_update, u)
            elif time.time() - started < 3:
                time.sleep(1 if not data.get('ok') else 0.2)
        except Exception as e:
            print('⚠️ خطای حلقه اصلی:', e)
            time.sleep(2)


if __name__ == '__main__':
    main()
