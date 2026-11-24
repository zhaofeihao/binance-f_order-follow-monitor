import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import healthcheck as h


def settings():
    return {'FEISHU_WEBHOOK_URL': 'https://open.feishu.cn/open-apis/bot/v2/hook/test-only',
            'FEISHU_SECRET': 'test-secret', 'HEALTH_STALE_SECONDS': 300,
            'HEALTH_STARTUP_GRACE': 180, 'HEALTH_BACKLOG_SECONDS': 300,
            'HEALTH_REPEAT_SECONDS': 1800, 'HEALTH_REPORT_SECONDS': 86400}


def running():
    return {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
            'MainPID': '123', 'ActiveEnterTimestampMonotonic': '1000000'}


def healthy():
    return {'pid': 123, 'updated_at': 9990, 'last_success': 9990, 'started_at': 9000,
            'poll_interval': 30, 'notification_backlog': {}}


class HealthTests(unittest.TestCase):
    def test_service_stopped_failed_and_restart_exhausted(self):
        for active, sub in [('inactive', 'dead'), ('failed', 'failed'), ('activating', 'auto-restart')]:
            status = {**running(), 'ActiveState': active, 'SubState': sub}
            issues, waiting = h.inspect_health(status, healthy(), settings(), 10000, 1000)
            self.assertIn('service_down', issues)
            self.assertFalse(waiting)

    def test_startup_grace_old_pid_then_alarm(self):
        for health in (None, {**healthy(), 'pid': 122}):
            self.assertEqual(h.inspect_health(running(), health, settings(), 10000, 100), ({}, True))
            issues, waiting = h.inspect_health(running(), health, settings(), 10000, 1000)
            self.assertTrue(issues)
            self.assertFalse(waiting)

    def test_frozen_process_and_stale_fetch(self):
        data = {**healthy(), 'updated_at': 9500, 'last_success': 9400}
        issues, _ = h.inspect_health(running(), data, settings(), 10000, 1000)
        self.assertEqual(set(issues), {'heartbeat_stale', 'fetch_stale'})
        data['updated_at'] = 9990
        self.assertEqual(set(h.inspect_health(running(), data, settings(), 10000, 1000)[0]), {'fetch_stale'})

    def test_long_poll_interval_and_backlog(self):
        data = {**healthy(), 'updated_at': 9500, 'last_success': 9400, 'poll_interval': 300}
        self.assertFalse(h.inspect_health(running(), data, settings(), 10000, 1000)[0])
        data['notification_backlog'] = {'feishu': {'count': 2, 'oldest_at': 9600}}
        self.assertIn('notification_backlog', h.inspect_health(running(), data, settings(), 10000, 1000)[0])

    def test_invalid_and_future_health(self):
        for value in ('not-a-number', float('nan'), -1):
            data = {**healthy(), 'updated_at': value}
            self.assertIn('health_invalid', h.inspect_health(running(), data, settings(), 10000, 1000)[0])
        data = {**healthy(), 'updated_at': 11000}
        self.assertIn('clock_skew', h.inspect_health(running(), data, settings(), 10000, 1000)[0])

    def test_alert_dedupe_repeat_recovery_and_daily_report(self):
        state, cfg = {}, settings()
        h.advance(state, {'down': '停止'}, False, cfg, 10000)
        self.assertEqual(len(state['queue']), 1)
        h.advance(state, {'down': '停止'}, False, cfg, 10060)
        self.assertEqual(len(state['queue']), 1)
        h.advance(state, {'down': '停止'}, False, cfg, 11800)
        self.assertEqual(len(state['queue']), 2)
        h.advance(state, {}, False, cfg, 11900)
        self.assertIn('服务已恢复', state['queue'][-1])
        h.advance(state, {}, False, cfg, 11960)
        self.assertEqual(len(state['queue']), 3)
        h.advance(state, {}, False, cfg, 11900 + 86400)
        self.assertEqual(len(state['queue']), 4)

    def test_startup_does_not_clear_incident_or_send_false_recovery(self):
        state = {}
        h.advance(state, {'down': '停止'}, False, settings(), 10000)
        h.advance(state, {}, True, settings(), 10060)
        self.assertEqual(state['issues'], {'down': '停止'})
        self.assertEqual(len(state['queue']), 1)

    def test_restarting_cannot_extend_startup_grace_forever(self):
        state = {}
        self.assertEqual(h.advance(state, {}, True, settings(), 10000), ({}, True))
        issues, waiting = h.advance(state, {}, True, settings(), 10200)
        self.assertIn('startup_stalled', issues)
        self.assertFalse(waiting)
        self.assertEqual(len(state['queue']), 1)
        h.advance(state, {}, False, settings(), 10300)
        self.assertNotIn('waiting_since', state)
        self.assertIn('服务已恢复', state['queue'][-1])

    def test_first_healthy_confirmation_and_report_disabled(self):
        cfg, state = {**settings(), 'HEALTH_REPORT_SECONDS': 0}, {}
        h.advance(state, {}, False, cfg, 10000)
        h.advance(state, {}, False, cfg, 100000)
        self.assertEqual(len(state['queue']), 1)
        self.assertIn('运行正常', state['queue'][0])

    def test_queue_survives_failed_send_and_delivers_incident_before_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, health = Path(tmp) / 'state.json', Path(tmp) / 'health.json'
            health.write_text(json.dumps(healthy()))
            with patch('healthcheck.time.time', return_value=10000), \
                 patch('healthcheck.time.monotonic', return_value=1000), \
                 patch('healthcheck.service_status', return_value={**running(), 'ActiveState': 'failed'}), \
                 patch('healthcheck.send_feishu', side_effect=h.HealthError('网络失败')):
                with self.assertRaises(h.HealthError):
                    h.run_check(settings(), health, state)
            self.assertEqual(len(json.loads(state.read_text())['queue']), 1)
            with patch('healthcheck.time.time', return_value=10000), \
                 patch('healthcheck.time.monotonic', return_value=1000), \
                 patch('healthcheck.service_status', return_value=running()), \
                 patch('healthcheck.time.sleep'), patch('healthcheck.send_feishu') as send, patch('builtins.print'):
                self.assertEqual(h.run_check(settings(), health, state), 0)
                self.assertIn('健康告警', send.call_args_list[0].args[1])
                self.assertIn('服务已恢复', send.call_args_list[1].args[1])
            self.assertEqual(json.loads(state.read_text())['queue'], [])

    def test_external_heartbeat_sent_only_when_healthy(self):
        cfg = {**settings(), 'HEALTH_HEARTBEAT_URL': 'https://example.invalid/heartbeat'}
        with tempfile.TemporaryDirectory() as tmp:
            health, state = Path(tmp) / 'health.json', Path(tmp) / 'state.json'
            health.write_text(json.dumps(healthy()))
            with patch('healthcheck.time.time', return_value=10000), \
                 patch('healthcheck.time.monotonic', return_value=1000), \
                 patch('healthcheck.service_status', return_value=running()) as status, \
                 patch('healthcheck.send_feishu'), patch('healthcheck.time.sleep'), \
                 patch('healthcheck.http') as http, patch('builtins.print'):
                h.run_check(cfg, health, state)
                http.assert_called_once_with(cfg['HEALTH_HEARTBEAT_URL'])
                http.reset_mock()
                status.return_value = {**running(), 'ActiveState': 'failed'}
                h.run_check(cfg, health, state)
                http.assert_not_called()

    def test_bad_systemd_query_and_corrupt_health_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            health, state = Path(tmp) / 'health.json', Path(tmp) / 'state.json'
            health.write_text('{broken')
            with patch('healthcheck.service_status', return_value=running()), \
                 patch('healthcheck.send_feishu') as send, patch('healthcheck.time.sleep'), patch('builtins.print'):
                self.assertEqual(h.run_check(settings(), health, state), 1)
                self.assertIn('损坏', send.call_args.args[1])
            with patch('healthcheck.service_status', side_effect=h.HealthError('failed')), \
                 patch('healthcheck.send_feishu') as send, patch('healthcheck.time.sleep'), patch('builtins.print'):
                self.assertEqual(h.run_check(settings(), health, state), 1)
                self.assertIn('systemd', send.call_args.args[1])

    def test_independent_config_ignores_broken_main_options(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / 'health.env'
            path.write_text('MONITOR_INTERVAL="broken\nFEISHU_WEBHOOK_URL=' + settings()['FEISHU_WEBHOOK_URL'] +
                            '\nFEISHU_SECRET="test-secret"\n')
            self.assertEqual(h.config(path)['HEALTH_STALE_SECONDS'], 300)

    def test_signature_and_error_redaction(self):
        with patch('healthcheck.time.time', return_value=1700000000), \
             patch('healthcheck.http', return_value={'code': 0}) as http:
            h.send_feishu(settings(), '测试')
            self.assertEqual(http.call_args.args[1]['sign'], 'mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84=')
        with patch('healthcheck.http', return_value={'code': 19021, 'msg': 'test-secret'}):
            with self.assertRaises(h.HealthError) as caught:
                h.send_feishu(settings(), '测试')
            self.assertNotIn('test-secret', str(caught.exception))

    def test_runs_without_importing_broken_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            shutil.copyfile(Path(h.__file__), directory / 'healthcheck.py')
            (directory / 'monitor.py').write_text('this is not valid Python !!!')
            result = subprocess.run([sys.executable, str(directory / 'healthcheck.py'), '--help'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertIn('--test-notification', result.stdout)


if __name__ == '__main__':
    unittest.main()
