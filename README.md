# Agent Manager Daemon

Linux 下管理 agent 的守护进程（Python 3.12 + Flask）。两件事：

1. **Web 自助改 OS 密码。** 用户用 Linux 账号登录，重新验证当前密码后，自己设置新密码。底层用 `crypt(3)` 校验 `/etc/shadow`、`chpasswd` 更新 —— 不碰 PAM、不走 shell，没有命令注入面。
2. **对外 HTTP API，管理 agent 生命周期。** 外部调用方（CI/CD、控制面）`POST` 一个升级请求，守护进程从指定 FTP/FTPS 服务器下载升级包、解压到 `/opt/<agent>/releases/<版本>/`、原子切换 `current` 软链接、按需跑 post-install hook、重启 systemd unit，并保留最近 N 个版本用于回滚。

```
                        +--------------------+
   浏览器  ─HTTPS───► |  Flask app (gunicorn)
   （登录+表单）       |  ├── web 蓝图   │
                        |  └── api 蓝图   │ ──Bearer token──► 外部调用方
                        |        │
                        |        ▼
                        |  UpgradeManager
                        |   ├── FtpDownloader（urllib）
                        |   ├── ArchiveExtractor
                        |   ├── JobRegistry（落盘 JSON）
                        |   └── systemctl / chpasswd
                        +--------------------+
```

## 目录结构

```
src/agent_manager/
├── app.py            Flask factory
├── __main__.py       入口（python -m agent_manager）
├── config.py         YAML + 环境变量配置加载
├── auth.py           /etc/shadow 校验 + chpasswd 改密
├── web.py            登录 / 登出 / 改密页面
├── api.py            /api/v1/* 接口
├── api_auth.py       Bearer token 装饰器（配置只存 SHA-256 摘要）
├── upgrade.py        FTP 下载 + 解压 + 软链接 + systemctl
└── logging_setup.py
templates/             base.html、login.html、change_password.html
static/                style.css
systemd/               agent-manager.service
tests/                 pytest 测试集
config.yaml            默认运行时配置
README.md              中文（本文件）
README.en.md           英文版（与中文内容一一对应）
```

## 安装

提供了一键脚本 `install.sh`：

```bash
# 生产安装（推荐）
sudo ./install.sh

# 看清楚会跑什么
./install.sh -n

# 卸载（停 unit + 清掉它装的所有文件，但保留运行时数据）
sudo ./install.sh -u

# 自定义安装路径
sudo ./install.sh -p /srv/agent-manager-daemon

# 只 stage 文件、不动 systemd（适合容器/CI）
./install.sh --skip-systemd -p /tmp/test

# 用系统 python 跳过 venv（不推荐，除非空间紧张）
sudo ./install.sh --system-python

# 重新覆盖默认 config.yaml（默认会保护已有 config 不被覆盖）
sudo ./install.sh --force-config
```

`install.sh` 会：

1. 预检查 Python 3.12 / uv / systemd / root
2. 在 `<prefix>/.venv` 创建虚拟环境并装好依赖
3. 把项目源同步到 `<prefix>/`
4. 把配置复制到 `/etc/agent-manager/config.yaml`（mode 0600）
5. 创建运行时目录 `/var/lib/agent-manager`、`/var/log/agent-manager`
6. 安装 systemd unit 并 enable + start

守护进程默认绑定 **`0.0.0.0:8443`**，开启 **HTTPS**（`tls.mode: adhoc` —— 启动时生成自签证书，浏览器每次会弹"不安全"警告）。生产环境请切到 `tls.mode: explicit` 并把 `certfile`/`keyfile` 指向 Let's Encrypt 或内网 CA 签发的证书。详细说明见 `config.yaml` 注释和下面的 *安全说明*。

### 手动安装（不走脚本）

如果不想用脚本，照下面装：

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"

sudo install -d /etc/agent-manager
sudo cp config.yaml /etc/agent-manager/config.yaml
sudoedit /etc/agent-manager/config.yaml

sudo install -m 0644 systemd/agent-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-manager
```

## 配置

所有配置都在 `config.yaml` 里。任何键都可用环境变量覆盖，格式：

```
AGENT_MANAGER_<大写下划线路径>
```

嵌套字段用 `__` 分隔：

```bash
export AGENT_MANAGER_SERVER__BIND_PORT=9090
export AGENT_MANAGER_UPGRADE__FTP__URL=ftp://新主机/构建产物
export AGENT_MANAGER_FTP_USER=deployer
export AGENT_MANAGER_FTP_PASS=...
```

### API token

`config.yaml` 里的 token **只存 SHA-256 摘要**，不存明文。生成：

```bash
python -c "from agent_manager.api_auth import hash_token; print(hash_token('my-real-token'))"
```

把生成的十六进制串贴到 `api.tokens[]`。调用方发 `Authorization: Bearer my-real-token`。

### TLS

```yaml
tls:
  enabled: true
  mode: "adhoc"               # 开发自签；生产改 "explicit"
  certfile: ""                # explicit 模式必填
  keyfile: ""                 # explicit 模式必填
  min_version: "TLSv1.2"
```

切换到生产证书：

```yaml
tls:
  enabled: true
  mode: "explicit"
  certfile: /etc/letsencrypt/live/agent.example.com/fullchain.pem
  keyfile:  /etc/letsencrypt/live/agent.example.com/privkey.pem
  min_version: "TLSv1.2"
