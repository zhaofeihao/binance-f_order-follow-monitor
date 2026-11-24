#!/usr/bin/env python3
"""只读币安公开操作记录监控。Python 3.9+，仅使用标准库。"""
import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

PORTFOLIO = '5075281354358777856'
BASE = 'https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/'
PAGE_URL = 'https://www.binance.com/zh-CN/copy-trading/lead-details/' + PORTFOLIO
STOP = threading.Event()
LOG = logging.getLogger('monitor')
APP_DIR = Path(__file__).resolve().parent


class MonitorError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def load_env_file(path, required=False, environ=None):
    """读取简单单行 .env；不执行命令、不展开变量，已有环境变量优先。"""
    environ = os.environ if environ is None else environ
    try:
        content = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        if required:
            raise MonitorError('指定的环境配置文件不存在') from None
        return
    except (OSError, UnicodeError):
        raise MonitorError('无法读取环境配置文件，请检查权限和 UTF-8 编码') from None
    values = {}
    for lineno, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise MonitorError(f'环境配置第 {lineno} 行格式错误，需使用 KEY=VALUE')
        if value.startswith(('"', "'")):
            end = value.find(value[0], 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith('#')):
                raise MonitorError(f'环境配置第 {lineno} 行引号格式错误')
            value = value[1:end]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        values[key] = value
    # 全文件验证通过才应用；错误不包含原行，防止泄露密钥。
    for key, value in values.items():
        environ.setdefault(key, value)


def request_json(url, payload=None):
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0',
                 'clienttype': 'web', 'Referer': PAGE_URL})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise MonitorError('响应过大，未更新状态')
        return json.loads(raw, parse_float=str)
    except urllib.error.HTTPError as exc:
        try:
            retry = float(exc.headers.get('Retry-After', 0))
        except ValueError:
            retry = 0
        if exc.code in (403, 418, 429, 451):
            retry = max(retry, 300)
        raise MonitorError(f'HTTP {exc.code}；请检查网络、区域限制或访问频率', retry) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        # 不输出 URL，避免 Telegram Token 或飞书 Webhook 出现在日志中。
        raise MonitorError('网络连接失败或超时') from None
    except (ValueError, UnicodeError):
        raise MonitorError('响应不是有效 JSON，可能遇到访问验证或接口变更') from None


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return format(result.normalize(), 'f')
    except (InvalidOperation, ValueError):
        raise MonitorError('操作记录包含无效数值') from None


def timestamp(row):
    for key in ('orderUpdateTime', 'orderTime'):
        try:
            value = int(row.get(key, 0))
            if 0 < value < 253402300799000:
                return value
        except (TypeError, ValueError):
            pass
    raise MonitorError('操作记录缺少有效时间')


def fingerprint(row):
    if not isinstance(row, dict) or not row.get('symbol'):
        raise MonitorError('操作记录缺少交易对')
    if row.get('side') not in ('BUY', 'SELL') or row.get('positionSide') not in ('LONG', 'SHORT', 'BOTH'):
        raise MonitorError('未知买卖/持仓方向，请检查接口变化')
    # 无公开订单 ID：按稳定业务字段去重，不依赖行序和 JSON 浮点表示。
    data = {k: row.get(k) for k in ('symbol', 'side', 'positionSide', 'type')}
    data['time'] = timestamp(row)
    data['orderTime'] = str(row.get('orderTime') or '')
    for key in ('executedQty', 'avgPrice', 'totalPnl'):
        data[key] = number(row[key]) if key in row else None
    if data['executedQty'] is None or data['avgPrice'] is None:
        raise MonitorError('操作记录缺少成交数量/价格')
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def action(row):
    return {('BUY', 'LONG'): '开多 / 加多', ('SELL', 'LONG'): '减多 / 平多',
            ('SELL', 'SHORT'): '开空 / 加空', ('BUY', 'SHORT'): '减空 / 平空'}.get(
                (row['side'], row['positionSide']),
                ('买入' if row['side'] == 'BUY' else '卖出') + '（单向持仓，开平未知）')


