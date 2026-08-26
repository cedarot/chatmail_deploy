# Mock/IP-only Chatmail 部署指南

`mock_install.sh` 用于没有域名、没有公网 DNS、没有受信任证书的内网或测试环境。

脚本使用服务器的 IPv4 地址作为 Chatmail 身份，并调用 Chatmail 官方支持的 IP-only 模式完成部署。该模式会跳过 DNS 检查并使用自签名 TLS 证书。

> IP-only 属于实验模式，适合内网测试和功能验证，不建议直接用于正式公网服务。自签名证书不能提供由公共 CA 验证的服务器身份，客户端需要允许无效或自签名证书。

## 1. 系统要求

- Debian、Ubuntu、RHEL、CentOS、Rocky Linux、AlmaLinux、Oracle Linux 或 Fedora
- `x86_64/amd64` 架构优先；Chatmail 官方目前主要测试该架构
- 使用 `root`，或者通过 `sudo` 执行脚本
- 服务器能够访问软件源、GitHub 和容器镜像仓库
- 服务器拥有一个稳定的 IPv4 地址
- 所需端口没有被其他服务占用

Chatmail 使用 host 网络模式，常见端口包括：

```text
25, 53, 80, 143, 443, 465, 587, 993, 3340, 8443
```

## 2. 最简单的用法

进入项目目录：

```bash
cd ~/project/chatmail_deploy
```

允许脚本自动探测默认路由使用的内网 IPv4：

```bash
sudo ./mock_install.sh
```

脚本会显示探测到的地址，例如：

```text
Detected IPv4 address: 192.168.10.20
```

确认地址正确后，脚本会继续安装 Docker、配置 DaoCloud 加速、拉取 Chatmail 镜像并启动容器。

## 3. 手动指定内网 IP

如果服务器有多个网卡，建议明确指定客户端能够访问的内网地址：

```bash
sudo ./mock_install.sh --ip 192.168.10.20
```

其他示例：

```bash
sudo ./mock_install.sh --ip 10.20.0.5
sudo ./mock_install.sh --ip 172.16.30.8
```

默认允许以下地址范围：

- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `100.64.0.0/10`（运营商级 NAT）
- `169.254.0.0/16`（链路本地地址）

建议不要使用 DHCP 可能随时变化的地址。如果服务器地址发生变化，Chatmail 配置和已有账号地址不会自动迁移。

## 4. 常用参数

查看完整帮助：

```bash
./mock_install.sh --help
```

### 指定安装目录

默认安装到 `/srv/chatmail-relay`：

```bash
sudo ./mock_install.sh \
  --ip 192.168.10.20 \
  --install-dir /srv/chatmail-mock
```

### 只准备文件，不启动容器

```bash
sudo ./mock_install.sh \
  --ip 192.168.10.20 \
  --no-start
```

准备完成后手动启动：

```bash
cd /srv/chatmail-relay
sudo docker compose up -d --no-build
```

### 不使用 DaoCloud

脚本默认使用 DaoCloud 加速 Docker Hub 和 Chatmail GHCR 镜像。禁用方式：

```bash
sudo ./mock_install.sh \
  --ip 192.168.10.20 \
  --no-daocloud
```

### 指定其他 Chatmail 镜像

```bash
sudo ./mock_install.sh \
  --ip 192.168.10.20 \
  --image ghcr.io/chatmail/docker:main
```

### 跳过 nginx 配置抽取

```bash
sudo ./mock_install.sh \
  --ip 192.168.10.20 \
  --no-extract
```

### 允许公网 IPv4

公网地址默认会被拒绝，防止误把测试模式部署到公网。如果确定需要使用 IP-only 公网测试：

```bash
sudo ./mock_install.sh \
  --ip 203.0.113.10 \
  --allow-public-ip
```

公网 IP-only 模式仍然使用自签名证书，并不等同于正式域名部署。

## 5. 部署结果

使用默认安装目录时，主要文件位于：

