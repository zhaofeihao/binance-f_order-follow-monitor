#!/usr/bin/env bash
# 在已完成服务器部署步骤 1～5 后运行；同样适用于升级已有部署。
set -euo pipefail
if [[ "$(uname -s)" != Linux || "$EUID" != 0 ]]; then
  echo '请在使用 systemd 的 Linux 服务器上执行 sudo bash install-health.sh' >&2
  exit 1
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
for tool in systemctl systemd-analyze runuser; do
  command -v "$tool" >/dev/null
done
id binance-monitor >/dev/null
if [[ ! -f /opt/binance-monitor/.env ]]; then
  echo '请先按服务器部署说明创建运行账号及 /opt/binance-monitor/.env' >&2
  exit 1
fi
/usr/bin/python3 - <<'PY'
import sys
from pathlib import Path
if sys.version_info < (3, 9):
    raise SystemExit('需要 Python 3.9+')
for name in ('monitor.py', 'healthcheck.py'):
    compile(Path(name).read_text(encoding='utf-8'), name, 'exec')
PY
install -m 0644 monitor.py healthcheck.py health.env.example /opt/binance-monitor/
if [[ ! -e /opt/binance-monitor/health.env ]]; then
  # 只在首次创建时复用已有机器人；以后保留独立告警配置。
  install -o root -g binance-monitor -m 0640 /opt/binance-monitor/.env /opt/binance-monitor/health.env
fi
chown root:binance-monitor /opt/binance-monitor/health.env
chmod 0640 /opt/binance-monitor/health.env
runuser -u binance-monitor -- /usr/bin/python3 /opt/binance-monitor/healthcheck.py \
  --env-file /opt/binance-monitor/health.env --check-config
install -d -o binance-monitor -g binance-monitor -m 0700 /var/lib/binance-monitor /var/lib/binance-monitor-health
install -m 0644 binance-monitor.service binance-monitor-health.service binance-monitor-health.timer /opt/binance-monitor/
systemd-analyze verify /opt/binance-monitor/binance-monitor.service \
  /opt/binance-monitor/binance-monitor-health.service /opt/binance-monitor/binance-monitor-health.timer
install -m 0644 binance-monitor.service binance-monitor-health.service binance-monitor-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable binance-monitor.service
# 不在重启前读取旧版健康文件；即使重启失败，也必须继续启用独立检查。
restart_failed=0
systemctl restart binance-monitor.service || restart_failed=1
systemctl enable --now binance-monitor-health.timer
if [[ "$restart_failed" == 1 ]]; then
  echo '主服务重启失败，但独立健康检查已启用；请检查主服务日志。' >&2
  exit 1
fi
echo '主服务已重启，独立健康检查已启用；原 .env 和交易数据库保持不变。'
echo '首次建立健康状态后将发送运行正常通知；请按健康告警说明测试独立通知通道。'