def describe(row):
    when = datetime.fromtimestamp(timestamp(row) / 1000, ZoneInfo('Asia/Shanghai'))
    return (f"熬鹰资本 · {row['symbol']} · {action(row)}\n"
            f"操作时间：{when:%Y-%m-%d %H:%M:%S} 北京时间\n"
            f"成交均价：{number(row['avgPrice'])}；数量：{number(row['executedQty'])}\n"
            f"本笔已实现盈亏：{number(row['totalPnl']) if row.get('totalPnl') is not None else '未公开'} "
            f"{row.get('quoteAsset', '')}\n记录：{fingerprint(row)[:12]}")


class Binance:
    def __init__(self, page_size=50, max_pages=100, spacing=5.0):
        self.page_size, self.max_pages, self.spacing = page_size, max_pages, spacing
        self.last_request = 0.0

    def api(self, endpoint, payload=None):
        delay = self.spacing - (time.monotonic() - self.last_request)
        if delay > 0 and STOP.wait(delay):
            raise KeyboardInterrupt
        self.last_request = time.monotonic()
        url = BASE + endpoint
        if payload is None:
            url += '?portfolioId=' + PORTFOLIO
        result = request_json(url, payload)
        if not isinstance(result, dict) or result.get('code') != '000000' or result.get('success') is False:
            code = result.get('code') if isinstance(result, dict) else 'unknown'
            raise MonitorError(f'币安业务错误 {str(code)[:40]}；未更新状态', 60)
        if not isinstance(result.get('data'), dict):
            raise MonitorError('币安返回空数据或隐藏记录；未更新状态', 60)
        return result['data']

    def verify(self):
        data = self.api('detail')
        if str(data.get('leadPortfolioId')) != PORTFOLIO or data.get('nickname') != '熬鹰资本':
            raise MonitorError('带单项目身份发生变化，请人工核对项目链接', 300)
        return data

    def page(self, page):
        data = self.api('order-history', {'portfolioId': PORTFOLIO,
                        'pageNumber': page, 'pageSize': self.page_size})
        rows = data.get('list')
        if not isinstance(rows, list):
            raise MonitorError('接口缺少 data.list；未更新状态')
        for row in rows:
            fingerprint(row)
        try:
            total = int(data['total'])
            if total < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MonitorError('接口缺少有效 total') from None
        if not rows and total > 0:
            raise MonitorError('记录意外为空；保留上次状态')
        return rows, total

    def fetch(self, cutoff=None):
        found, page_hashes, first_ids = {}, set(), None
        count = 0
        for page in range(1, self.max_pages + 1):
            rows, total = self.page(page)
            ids = tuple(fingerprint(row) for row in rows)
            if first_ids is None:
                first_ids = ids
            if ids and ids in page_hashes:
                raise MonitorError('分页重复，不能确认完整性；未推进状态')
            page_hashes.add(ids)
            found.update(zip(ids, rows))
            count += len(rows)
            if count >= total or not rows or (cutoff is not None and max(map(timestamp, rows)) < cutoff):
                break
        else:
            raise MonitorError('已达到分页上限；未推进状态，请增加 --max-pages')
        if page > 1:
            head, _ = self.page(1)
            if tuple(map(fingerprint, head)) != first_ids:
                raise MonitorError('分页期间记录变化，将重新读取以免漏单')
        return sorted(found.values(), key=timestamp)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, ts INTEGER, raw TEXT, detected REAL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT, channel TEXT, delivered INTEGER DEFAULT 0,
                PRIMARY KEY(id, channel));
        ''')

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def save(self, rows, now, full, channels):
        initial = self.get('initialized') is None
        added = 0
        with self.db:
            for row in rows:
                ident = fingerprint(row)
                inserted = self.db.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',
                    (ident, timestamp(row), json.dumps(row, ensure_ascii=False), now)).rowcount
                if inserted and not initial:
                    added += 1
                    self.db.executemany('INSERT INTO outbox(id,channel) VALUES (?,?)',
                                        [(ident, channel) for channel in channels])
            values = [('initialized', '1'), ('last_success', str(now))]
            if full:
                values.append(('last_full', str(now)))
            self.db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', values)
        return initial, added

    def deliver(self, senders):
        failed = set()
        pending = self.db.execute('''SELECT o.id,o.channel,r.raw FROM outbox o
            JOIN records r ON r.id=o.id WHERE o.delivered=0 ORDER BY r.ts,o.channel''').fetchall()
        for ident, channel, raw in pending:
            if channel in failed or channel not in senders:
                continue
            try:
                senders[channel](describe(json.loads(raw)))
            except Exception:
                # 独立通道重试；不回滚已抓取事件，不泄露 Token。
                LOG.error('%s 通知失败，已保留待发送队列', channel)
                failed.add(channel)
                continue
            with self.db:
                self.db.execute('UPDATE outbox SET delivered=1 WHERE id=? AND channel=?', (ident, channel))
        return len(pending), failed


def desktop(message):
    script = ('on run argv\n display notification (item 1 of argv) '
              'with title "熬鹰资本新操作"\nend run')
    subprocess.run(['osascript', '-e', script, message], check=True, capture_output=True, timeout=15)


def telegram_sender(token, chat):
    def send(message):
        result = request_json(f'https://api.telegram.org/bot{token}/sendMessage',
                              {'chat_id': chat, 'text': message + '\n' + PAGE_URL})
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise MonitorError('Telegram 通知失败')
        STOP.wait(1.1)
    return send


def feishu_sign(secret, ts):
    # 飞书协议：timestamp + 换行 + secret 作为 key，对空消息计算 HMAC。
    key = f'{ts}\n{secret}'.encode('utf-8')
    return base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode('ascii')


def feishu_sender(webhook, secret):
    try:
        parsed = urllib.parse.urlsplit(webhook)
        valid = (parsed.scheme == 'https' and parsed.netloc == 'open.feishu.cn'
                 and parsed.path.startswith('/open-apis/bot/v2/hook/')
                 and bool(parsed.path.removeprefix('/open-apis/bot/v2/hook/'))
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise MonitorError('FEISHU_WEBHOOK_URL 必须是飞书 HTTPS 自定义机器人 Webhook 地址')
    if not secret:
        raise MonitorError('请设置 FEISHU_SECRET，并在机器人安全设置中启用签名校验')

    def send(message):
        # 每次重试重新签名，避免队列停留导致签名过期。
        ts = str(int(time.time()))
        result = request_json(webhook, {
            'timestamp': ts, 'sign': feishu_sign(secret, ts), 'msg_type': 'text',
            'content': {'text': message + '\n' + PAGE_URL},
        })
        # 仅明确业务成功才确认投递；HTTP 200 也可能是签名/关键词错误。
        code = result.get('code', result.get('StatusCode')) if isinstance(result, dict) else None
        if type(code) is not int or code != 0:
            hint = {19021: '签名不匹配或系统时间偏差', 19024: '未满足机器人关键词规则',
                    19022: '未满足机器人 IP 白名单'}.get(code) if type(code) is int else None
            raise MonitorError('飞书通知失败：' + (hint or '接口未返回成功状态'))
        STOP.wait(1.1)
    return send


def configured_feishu():
    webhook = os.environ.get('FEISHU_WEBHOOK_URL', '').strip()
    secret = os.environ.get('FEISHU_SECRET', '').strip()
    if not webhook and not secret:
        return None
    if not webhook or not secret:
        raise MonitorError('请同时设置 FEISHU_WEBHOOK_URL 和 FEISHU_SECRET')
    return feishu_sender(webhook, secret)


@contextlib.contextmanager
def single_instance(path):
    # macOS / Linux：进程退出时自动释放，不依赖 PID 文件清理。
    import fcntl
    with open(path, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MonitorError('相同数据目录已有监控程序在运行') from None
        yield


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='配置文件路径，默认读取脚本同目录 .env')
    parser.add_argument('--interval', type=float, help='检查间隔秒数；覆盖 MONITOR_INTERVAL，默认 30')
    parser.add_argument('--data-dir', type=Path, help='数据目录；覆盖 MONITOR_DATA_DIR')
    parser.add_argument('--max-pages', type=int, help='分页上限；覆盖 MONITOR_MAX_PAGES，默认 100')
    parser.add_argument('--desktop', action='store_true', help='开启 macOS 桌面提醒')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--once', action='store_true', help='执行一次，保存状态后退出')
    modes.add_argument('--check', action='store_true', help='只检查接口和显示最近 3 笔，不保存状态或发送通知')
    modes.add_argument('--test-feishu', action='store_true', help='向配置的飞书群发送一条测试消息后退出')
    args = parser.parse_args(argv)
    load_env_file(args.env_file or APP_DIR / '.env', required=args.env_file is not None)
    try:
        if args.interval is None:
            args.interval = float(os.environ.get('MONITOR_INTERVAL', '30'))
        if args.max_pages is None:
            args.max_pages = int(os.environ.get('MONITOR_MAX_PAGES', '100'))
    except ValueError:
        raise MonitorError('MONITOR_INTERVAL 必须是数字，MONITOR_MAX_PAGES 必须是整数') from None
    if args.data_dir is None:
        args.data_dir = Path(os.environ.get('MONITOR_DATA_DIR') or 'data').expanduser()
        if not args.data_dir.is_absolute():
            args.data_dir = APP_DIR / args.data_dir
    if not 10 <= args.interval <= 86400 or args.max_pages < 1:
        parser.error('interval 必须在 10～86400 秒，max-pages 必须为正整数')
    if args.desktop and sys.platform != 'darwin':
        parser.error('--desktop 仅支持 macOS')
    return args


def main():
    args = parse_options()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    client = Binance(max_pages=args.max_pages)
    if args.check:
        info = client.verify()
        rows, total = client.page(1)
        print(f"接口正常：{info['nickname']} / {PORTFOLIO}；可见记录 {total} 条")
        for row in rows[:3]:
            print(describe(row) + '\n')
        return 0
    feishu = configured_feishu()
    if args.test_feishu:
        if feishu is None:
            raise MonitorError('请先设置 FEISHU_WEBHOOK_URL 和 FEISHU_SECRET')
        feishu('熬鹰资本监控 · 飞书通知测试\n签名通知通道测试消息，不是真实交易。')
        print('飞书接口已确认接收测试消息，请查看机器人所在群。')
        return 0
    args.data_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(args.data_dir / 'monitor.log',
                maxBytes=2_000_000, backupCount=3, encoding='utf-8')]
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    senders = {'console': lambda message: LOG.info('新公开操作 / 记录更新\n%s', message)}
    if feishu:
        senders['feishu'] = feishu
    if args.desktop:
        senders['desktop'] = desktop
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if bool(token) != bool(chat):
        raise MonitorError('请同时设置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID')
    if token:
        senders['telegram'] = telegram_sender(token, chat)
    with single_instance(args.data_dir / 'monitor.lock'):
        store = Store(args.data_dir / 'records.sqlite3')
        failures, verified_at = 0, 0
        LOG.info('开始监控熬鹰资本；间隔 %s 秒；通知通道 %s', args.interval, ','.join(senders))
        try:
            while not STOP.is_set():
                now, wait = time.time(), args.interval
                status = {'checked_at': now, 'ok': False}
                # 即使币安不可访问，也继续投递此前已持久化的事件。
                store.deliver(senders)
                try:
                    if now - verified_at >= 3600:
                        client.verify()
                        verified_at = now
                    full = now - float(store.get('last_full', '0')) >= 3600
                    cutoff = None if full else (float(store.get('last_success', '0')) - 86400) * 1000
                    rows = client.fetch(cutoff)
                    initial, added = store.save(rows, now, full, senders)
                    failures = 0
                    if initial:
                        LOG.info('已建立基准：%s 条历史记录；后续新记录才触发提醒', len(rows))
                    else:
                        LOG.info('检查成功：扫描 %s 条，新增 %s 条%s', len(rows), added, '（完整回查）' if full else '')
                    _, failed = store.deliver(senders)
                    status.update(ok=True, records_scanned=len(rows), new_records=added,
                                  notification_failures=sorted(failed))
                except MonitorError as exc:
                    failures += 1
                    wait = max(min(900, args.interval * 2 ** min(failures, 8)), exc.retry_after)
                    LOG.error('%s；%.0f 秒后重试（连续失败 %s 次）', exc, wait, failures)
                    status['error'] = str(exc)
                status['last_success'] = store.get('last_success')
                status['pending_notifications'] = store.db.execute('SELECT COUNT(*) FROM outbox WHERE delivered=0').fetchone()[0]
                temp = args.data_dir / 'health.tmp'
                temp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(args.data_dir / 'health.json')
                if args.once:
                    return 0 if status['ok'] and not status['pending_notifications'] else 1
                STOP.wait(wait + random.uniform(0, min(3, wait * .1)))
        finally:
            store.db.close()
            LOG.info('监控已停止，状态已保留')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except MonitorError as exc:
        print(f'错误：{exc}', file=sys.stderr)
        sys.exit(1)