```

### 防火墙提示

`0.0.0.0:8443` 会监听所有网卡。生产环境建议加防火墙规则只放行必要来源：

```bash
# nftables 示例：仅允许内网网段 10.0.0.0/8 访问 8443
sudo nft add rule inet filter input ip saddr 10.0.0.0/8 tcp dport 8443 accept
sudo nft add rule inet filter input tcp dport 8443 drop
```

## API

所有接口都要求 `Authorization: Bearer <token>`（除非 `api.require_token: false`）。

| 方法 | 路径 | Body | 说明 |
|------|------|------|------|
| GET | `/api/v1/health` | — | 健康检查，返回 version + 配置摘要 |
| POST | `/api/v1/upgrades` | `{job_id, filename, version, ftp_url?}` | 触发升级，返回 202 |
| GET | `/api/v1/upgrades` | — | 列出所有 job（最新在前） |
| GET | `/api/v1/upgrades/<id>` | — | 单个 job 状态、日志、错误 |
| POST | `/api/v1/upgrades/<id>/rollback` | — | 把 `current` 切回上一个版本 |

`POST /api/v1/upgrades` 是**异步**的 —— 后台开工作线程，立刻返回 `202 Accepted`。客户端轮询 `GET /api/v1/upgrades/<id>` 看进度。

### 示例

```bash
TOKEN=my-real-token
curl -ks -X POST https://localhost:8443/api/v1/upgrades \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":"u-$(date +%s)","filename":"myagent-1.4.2.tar.gz","version":"1.4.2"}'
# {"job_id":"u-1700000000","status":"pending","poll_url":"/api/v1/upgrades/u-1700000000"}

curl -ks -H "Authorization: Bearer $TOKEN" \
  https://localhost:8443/api/v1/upgrades/u-1700000000
```

## 升级后的目录结构

```
/opt/myagent/
├── current -> releases/1.4.2          # 当前生效版本（原子软链接）
└── releases/
    ├── 1.4.2/
    │   └── bin/myagent
    ├── 1.4.1/
    └── 1.3.7/

/var/lib/agent-manager/
├── work/
│   ├── jobs.json                       # job 历史持久化
│   └── agent-1.4.2.tar.gz              # 最近一次下载的产物
└── ...
```

`upgrade.keep_releases` 控制保留几个版本，更老的升级成功后会清理掉。

## 安全说明

* **必须以 root 运行** —— 要读 `/etc/shadow`、调 `chpasswd`、重启 systemd unit。提供的 systemd unit 已经做了能做的 hardening（`ProtectSystem=strict`、`PrivateTmp` 等），但权限本身没法降。
* **API token 配置里只存 SHA-256 摘要**，不存明文。要轮换就追加新摘要，调用方迁移完再删旧的。
* **Web 登录用 Flask session cookie**。开启 HTTPS 后 `server.session_cookie_secure: true` 默认就是开 —— **不要关**，否则浏览器会明文回传 cookie。
* `web_allowed_users` 是可选白名单 —— 留空表示允许本机任意账号登录。
* **新密码前端校验**（≥8 字符、≤128 字符、不含控制字符）。要更强校验就上 `pam_pwquality` / `passwdqc` —— 守护进程用 `chpasswd`，会自动走 PAM 那套规则。
* **TLS**：`tls.mode: adhoc` 仅供开发。生产请用 `tls.mode: explicit` + 真实证书（Let's Encrypt / 内网 CA）。`tls.enabled: true` 但拿不到可用证书时守护进程会拒绝启动。
* **监听 0.0.0.0** 意味着任何能访问 8443 端口的客户端都能尝试登录或调 API。建议配合防火墙 / VPN / 反向代理做访问控制。

## 测试

```bash
uv run pytest -v
```

测试用 fake `/etc/shadow`、`/etc/passwd`、本地 HTTP 文件服务器代替 FTP、完全跳过 `systemctl` —— 不动本机真实账号和服务。

### 已知 Python 版本警告

* `crypt` 和 `spwd` 在 3.12 已 deprecated，**3.13 会被移除**。如果将来升级到 3.13+，需要迁移到 `passlib` 或 `cryptography` 自己做 shadow 校验。本项目当前按你要求锁定 3.12。
* `tarfile.TarFile.extractall` 在 3.14+ 要求 `filter=` 参数（防 zip-slip / 元数据注入）。升级前先补 `filter="data"`。

## 本机烟测（dev 模式）

```bash
uv run python -m agent_manager --dev
```

浏览器打开 <https://localhost:8443/login>。默认是 adhoc 自签证书，会弹"连接不安全"警告 —— 点继续，或在配置里换成可信证书。

dev 模式下用 Flask 自带 server（单进程，不适合生产）。生产用 systemd unit 起，由 gunicorn 跑。

## 手动触发回滚

CLI 本身不发命令，但可以通过 API 回滚到上一个版本：

```bash
curl -ks -X POST -H "Authorization: Bearer $TOKEN" \
  https://localhost:8443/api/v1/upgrades/<job_id>/rollback
```

回滚后 `current` 软链接指向 releases 目录里**字典序上一个**版本 —— 用可排序的版本号字符串（如 SemVer）即可。如果想做"回到任意指定版本"，在 `UpgradeManager.rollback` 里加个 `target_version` 参数即可，函数本身已经走的是软链接原子替换。