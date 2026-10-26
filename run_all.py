#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اجرای هم‌زمان «بات‌های فروشی» (bot.py) و «ربات مدیریت» (master_bot.py) تو یه سرویس.

- bot.py خودش از روی BOT_TOKEN1..N چند بات بالا میاره (یا یه بات از BOT_TOKEN).
- master_bot.py فقط وقتی اجرا می‌شه که MASTER_BOT_TOKEN ست باشه.
- اگه هر کدوم کرش کنه، جدا از اون یکی دوباره بالا میاد (با فاصله‌ی افزایشی تا کرش‌لوپ نشه).
- پورت سلامت‌سنجی (PORT) رو همین فایل باز می‌کنه.
Start Command / Procfile:  python -u run_all.py
"""
import os
import re
import sys
import time
import signal
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
STOP = threading.Event()


def has_sales_bots():
    return any(re.fullmatch(r'BOT_TOKEN\d*', k) and v.strip() for k, v in os.environ.items())


def has_master():
    return bool(os.getenv('MASTER_BOT_TOKEN', '').strip())


SERVICES = [
    # (اسم، فایل، شرط اجرا، دلیل نرفتن)
    ('bots', 'bot.py', has_sales_bots, 'هیچ BOT_TOKEN / BOT_TOKEN1.. ست نشده'),
    ('master', 'master_bot.py', has_master, 'MASTER_BOT_TOKEN ست نشده'),
]


def start_health_server(port):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok')

        do_HEAD = do_GET

        def log_message(self, *a):
            pass
    try:
        srv = ThreadingHTTPServer(('0.0.0.0', port), _H)
    except OSError as e:
        print('⚠️ health server بالا نیومد:', e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def pump(name, proc):
    """خروجی پردازش رو با برچسب اسمش تو لاگ چاپ می‌کنه."""
    for line in proc.stdout:
        print('[%s] %s' % (name, line.rstrip()), flush=True)


def spawn(name, script):
    env = dict(os.environ)
    env['DISABLE_HEALTH_SERVER'] = '1'       # سلامت‌سنجی رو همین فایل انجام می‌ده
    env['PYTHONUNBUFFERED'] = '1'
    proc = subprocess.Popen([sys.executable, '-u', os.path.join(BASE, script)], cwd=BASE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                            errors='replace', bufsize=1, start_new_session=True)
    threading.Thread(target=pump, args=(name, proc), daemon=True).start()
    print('🚀 %s اجرا شد (pid %d)' % (name, proc.pid), flush=True)
    return proc


def kill_group(proc, sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)     # خود پردازش + بات‌هایی که ازش بالا اومدن
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def shutdown(procs):
    for p in procs.values():
        if p['proc'] and p['proc'].poll() is None:
            kill_group(p['proc'], signal.SIGTERM)
    deadline = time.time() + 8
    for p in procs.values():
        while p['proc'] and p['proc'].poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if p['proc'] and p['proc'].poll() is None:
            kill_group(p['proc'], signal.SIGKILL)


def main():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    start_health_server(int(os.getenv('PORT', '8080')))
    procs = {}
    for name, script, cond, why in SERVICES:
        if not os.path.exists(os.path.join(BASE, script)):
            print('⚠️ %s پیدا نشد؛ %s اجرا نمی‌شه.' % (script, name), flush=True)
        elif not cond():
            print('ℹ️ %s اجرا نمی‌شه: %s.' % (name, why), flush=True)
        else:
            procs[name] = {'script': script, 'proc': None, 'started': 0.0, 'fails': 0, 'next': 0.0}
    if not procs:
        print('❌ هیچ سرویسی برای اجرا نیست؛ متغیرهای محیطی رو چک کن.', flush=True)
        sys.exit(1)
    try:
        while not STOP.is_set():
            now = time.time()
            for name, p in procs.items():
                if p['proc'] is None:
                    if now >= p['next']:
                        p['proc'], p['started'] = spawn(name, p['script']), now
                elif p['proc'].poll() is not None:
                    ran = now - p['started']
                    p['fails'] = p['fails'] + 1 if ran < 30 else 0     # کرش سریع = کرش‌لوپ
                    delay = min(60, 5 * 2 ** min(p['fails'], 4))
                    print('⚠️ %s خارج شد (کد %s)؛ %ds بعد دوباره بالا میاد.' % (name, p['proc'].returncode, delay), flush=True)
                    p['proc'], p['next'] = None, now + delay
            STOP.wait(2)
    finally:
        print('🛑 در حال خاموش‌کردن...', flush=True)
        shutdown(procs)


if __name__ == '__main__':
    main()