```text
/srv/chatmail-relay/.env
/srv/chatmail-relay/install.conf
/srv/chatmail-relay/docker-compose.yaml
/srv/chatmail-relay/docker-compose.override.yaml
/srv/chatmail-relay/config/chatmail.ini
/srv/chatmail-relay/config/nginx.generated.conf
/srv/chatmail-relay/data/mail/
/srv/chatmail-relay/data/dkim/
/srv/chatmail-relay/data/certs/
```

`.env` 中的 `MAIL_DOMAIN` 是 IPv4 地址，例如：

```dotenv
MAIL_DOMAIN=192.168.10.20
```

IP-only Chatmail 账号采用地址字面量格式，例如：

```text
alice@[192.168.10.20]
```

## 6. 检查部署状态

查看容器状态：

```bash
cd /srv/chatmail-relay
sudo docker compose ps
```

查看启动日志：

```bash
sudo docker compose logs -f chatmail
```

查看 Chatmail 状态：

```bash
sudo docker exec chatmail cmdeploy status --ssh-host @local
```

确认服务端口：

```bash
sudo ss -lntup | grep -E ':(25|80|143|443|465|587|993|3340|8443)\\b'
```

测试 HTTPS：

```bash
curl -kI https://192.168.10.20/
```

这里使用 `-k` 是因为 IP-only 模式使用自签名证书。不要把 `-k` 作为正式公网域名部署的解决方案。

查看服务端证书：

```bash
openssl s_client \
  -connect 192.168.10.20:443 \
  -servername 192.168.10.20 </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -fingerprint -sha256
```

## 7. 客户端连接

连接 IP-only relay 的客户端必须能够访问服务器内网 IP，并允许自签名证书。

如果客户端启用了严格证书校验，连接会出现类似错误：

```text
self-signed certificate
certificate verify failed
```

这在 mock/IP-only 模式中是预期行为。仅在可信内网或测试设备上允许无效证书，不要在不可信网络中忽略证书校验。

## 8. 更新和重启

拉取新镜像并重建容器：

```bash
cd /srv/chatmail-relay
sudo docker compose pull
sudo docker compose up -d --no-build
```

重启 Chatmail：

```bash
sudo docker restart chatmail
```

## 9. 常见问题

### 自动探测到了错误的地址

多网卡服务器可能探测到管理网、容器网或其他不可访问地址。手动传入客户端可以访问的 IP：

```bash
sudo ./mock_install.sh --ip 192.168.10.20
```

### 提示端口已经被占用

查找占用进程：

```bash
sudo ss -lntup
```

Chatmail 使用 host 网络，不能与宿主机上监听相同端口的 nginx、Postfix、Dovecot 或 DNS 服务并存。停止冲突服务后重新运行脚本。

### 容器一直不是 healthy

查看日志：

```bash
sudo docker logs --tail 300 chatmail
sudo docker exec chatmail systemctl --failed
```

检查生成配置：

```bash
sudo sed -n '1,220p' /srv/chatmail-relay/config/chatmail.ini
```

### 浏览器显示证书不受信任

这是自签名证书的正常表现。mock 模式没有域名，也没有公共 CA 证书。正式部署时应改用 `install.sh`，提供域名和有效证书，或允许 Chatmail 通过 ACME 自动申请证书。

## 10. 与正式安装脚本的区别

| 项目 | `mock_install.sh` | `install.sh` |
|---|---|---|
| 身份 | IPv4 地址 | 域名或 IPv4 |
| DNS | 不需要 | 正式域名需要正确 DNS |
| TLS | 自动生成自签名证书 | ACME 或外部证书 |
| DKIM | IP-only 模式不签名 | 正式域名支持 DKIM |
| 推荐用途 | 内网、开发、测试 | 正式服务 |

正式部署示例：

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --cert /root/fullchain.pem \
  --key /root/privkey.pem
```

## 参考资料

- [Chatmail：Hosting without DNS records](https://chatmail.at/doc/relay/iponly.html)
- [Chatmail Docker](https://github.com/chatmail/docker)
