# 熬鹰资本公开操作记录监控

定时读取币安带单项目「熬鹰资本」（`5075281354358777856`）的公开成交操作记录，并通过飞书自定义机器人、Telegram 或 macOS 桌面通知提醒。

- Python 3.9+，仅标准库，无需币安账号或 API Key。
- 默认每轮结束等待约 30 秒；分页和网络耗时会增加实际检查间隔。
- 首次启动静默建立历史基准，后续记录持久化去重，重启后继续补查。
- SQLite 保存记录与各通知通道的待发队列，失败后重试。
- 飞书支持 Webhook 签名校验；提供 Linux systemd 服务模板。

## 快速开始

```bash
git clone git@github.com:zhaofeihao/binance-f_order-follow-monitor.git
cd binance-f_order-follow-monitor
cp -n .env.example .env
chmod 600 .env
```

编辑 `.env`，填写飞书配置：

```dotenv
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的地址
FEISHU_SECRET=你的签名密钥
MONITOR_INTERVAL=30
```

先检查服务器网络和通知，再启动监控：

```bash
python3 monitor.py --check
python3 monitor.py --test-feishu
python3 monitor.py
```

`--test-feishu` 会向机器人所在群发送一条测试消息。未填写通知配置时，仅记录终端日志。程序自动读取脚本同目录 `.env`，也支持 `--env-file /绝对路径/配置文件`；命令行参数优先于进程环境变量，环境变量优先于 `.env`。

## 服务器部署

支持具备 Python 3.9+、CA 证书和时区数据库的 Linux 服务器。服务器需要能访问币安和飞书的 HTTPS 接口，无需开放入站端口。长期运行请按 [服务器部署说明](服务器部署.txt) 安装 [systemd 服务](binance-monitor.service)，以便退出 SSH 后继续运行、开机自启及异常重启。

服务模板将代码放在 `/opt/binance-monitor`，将状态和日志保存到 `/var/lib/binance-monitor`。升级时保留 `.env` 和数据目录。仅提供部署文件，克隆仓库不会自动安装或启动服务。

## 验证与说明

```bash
python3 -m unittest -v
```

更多配置、通知含义和运行方式见 [使用说明](使用说明.txt)，已有测试记录见 [验证结果](验证结果.txt)。Linux 服务需在目标服务器上验证。

监控使用币安网页公开接口，接口可能变动或限制访问；提醒延迟受公开数据更新时间影响。公开记录无法准确区分首次开仓与加仓、部分减仓与完全平仓，因此通知会明确保留这些不确定性。程序仅监控，不执行交易。

`.env`、`data/`、日志和数据库不纳入版本管理。请勿将真实 Webhook、secret 或运行数据提交到仓库。
