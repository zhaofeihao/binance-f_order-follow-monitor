import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import (Binance, MonitorError, Store, action, fingerprint, timestamp,
                     configured_feishu, feishu_sender, feishu_sign, main, load_env_file,
                     parse_options)


def row(t, **changes):
    data = dict(symbol='BTCUSDT', side='BUY', positionSide='LONG', type='MARKET',
                executedQty='1.000', avgPrice='60000.00', totalPnl='0',
                orderTime=t, orderUpdateTime=t)
    data.update(changes)
    return data


class MonitorTests(unittest.TestCase):
    def test_main_writes_current_process_health_and_notification_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / '.env').write_text('')
            with patch.dict('os.environ', {}, clear=True), \
                 patch('sys.argv', ['monitor.py', '--once', '--env-file', str(directory / '.env'),
                                    '--data-dir', str(directory / 'data')]), \
                 patch('monitor.signal.signal'), patch('monitor.logging.basicConfig'), \
                 patch('monitor.RotatingFileHandler'), \
                 patch('monitor.Binance') as client, \
                 patch('monitor.configured_feishu', return_value=Mock(side_effect=RuntimeError('offline'))):
                client.return_value.fetch.return_value = [row(1000)]
                self.assertEqual(main(), 0)
                client.return_value.fetch.return_value = [row(1000), row(2000)]
                with self.assertLogs('monitor', 'ERROR'):
                    self.assertEqual(main(), 1)
                health = json.loads((directory / 'data' / 'health.json').read_text())
                self.assertEqual(health['pid'], os.getpid())
                self.assertGreaterEqual(health['updated_at'], float(health['last_success']))
                self.assertEqual(health['poll_interval'], 30)
                self.assertEqual(health['notification_backlog']['feishu']['count'], 1)
                self.assertNotIn('console', health['notification_backlog'])

    def test_env_file_quotes_comments_and_literal_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('\ufeff# 配置\nexport A = "a#b=c" # 注释\n'
                            "B='$(do-not-execute) ${HOME} \\n'\nC=abc#123\nD=value # comment\nEMPTY=\n",
                            encoding='utf-8')
            env = {'A': 'process-value'}
            load_env_file(path, environ=env)
            self.assertEqual(env, {'A': 'process-value', 'B': '$(do-not-execute) ${HOME} \\n',
                                   'C': 'abc#123', 'D': 'value', 'EMPTY': ''})

    def test_env_file_parse_failure_is_atomic_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            for text in ('A=ok\nsecret-without-equals', 'A=ok\nB="private-secret',
                         'A=ok\nB="private-secret" unexpected'):
                with self.subTest(text=text):
                    path.write_text(text)
                    env = {}
                    with self.assertRaises(MonitorError) as caught:
                        load_env_file(path, environ=env)
                    self.assertEqual(env, {})
                    self.assertNotIn('secret', str(caught.exception))

    def test_env_file_missing_explicit_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'missing'
            load_env_file(path, environ={})
            with self.assertRaises(MonitorError):
                load_env_file(path, required=True, environ={})

    def test_env_options_priority_and_stable_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            env_file = directory / '.env'
            env_file.write_text('MONITOR_INTERVAL=60\nMONITOR_MAX_PAGES=5\nMONITOR_DATA_DIR=state\n')
            with patch.dict('os.environ', {'MONITOR_INTERVAL': '45'}, clear=True), patch('monitor.APP_DIR', directory):
                args = parse_options(['--interval', '20'])
                self.assertEqual(args.interval, 20)
                self.assertEqual(args.max_pages, 5)
                self.assertEqual(args.data_dir, directory / 'state')
                self.assertEqual(parse_options([]).interval, 45)
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(parse_options(['--env-file', str(env_file)]).interval, 60)

    def test_env_invalid_numeric_value_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('monitor.APP_DIR', Path(tmp)), \
             patch.dict('os.environ', {'MONITOR_INTERVAL': 'secret-value'}, clear=True):
            with self.assertRaises(MonitorError) as caught:
                parse_options([])
            self.assertNotIn('secret-value', str(caught.exception))

    def test_env_feishu_configuration_flows_into_test_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / '.env'
            env_file.write_text('FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/test-only\n'
                                'FEISHU_SECRET=test-secret\n')
            with patch.dict('os.environ', {}, clear=True), \
                 patch('sys.argv', ['monitor.py', '--env-file', str(env_file), '--test-feishu']), \
                 patch('monitor.request_json', return_value={'code': 0}) as request, \
                 patch('monitor.STOP.wait'), patch('monitor.signal.signal'), patch('builtins.print'):
                self.assertEqual(main(), 0)
                self.assertEqual(request.call_count, 1)
                self.assertIn('/hook/test-only', request.call_args.args[0])
                self.assertIn('sign', request.call_args.args[1])

    def test_feishu_signature_vector(self):
        # 独立使用 OpenSSL HMAC-SHA256 生成的固定结果。
        self.assertEqual(feishu_sign('test-secret', '1700000000'),
                         'mbm4Y4oluIPQ00qlBIhX8vAZ0EKv3nw0LuTb91jPL84=')

    def test_feishu_payload_and_fresh_retry_signature(self):
        url = 'https://open.feishu.cn/open-apis/bot/v2/hook/test-only'
        sender = feishu_sender(url, 'test-secret')
        with patch('monitor.request_json', return_value={'code': 0}) as request, \
             patch('monitor.time.time', side_effect=[1700000000, 1700000020]), \
             patch('monitor.STOP.wait'):
            sender('熬鹰资本 · BTCUSDT')
            sender('熬鹰资本 · BTCUSDT')
        first, second = [call.args[1] for call in request.call_args_list]
        self.assertEqual(request.call_args_list[0].args[0], url)
        self.assertEqual(first['msg_type'], 'text')
        self.assertIn('熬鹰资本 · BTCUSDT', first['content']['text'])
        self.assertEqual(first['timestamp'], '1700000000')
        self.assertEqual(second['timestamp'], '1700000020')
        self.assertNotEqual(first['sign'], second['sign'])
        self.assertNotIn('test-secret', str(first))

    def test_feishu_rejects_business_failures_and_malformed_responses(self):
        sender = feishu_sender('https://open.feishu.cn/open-apis/bot/v2/hook/test-only', 'test-secret')
        for response in ({'code': 19021}, {'code': 19024}, {'code': 19022}, {},
                         None, {'code': False}, {'code': 1, 'StatusCode': 0}):
            with self.subTest(response=response), patch('monitor.request_json', return_value=response):
                with self.assertRaises(MonitorError) as caught:
                    sender('测试')
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertNotIn('test-only', str(caught.exception))
        with patch('monitor.request_json', return_value={'StatusCode': 0}), patch('monitor.STOP.wait'):
            sender('兼容旧版成功响应')

    def test_feishu_configuration_validation(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(configured_feishu())
        for env in ({'FEISHU_SECRET': 'test-secret'}, {'FEISHU_WEBHOOK_URL': 'https://example.com'},
                    {'FEISHU_WEBHOOK_URL': 'https://example.com', 'FEISHU_SECRET': 'test-secret'}):
            with self.subTest(env=env), patch.dict('os.environ', env, clear=True):
                with self.assertRaises(MonitorError):
                    configured_feishu()

    def test_feishu_failure_persists_independently_and_retries_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            store = Store(path)
            store.save([row(1000)], 1, True, ['console', 'feishu'])
            store.save([row(2000)], 2, False, ['console', 'feishu'])
            console = Mock()
            sender = feishu_sender('https://open.feishu.cn/open-apis/bot/v2/hook/test-only', 'test-secret')
            with patch('monitor.request_json', return_value={'code': 19021}), self.assertLogs('monitor', 'ERROR'):
                store.deliver({'console': console, 'feishu': sender})
            store.db.close()
            store = Store(path)
            with patch('monitor.request_json', return_value={'code': 0}), patch('monitor.STOP.wait'):
                store.deliver({'console': console, 'feishu': sender})
            self.assertEqual(console.call_count, 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox WHERE delivered=0').fetchone()[0], 0)
            store.db.close()

    def test_feishu_test_mode_does_not_fetch_trades_or_create_state(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('sys.argv', ['monitor.py', '--test-feishu', '--data-dir', str(Path(tmp) / 'data')]), \
             patch('monitor.configured_feishu', return_value=Mock()) as config, \
             patch('monitor.Binance') as client, patch('monitor.signal.signal'), patch('builtins.print'):
            self.assertEqual(main(), 0)
            config.return_value.assert_called_once()
            client.return_value.verify.assert_not_called()
            client.return_value.fetch.assert_not_called()
            self.assertFalse((Path(tmp) / 'data').exists())

    def test_semantic_dedupe_and_update(self):
        a = row(1000)
        self.assertEqual(fingerprint(a), fingerprint(row(1000, avgPrice=60000, executedQty=1)))
        self.assertNotEqual(fingerprint(a), fingerprint(row(1000, executedQty=2)))
        self.assertEqual(timestamp(row(1000, orderUpdateTime=None)), 1000)

    def test_direction_is_not_overstated(self):
        self.assertEqual(action(row(1000)), '开多 / 加多')
        self.assertEqual(action(row(1000, side='BUY', positionSide='SHORT')), '减空 / 平空')
        self.assertIn('开平未知', action(row(1000, positionSide='BOTH')))

    def test_hidden_data_and_schema_fail_closed(self):
        client = Binance(spacing=0)
        client.api = Mock(return_value={'list': None, 'total': 1})
        with self.assertRaises(MonitorError):
            client.fetch()
        with self.assertRaises(MonitorError):
            fingerprint(row(1000, avgPrice='NaN'))

    def test_pagination_and_head_stability(self):
        client = Binance(page_size=2, spacing=0)
        a, b, c = row(3000), row(2000), row(1000)
        client.page = Mock(side_effect=[([a, b], 3), ([c], 3), ([a, b], 3)])
        self.assertEqual(client.fetch(), [c, b, a])
        client.page = Mock(side_effect=[([a, b], 3), ([c], 3), ([row(4000), a], 4)])
        with self.assertRaisesRegex(MonitorError, '分页期间'):
            client.fetch()

    def test_pagination_repeat_and_limit(self):
        client = Binance(page_size=1, max_pages=2, spacing=0)
        client.page = Mock(return_value=([row(1000)], 5))
        with self.assertRaisesRegex(MonitorError, '分页重复'):
            client.fetch()
        client.page = Mock(side_effect=[([row(3000)], 5), ([row(2000)], 5)])
        with self.assertRaisesRegex(MonitorError, '分页上限'):
            client.fetch()

    def test_incremental_overlap_includes_late_rows(self):
        client = Binance(page_size=2, spacing=0)
        a, b, c, d = [row(t) for t in (4000, 3000, 2000, 1000)]
        client.page = Mock(side_effect=[([a, b], 100), ([c, d], 100), ([a, b], 100)])
        self.assertEqual(client.fetch(cutoff=2500), [d, c, b, a])

    def test_restart_outbox_retry_and_initial_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            store = Store(path)
            self.assertEqual(store.save([row(1000)], 1, True, ['console', 'telegram']), (True, 0))
            self.assertEqual(store.save([row(1000), row(2000)], 2, False, ['console', 'telegram']), (False, 1))
            good, bad = Mock(), Mock(side_effect=RuntimeError('offline'))
            store.deliver({'console': good, 'telegram': bad})
            self.assertEqual(good.call_count, 1)
            store.db.close()
            store = Store(path)
            self.assertEqual(store.save([row(2000), row(1000)], 3, False, ['console', 'telegram']), (False, 0))
            retry = Mock()
            store.deliver({'console': good, 'telegram': retry})
            self.assertEqual(good.call_count, 1)
            self.assertEqual(retry.call_count, 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox WHERE delivered=0').fetchone()[0], 0)
            store.db.close()

    def test_atomic_failure_preserves_watermark(self):
        store = Store(':memory:')
        store.save([row(1000)], 1, True, ['console'])
        invalid = copy.deepcopy(row(3000))
        del invalid['symbol']
        with self.assertRaises(MonitorError):
            store.save([row(2000), invalid], 2, False, ['console'])
        self.assertEqual(store.get('last_success'), '1')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        store.db.close()


if __name__ == '__main__':
    unittest.main()
