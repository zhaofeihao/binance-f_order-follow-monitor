#!/usr/bin/env python3
"""独立服务健康检查；不导入 monitor.py，主程序损坏时也能告警。"""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UNIT = 'binance-monitor.service'


class HealthError(Exception):
    pass


def config(path):
    """仅解析健康检查的键，不受主程序其它配置错误影响。"""
    keys = {'FEISHU_WEBHOOK_URL', 'FEISHU_SECRET', 'HEALTH_STALE_SECONDS',
            'HEALTH_STARTUP_GRACE', 'HEALTH_BACKLOG_SECONDS', 'HEALTH_REPEAT_SECONDS',
            'HEALTH_REPORT_SECONDS', 'HEALTH_HEARTBEAT_URL'}
    values = {}
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        raise HealthError('无法读取健康告警配置文件') from None
    for index, line in enumerate(lines, 1):
        line = line.strip()
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key not in keys:
            continue
        if not sep:
            raise HealthError(f'健康告警配置第 {index} 行格式错误')
        if value.startswith(('"', "'")):
            end = value.find(value[0], 1)
            if end < 0 or (value[end + 1:].strip() and not value[end + 1:].lstrip().startswith('#')):
                raise HealthError(f'健康告警配置第 {index} 行引号错误')
            value = value[1:end]
        else:
            import re
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        values[key] = value
    for key in keys:
        if key in os.environ:
            values[key] = os.environ[key]
    webhook, secret = values.get('FEISHU_WEBHOOK_URL', ''), values.get('FEISHU_SECRET', '')
    try:
        url = urllib.parse.urlsplit(webhook)
        valid = (url.scheme == 'https' and url.netloc == 'open.feishu.cn'
                 and url.path.startswith('/open-apis/bot/v2/hook/')
                 and bool(url.path.removeprefix('/open-apis/bot/v2/hook/'))
                 and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid or not secret:
        raise HealthError('健康告警必须配置有效的飞书 Webhook 和 secret')
    defaults = {'HEALTH_STALE_SECONDS': 300, 'HEALTH_STARTUP_GRACE': 180,
                'HEALTH_BACKLOG_SECONDS': 300, 'HEALTH_REPEAT_SECONDS': 1800,
                'HEALTH_REPORT_SECONDS': 86400}
    for key, default in defaults.items():
        try:
            values[key] = int(values.get(key, default))
            if values[key] < (0 if key == 'HEALTH_REPORT_SECONDS' else 30):
                raise ValueError
        except (ValueError, TypeError):
            raise HealthError(f'{key} 数值无效；日报可设 0，其他值至少 30 秒') from None
    heartbeat = values.get('HEALTH_HEARTBEAT_URL', '')
    try:
        url = urllib.parse.urlsplit(heartbeat)
        valid = not heartbeat or (url.scheme == 'https' and bool(url.hostname) and not url.username and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise HealthError('外部心跳地址必须为 HTTPS URL')
    return values


def http(url, payload=None):
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read(100_001)
            if not 200 <= response.status < 300 or len(raw) > 100_000:
                raise ValueError
        return json.loads(raw) if payload is not None else None
    except Exception:
        # urllib 异常可能带完整凭据 URL，绝不原样写日志。
        raise HealthError('通知或心跳请求失败，请检查网络与配置') from None


def send_feishu(cfg, message):
    ts = str(int(time.time()))
    sign = base64.b64encode(hmac.new(f"{ts}\n{cfg['FEISHU_SECRET']}".encode(), b'', hashlib.sha256).digest()).decode()
    result = http(cfg['FEISHU_WEBHOOK_URL'], {'timestamp': ts, 'sign': sign,
                  'msg_type': 'text', 'content': {'text': message}})
    code = result.get('code', result.get('StatusCode')) if isinstance(result, dict) else None
    if type(code) is not int or code != 0:
        raise HealthError('飞书未确认健康告警，请检查签名、系统时间、关键词及 IP 白名单')


def service_status():
    properties = ['LoadState', 'ActiveState', 'SubState', 'Result', 'MainPID',
                  'ActiveEnterTimestampMonotonic', 'NRestarts']
    try:
        result = subprocess.run(['systemctl', 'show', UNIT, '--no-pager',
                                 '--property=' + ','.join(properties)],
                                capture_output=True, text=True, timeout=10, check=True)
        data = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if not data.get('ActiveState') or not data.get('LoadState'):
            raise ValueError
        return data
    except Exception:
        raise HealthError('无法查询 systemd 服务状态') from None


def read_json(path, missing=None):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except FileNotFoundError:
        return missing
    except (OSError, ValueError, UnicodeError):
        raise HealthError('无法读取健康状态 JSON') from None


def numeric(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError
    return value


def inspect_health(status, health, cfg, now, monotonic):
    """返回稳定的故障代码，避免时间每秒变化造成重复告警。"""
    issues = {}
    active = status.get('ActiveState')
    if status.get('LoadState') != 'loaded':
        return {'service_missing': 'systemd 服务未加载'}, False
    if active != 'active' or status.get('SubState') != 'running':
        return {'service_down': '服务未运行，可能启动失败、正在重启、重试耗尽或被停止'}, False
    try:
        started = numeric(status.get('ActiveEnterTimestampMonotonic', 0)) / 1_000_000
        grace = started > 0 and 0 <= monotonic - started < cfg['HEALTH_STARTUP_GRACE']
        if health is None:
            return ({}, True) if grace else ({'health_missing': '主程序尚未写入健康状态'}, False)
        if health.get('pid') != int(status.get('MainPID', '0')):
            return ({}, True) if grace else ({'health_old_process': '健康状态不属于当前进程，启动可能卡住'}, False)
        last_success = numeric(health.get('last_success') or 0)
        updated = numeric(health['updated_at'])
        threshold = max(cfg['HEALTH_STALE_SECONDS'], numeric(health['poll_interval']) * 3 + 60)
        if updated > now + 60 or last_success > now + 60:
            issues['clock_skew'] = '健康时间戳异常，请检查服务器时间'
        if now - updated > threshold:
            issues['heartbeat_stale'] = '主程序长时间未更新健康状态，可能卡住'
        if now - last_success > threshold and not grace:
            issues['fetch_stale'] = '长时间未成功读取币安操作记录'
        backlog = health.get('notification_backlog', {})
        if not isinstance(backlog, dict):
            raise ValueError
        for channel, item in backlog.items():
            if numeric(item['count']) > 0 and now - numeric(item['oldest_at']) > cfg['HEALTH_BACKLOG_SECONDS']:
                issues['notification_backlog'] = '启用的通知通道存在长时间未投递的记录'
        waiting = grace and (not last_success or last_success < numeric(health.get('started_at', 0)))
        return issues, waiting and not issues
    except (ValueError, TypeError, KeyError, OverflowError):
        return {'health_invalid': '健康状态字段缺失或无效，请更新主程序或检查文件'}, False


def message(title, details, now):
    stamp = datetime.fromtimestamp(now, timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    return (f'熬鹰资本监控 · {title}\n主机：{socket.gethostname()}\n时间：{stamp}\n'
            f'{details}\n检查：sudo journalctl -u binance-monitor -n 50 --no-pager')


def advance(state, issues, waiting, cfg, now):
    # 不能因不断重启而无限刷新启动宽限，跨检查记住持续未就绪时间。
    if waiting:
        since = state.setdefault('waiting_since', now)
        if now - since >= cfg['HEALTH_STARTUP_GRACE']:
            issues = {'startup_stalled': '服务反复启动或重启，持续未完成有效数据检查'}
            waiting = False
    elif not issues:
        state.pop('waiting_since', None)
    previous = state.get('issues', {})
    queue = state.setdefault('queue', [])
    codes = sorted(issues)
    if issues:
        if codes != sorted(previous) or now - state.get('last_alert_at', 0) >= cfg['HEALTH_REPEAT_SECONDS']:
            queue.append(message('健康告警', '\n'.join(issues.values()), now))
            state['last_alert_at'] = now
        state['issues'] = issues
    elif not waiting:
        if previous:
            queue.append(message('服务已恢复', '服务运行中，数据检查恢复正常，通知无超时积压。', now))
            state['last_report_at'] = now
        elif not state.get('initialized') or (cfg['HEALTH_REPORT_SECONDS'] and
                    now - state.get('last_report_at', 0) >= cfg['HEALTH_REPORT_SECONDS']):
            queue.append(message('运行正常', '独立健康检查已启用，服务及最近的数据读取正常。', now))
            state['last_report_at'] = now
        state['initialized'] = True
        state['issues'] = {}
    state['checked_at'] = now
    return issues, waiting


def save_state(path, state):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def run_check(cfg, health_path, state_path):
    now = time.time()
    state = read_json(state_path, {})
    if not isinstance(state.get('queue', []), list) or not isinstance(state.get('issues', {}), dict):
        raise HealthError('健康告警自身状态损坏，请备份后检查状态文件')
    try:
        status = service_status()
        try:
            health = read_json(health_path)
            issues, waiting = inspect_health(status, health, cfg, now, time.monotonic())
        except HealthError:
            issues, waiting = {'health_unreadable': '主程序健康状态不可读或损坏'}, False
    except HealthError:
        issues, waiting = {'systemd_query_failed': '无法查询 systemd 服务状态'}, False
    issues, waiting = advance(state, issues, waiting, cfg, now)
    save_state(state_path, state)  # 先落盘，通知失败留待下次；故障后恢复也不丢失故障事件。
    for _ in range(min(2, len(state['queue']))):
        send_feishu(cfg, state['queue'][0])
        state['queue'].pop(0)
        save_state(state_path, state)
        time.sleep(1.1)
    if not issues and not waiting and not state['queue'] and cfg.get('HEALTH_HEARTBEAT_URL'):
        http(cfg['HEALTH_HEARTBEAT_URL'])
    print(f"健康检查完成：{'启动宽限中' if waiting else '异常' if issues else '正常'}；待发告警 {len(state['queue'])}")
    return 1 if issues else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).resolve().parent / 'health.env')
    parser.add_argument('--health-file', type=Path, default=Path('/var/lib/binance-monitor/health.json'))
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/binance-monitor-health'))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--test-notification', action='store_true')
    modes.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    cfg = config(args.env_file)
    if args.check_config:
        print('独立健康告警配置格式有效（尚未验证飞书网络）')
        return 0
    if args.test_notification:
        send_feishu(cfg, message('独立告警通道测试', '测试消息，不代表服务发生故障。', time.time()))
        print('飞书已接收独立健康告警测试消息')
        return 0
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / 'check.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return run_check(cfg, args.health_file, args.state_dir / 'state.json')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # 不输出未知异常内容，其中可能含 URL 或配置值。
        print(str(exc) if isinstance(exc, HealthError) else '健康检查异常，请检查文件权限、磁盘及配置', file=sys.stderr)
        sys.exit(1)
