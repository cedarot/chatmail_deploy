# Chatmail Deploy

用于在新服务器上部署 Dockerized Chatmail Relay 的安装脚本。

项目提供两种部署方式：

- `install.sh`：使用正式域名部署，支持外部 TLS 证书或 Chatmail ACME 自动签发。
- `mock_install.sh`：没有域名和证书时，使用内网 IPv4 和自签名证书部署测试环境。

上游项目：[chatmail/docker](https://github.com/chatmail/docker)

> Linux、macOS、Windows、Android 和 iOS/iPadOS 上的 Delta Chat 客户端安装方法，请阅读[客户端安装指南](./docs/installation.md)。Chatmail Relay 服务端安装仍以本文为准。

## 功能

- 自动安装 Docker Engine 和 Docker Compose v2
- 支持 Debian/Ubuntu 和常见 RPM 系发行版
- 使用官方 Chatmail Compose 配置和容器镜像
- 支持 DaoCloud Docker Hub 与 GHCR 镜像加速
- 创建宿主机持久化数据目录
- 自动生成并挂载 `chatmail.ini`
- 支持 Nginx 证书 ZIP 或独立 PEM/KEY 文件
- 校验证书有效期、域名覆盖和公私钥匹配
- 支持无域名、无 DNS、无证书的 IPv4-only 测试部署
- 检查常用端口占用和容器健康状态
- 抽取生成后的 nginx 配置供维护和排障
- 可选本机通知网关：支持 iOS 后台/锁屏收信通知的服务器出口优化、加密 DNS 刷新和队列健康检查

网关用于 `chatmail-metadata → 官方通知服务 → APNs → iPhone 唤醒并经 IMAP 收信`。它不承诺手机断网期间收到推送，也不承诺固定的 iOS 到达秒数。详细部署、已有服务器启用、验证和回滚见 [iOS 通知与网关部署](./docs/notifications.md)。

## 支持的操作系统

- Debian
- Ubuntu
- RHEL
- CentOS
- Rocky Linux
- AlmaLinux
- Oracle Linux
- Fedora

推荐使用 `x86_64/amd64`。Chatmail Docker 对其他架构的支持可能未经充分测试。

## 项目文件

```text
chatmail_deploy/
├── README.md
├── MOCK_INSTALL.md
├── docs/
│   ├── installation.md
│   └── notifications.md
├── config/notification-nginx.conf
├── notification_gateway.py
├── notification_metadata.py
├── tests/
├── install.sh
└── mock_install.sh
```

| 文件 | 用途 |
|---|---|
| `install.sh` | 正式域名部署脚本 |
| `mock_install.sh` | 内网 IPv4-only 测试部署脚本 |
| `MOCK_INSTALL.md` | Mock/IP-only 完整使用文档 |
| `docs/installation.md` | Delta Chat 客户端跨平台安装指南 |
| `docs/notifications.md` | iOS 后台/锁屏通知、网关部署与诊断 |
| `notification_gateway.py` | 已有部署审计、启用/停用网关、地址刷新和健康检查 |
| `notification_metadata.py` | 使用当前安装包的 metadata 启动适配器，不覆盖整份模块 |

## 前置要求

- 新服务器或专用于 Chatmail 的服务器
- `root` 权限，或者能够使用 `sudo`
- 服务器能够访问系统软件源和容器镜像仓库
- 正式模式需要域名及对应 DNS 记录
- Mock 模式只需要一个稳定且客户端可以访问的 IPv4 地址

Chatmail 使用 Docker host 网络模式。服务器上的 nginx、Postfix、Dovecot、DNS 服务或其他程序可能与 Chatmail 发生端口冲突。

常见端口：

```text
25, 53, 80, 143, 443, 465, 587, 993, 3340, 8443
```

## 获取项目

```bash
git clone https://github.com/cedarot/chatmail_deploy.git
cd chatmail_deploy
```

确认脚本具有可执行权限：

```bash
chmod +x install.sh mock_install.sh
```

## 正式部署

### 使用 Nginx 证书 ZIP

适用于阿里云、腾讯云等平台下载的 Nginx 格式证书包：

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --cert-zip /tmp/example_nginx.zip
```

ZIP 中应包含：

- PEM、CRT 或 CER 格式的证书链
- 未加密的 PEM 私钥文件

脚本会在部署前检查：

- 证书是否可以解析
- 证书是否过期
- 证书是否覆盖指定域名
- 证书与私钥是否匹配

### 使用独立证书和私钥

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --cert /root/fullchain.pem \
  --key /root/privkey.pem
```

### 使用 Chatmail ACME 自动签发

域名和 DNS 已正确配置时，可以不提供证书：

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --acme-email admin@example.org
```

### 新装时启用 iOS 通知网关

网关默认关闭。需要优化通知出口时，在完整仓库目录中显式启用：

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --cert-zip /tmp/example_nginx.zip \
  --notification-gateway
```

依赖 Python 3.9+、systemd、Docker Compose、curl、OpenSSL 和系统 CA。网关只监听 `127.0.0.1:10089`，不需要开放公网端口；默认开启 SNI 并严格验证官方证书。`--notification-sni-workaround` 仅允许脚本执行严格的同 IP 对照测试，证明必要后才关闭 SNI，不会关闭证书验证。

已有安装请使用[独立启用命令](./docs/notifications.md#已有部署)，不要重跑全量安装器。`--no-start` 会延后网关启用，主服务手动启动后再运行独立命令。

### 正式部署常用参数

```text
--domain DOMAIN       Chatmail 完整域名，必填
--cert FILE           PEM/full-chain 证书文件
--key FILE            未加密的 PEM 私钥文件
--cert-zip FILE       Nginx 格式证书 ZIP
--acme-email EMAIL    ACME 联系邮箱
--install-dir DIR     安装目录，默认 /srv/chatmail-relay
--image IMAGE         自定义 Chatmail 镜像
--disable-ipv6        禁用 IPv6
--no-daocloud         禁用 DaoCloud 加速
--no-start            只准备文件和镜像，不启动服务
--no-extract          不抽取生成后的 nginx 配置
--notification-gateway
                     启用可选通知网关和维护 timer；默认关闭
--notification-gateway-image IMAGE
                     指定 Nginx 镜像，拉取后固定实际 digest
--notification-sni-workaround
                     仅在同 IP 对照证明必要时允许关闭上游 SNI
```

查看实时帮助：

```bash
./install.sh --help
```

## Mock/IP-only 部署

没有域名、DNS 和公共 CA 证书时，可以在内网中使用 IPv4-only 模式。

自动探测服务器内网 IPv4：

```bash
sudo ./mock_install.sh
```

手动指定地址：

```bash
sudo ./mock_install.sh --ip 192.168.10.20
```

该模式会：

- 将 IPv4 地址用作 Chatmail 身份
- 自动跳过 DNS 检查
- 禁用 IPv6
- 由 Chatmail 自动生成自签名 TLS 证书
- 使用类似 `user@[192.168.10.20]` 的账号地址

> Mock/IP-only 是实验模式，仅推荐用于可信内网、开发和测试。客户端必须允许自签名证书。

完整说明请阅读 [MOCK_INSTALL.md](./MOCK_INSTALL.md)。

## DaoCloud 加速

脚本默认配置 Docker Hub 镜像加速器：

```text
https://docker.m.daocloud.io
```

并默认通过 DaoCloud 拉取 Chatmail GHCR 镜像：

```text
ghcr.m.daocloud.io/chatmail/docker:main
```

Docker 的 `registry-mirrors` 只代理 Docker Hub，因此 GHCR 镜像需要使用单独的 DaoCloud 映射地址。

脚本会合并 `/etc/docker/daemon.json` 中的原有配置，并在修改前创建时间戳备份。禁用 DaoCloud：

```bash
sudo ./install.sh --domain chat.example.org --no-daocloud
```

## 部署目录

默认安装目录是 `/srv/chatmail-relay`：

```text
/srv/chatmail-relay/
├── .env
├── install.conf
├── docker-compose.yaml
├── docker-compose.override.yaml
├── config/
│   ├── chatmail.ini
│   └── nginx.generated.conf
└── data/
    ├── certs/
    ├── dkim/
    ├── mail/
    └── www/
```

正式外部证书默认保存在：

```text
/srv/chatmail-relay/data/certs/external/fullchain.pem
/srv/chatmail-relay/data/certs/external/privkey.pem
```

容器内对应路径：

```text
/var/lib/acme/external/fullchain.pem
/var/lib/acme/external/privkey.pem
```

## 部署后检查

查看容器状态：

```bash
cd /srv/chatmail-relay
sudo docker compose ps
```

查看日志：

```bash
sudo docker compose logs -f chatmail
```

检查 Chatmail 状态：

```bash
sudo docker exec chatmail cmdeploy status --ssh-host @local
```

显示正式部署所需的 DNS 记录：

```bash
sudo docker exec chatmail cmdeploy dns --ssh-host @local
```

测试 HTTPS：

```bash
curl -I https://chat.example.org/
```

Mock 模式使用自签名证书：

```bash
curl -kI https://192.168.10.20/
```

## 更新

已启用通知网关时，先按[网关更新步骤](./docs/notifications.md#更新与回滚)停用适配器并备份，再更新主镜像；镜像改变后需要重新审计并启用。下面的普通更新命令适用于未启用网关的部署。已有容器内非持久化的 Postfix spool 需先处理在途投递，避免重建丢失任务。

```bash
cd /srv/chatmail-relay
sudo docker compose pull
sudo docker compose up -d --no-build
```

更新前建议备份整个安装目录，特别是以下持久化数据：

```text
data/mail/
data/dkim/
data/certs/
config/chatmail.ini
```

## 安全说明

- 不要提交或公开私钥文件。
- `/srv/chatmail-relay/data/certs/external/privkey.pem` 默认权限为 `0600`。
- Mock 模式的自签名证书不能验证服务器真实身份。
- 正式公网部署应使用有效域名和公共 CA 证书。
- Chatmail 容器使用 host 网络以及 systemd 所需的宿主机权限，建议部署在专用服务器上。
- 执行脚本前请确认端口没有被现有邮件、Web 或 DNS 服务占用。
- 通知网关不会清空 `pending_notifications`，日志不输出设备令牌、通知正文、密码或邮件内容。维护前应停用本组件 timer，恢复后重新启用。

## 开发验证

```bash
bash -n install.sh mock_install.sh
python3 -m unittest discover -s tests -v
git diff --check
```

测试使用临时目录和隔离的命令/网络响应，覆盖通知出口验证、失败回滚、启用/停用、日志脱敏和恢复冷却；不能替代真实部署和 iPhone 锁屏测试。

## 参考资料

- [Chatmail Docker](https://github.com/chatmail/docker)
- [Chatmail Relay 文档](https://chatmail.at/doc/relay/)
- [Chatmail IP-only 模式](https://chatmail.at/doc/relay/iponly.html)
- [DaoCloud Public Image Mirror](https://github.com/DaoCloud/public-image-mirror)

## License

本项目尚未附带许可证。上游 Chatmail 项目及其组件分别遵循各自的许可证。
