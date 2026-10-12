"""Optional Discord delivery; never log webhook URLs or response bodies."""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from .chain import NoRedirect

WEBHOOK = re.compile(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+')


def read_webhook(path='', url=''):
    if path and url:
        raise ValueError('Set DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_FILE, not both')
    if not path and not url:
        return None
    value = Path(path).read_text().strip() if path else url.strip()
    if not WEBHOOK.fullmatch(value):
        raise ValueError('Invalid Discord webhook (expected an HTTPS Discord webhook URL)')
    return value


def deliver(url, content):
    request = urllib.request.Request(url+'?wait=true', data=json.dumps({
        'content':content[:1900], 'allowed_mentions':{'parse':[]}}).encode(),
        headers={'Content-Type':'application/json','User-Agent':'ValidatorWatch/1'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, 0
    except urllib.error.HTTPError as exc:
        delay=0
        if exc.code==429:
            try: delay=float(json.loads(exc.read(4096)).get('retry_after',60))
            except (ValueError,TypeError):delay=60
        return exc.code,delay


def drain(store, url, send=deliver, now=None):
    if not url:
        return
    now=time.time() if now is None else now
    # One request per iteration. At-least-once: Discord provides no idempotency key;
    # a crash after delivery but before the local commit can produce a duplicate.
    row=store.db.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1",(now,)).fetchone()
    if not row:return
    try: status,retry=send(url,row['content'])
    except Exception:status,retry=0,0
    attempts=row['attempts']+1
    if 200<=status<300:state='sent'
    elif attempts>=10 or (400<=status<500 and status not in (408,429)):state='failed'
    else:state='pending'
    delay=max(min(max(0,retry),86400),min(3600,30*2**min(attempts,7)))
    with store.db:
        store.db.execute('UPDATE outbox SET state=?,attempts=?,next_try=?,last_status=? WHERE id=?',
                         (state,attempts,now+delay,str(status),row['id']))
