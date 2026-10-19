# iOS 后台/锁屏通知与可选网关

邮件到达 Chatmail 后，Dovecot 通知 `chatmail-metadata`，后者把设备任务写入持久化 `pending_notifications`，再 POST 到 `https://notifications.delta.chat/notify`；官方服务通过 APNs 唤醒 iPhone，Delta Chat 再经 IMAP 拉取邮件。[Delta Chat 官方说明](https://delta.chat/en/help#instant-delivery)介绍了这条推送链路。

这里的“离线消息”指 App 不在前台、手机锁屏时的收信场景。手机完全断网时需要等待恢复联网；网关不提供 iOS 后台发送能力，也不保证通知在固定秒数内出现。

网关是可选的服务器出口优化，默认安装继续官方直连。它不能修复发送端 SMTP/联邦投递问题，也不能修复客户端到邮件服务器 IMAP/SMTP 的连接问题。

## 前置条件与范围

- 专用 Linux 邮件服务器，root、systemd、Docker Compose v2+、Python 3.9+、curl、OpenSSL 1.1.1+ 和系统 CA。
- 已运行且使用 host 网络的 Chatmail 容器，metadata 服务为 `chatmail-metadata.service`。
- 当前入口须是 `python /实际路径/chatmail-metadata SOCKET CONFIG`，并具备持久化任务、重载、重试及每线程 Session 接口。脚本先核对实际安装包，不支持的入口/版本会拒绝启用。
- 队列必须位于可写的持久化 bind mount 或 Docker volume。脚本从 Chatmail 配置和容器挂载确定实际路径，不根据域名拼接猜测。
- 网关通过独立 Compose 项目运行，不覆盖主项目 override，不重建主邮件容器。已有手工网关或其他 metadata 启动覆盖需要先审计、按原方案停用；脚本不会自动覆盖它们。

主镜像中的 metadata/notifier 模块保持原样。适配器用当前安装的 `metadata.main()`，只设置通知 URL，并对可能包含令牌的旧版 metadata 日志脱敏；上游多设备、持久化队列、线程 Session、顺序调度、退避和 HTTP 410 失效令牌处理仍由原安装包负责。优先队列和并发线程不保证 APNs 的全局通知顺序。

## 新装

在完整仓库目录执行正常安装，增加可选参数：

```bash
sudo ./install.sh \
  --domain chat.example.org \
  --cert-zip /tmp/example_nginx.zip \
  --notification-gateway
```

可通过 `--notification-gateway-image nginx:stable-alpine` 指定网关镜像；默认遵循安装器 DaoCloud 选择，启用时将实际拉取结果固定为 digest。`--no-daocloud` 使用官方 Nginx 镜像。网关应使用包含 nginx、wget、UID 101 的兼容镜像。

`--no-start` 仅准备邮件部署，不启动网关或 timer。之后先手动启动 Chatmail，待 metadata 可用，再执行下一节的独立启用命令。Mock/IP-only 安装也可转发这些网关参数，但仍需要服务器和手机能够访问互联网、客户端能够经 IMAP 访问邮件服务器。

## 已有部署

先取得实际工作目录，不要直接套用示例路径。以下 `/srv/my-chatmail` 是需要替换的示例；命令从本项目完整 checkout 执行：

```bash
CHATMAIL_ROOT=/srv/my-chatmail
sudo python3 notification_gateway.py --root "$CHATMAIL_ROOT" audit
```

审计读取 Compose、当前容器和 systemd 进程，显示实际容器、镜像 ID/Relay revision、metadata 安装路径/包版本、域名、队列目录及数量/最老年龄、Postfix/Dovecot/metadata 状态、系统 DNS A 记录和经过加密 DNS/TLS/GET 405 验证的端点。它不会读出令牌或修改服务。

如果有多个 metadata 服务，通过 `--service 实际Compose服务名` 明确选择。系统 DNS 与 DoH 结果不同不等于任一地址可信，候选地址仍必须通过系统 CA、官方主机名和端点验证。

```bash
sudo python3 notification_gateway.py \
  --root "$CHATMAIL_ROOT" \
  --service mail-relay \
  enable
```

`mail-relay` 是示例服务名，应使用审计发现的值；只有一个服务时可省略 `--service`。默认端口为 10089；确有本机端口冲突时可显式指定 `--port 其他未占用端口`。网关只绑定 `127.0.0.1`，无需开放防火墙或云安全组端口。

启用流程为：审计和兼容检查 → 加密 DNS 与严格 TLS 验证 → 备份原服务/Compose → 创建独立网关 → `nginx -t` 和端点验证 → 安装专用 metadata drop-in/启动适配器 → 重启 metadata → 启用维护单元。失败会恢复原入口并保留队列；若恢复本身失败，保留网关供人工核查，重新运行 `disable` 完成恢复。

备份和状态保存在 `CHATMAIL_ROOT/notification-gateway/`，仅 root 可访问；其中只有 `nginx/` 子目录以只读目录方式挂给网关。Nginx 采用非 root、只读根文件系统、tmpfs、移除 capabilities、`no-new-privileges`、`unless-stopped`、健康检查和 10 MB × 3 日志轮转。

## SNI 兼容例外

默认始终开启 `proxy_ssl_server_name on`，保留系统 CA、`proxy_ssl_verify on` 和 `proxy_ssl_name notifications.delta.chat`。这些选项的职责见 [Nginx 官方文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ssl_name)。

只有标准路径失败时，才考虑显式允许兼容测试：

```bash
sudo python3 notification_gateway.py \
  --root "$CHATMAIL_ROOT" \
  --allow-sni-workaround \
  enable
```

该参数不能强制关闭 SNI。脚本必须对同一个 DoH 候选 IP 进行三组对照：三次带官方 SNI 的连接均明确被重置，且至少两次无 SNI 连接完成证书链/主机名验证并返回 GET 405，才能采用无 SNI。超时、DNS 失败或任意 HTTP 错误不构成证明。证据保存在 root-only 配置中。

无 SNI 模式通过 OpenSSL 的 `-verify_hostname`、`-verify_return_error` 和系统 CA 继续验证官方身份；不会使用 `curl -k`、`proxy_ssl_verify off` 或过时的 Python `ssl.match_hostname`。网络环境变化后应停用、重新审计并尝试默认 SNI。

## 服务与验证

启用后，管理脚本和适配器会复制到实际安装目录，可脱离原 checkout 执行日常维护：

```bash
NOTIFY_MANAGER="$CHATMAIL_ROOT/notification-gateway/notification_gateway.py"
sudo python3 "$NOTIFY_MANAGER" --root "$CHATMAIL_ROOT" status
sudo python3 "$NOTIFY_MANAGER" --root "$CHATMAIL_ROOT" health
sudo docker compose -f "$CHATMAIL_ROOT/notification-gateway/compose.json" ps
sudo docker compose -f "$CHATMAIL_ROOT/notification-gateway/compose.json" logs --since 30m gateway
systemctl list-timers --all --no-pager 'chatmail-notification-*'
```

`status` 输出实际 gateway/container/unit 名字，单元名带安装目录标识，避免不同项目相互覆盖。正常结果为邮件和网关 running/healthy、邮件服务 active、适配器存在、上游可用、队列没有持续变老。`health` 通过 JSON 和退出码报告状态；服务异常、上游不可用或队列过旧时返回非零，timer 下一分钟会再次检查。

默认端口的端点探测如下，自定义端口时相应替换：

```bash
curl -fsS --max-time 3 http://127.0.0.1:10089/healthz
curl -sS --max-time 12 -o /dev/null \
  -w 'code=%{http_code} connect=%{time_connect} total=%{time_total}\n' \
  http://127.0.0.1:10089/notify
```

`/healthz` 的 200 仅证明本机网关运行；GET `/notify` 的 **405 正常**，仅证明官方端点可连通且不支持 GET。必须观察真实任务的 POST 200、队列消退和手机实测，才能逐段验证通知链路。成功 POST 的耗时不包含之前的排队/退避，也不等于 APNs 或手机显示耗时。

网关日志只记录时间、方法、状态和耗时；拒绝 query string，不记录请求正文、设备令牌、账号密码或邮件内容。旧 metadata 的历史日志可能已有敏感信息，不要把原始日志/备份粘贴到公开 issue。

## 自动维护

- 每小时通过 HTTPS DoH 获取官方 A 记录，逐个验证 CA、主机名及 GET 405。没有有效候选时保持当前配置，不永久手填官方 IP。
- 地址变化时原子替换目录内 upstream，`nginx -t` 后热重载，再探测回环出口；任一步失败恢复之前的配置。持久化的事务标记使进程中断后可在下一次刷新中先恢复。
- upstream 使用 HTTP/1.1、空 Connection、keepalive 8、空闲 90 秒、每连接 1000 请求及 TLS session reuse；metadata 保留每线程 Session。
- 每分钟检查主容器、网关、邮件服务、适配器、上游和队列年龄。同一组件连续三次失败才恢复，恢复动作前保存时间，冷却至少十分钟；容器近期启动或无法确认启动时间时不主动重启。
- 上游网络失败或队列积压本身不会重启整个邮件容器。上游恢复而任务仍老于 300 秒时，可在门槛和冷却限制下重启 metadata，重新加载持久化任务。
- 开机 apply 单元和检查会重新安装本组件的适配器；镜像、配置身份、队列挂载或其他 systemd 覆盖改变时拒绝自动套用，要求重新审计。Docker/metadata 尚未就绪时，后续健康检查会按门槛重试。

维护前先暂停实际输出的 timer。可从 root-only 配置中只提取单元标识，不输出其他配置：

```bash
NOTIFY_UNIT=$(sudo python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["unit"])' \
  "$CHATMAIL_ROOT/notification-gateway/config.json")
sudo systemctl stop "$NOTIFY_UNIT-health.timer" "$NOTIFY_UNIT-refresh.timer"
# 完成维护后：
sudo systemctl start "$NOTIFY_UNIT-health.timer" "$NOTIFY_UNIT-refresh.timer"
```

不要删除 `pending_notifications` 来清积压。任务超时/失效的原上游语义保留，已经超时的通知不能等同于邮件丢失。

## 更新与回滚

更新邮件镜像、Chatmail 配置或 systemd 覆盖前，先停止本组件并恢复原始入口：

```bash
sudo python3 "$NOTIFY_MANAGER" --root "$CHATMAIL_ROOT" disable
```

这会停用/移除本组件的维护单元、移除专用 drop-in、重启 metadata 恢复原入口，并停止网关；不恢复旧邮件快照、不删除/覆盖当前通知队列，不更改其他用户 drop-in。备份会保留。重复停用不会再次重启已恢复的 metadata。

再备份整个持久化邮件、令牌/通知队列、DKIM、证书及实际 Compose/Chatmail 配置，并核对是否有尚未持久化的 `/var/spool/postfix` 在途任务。不能用直接重建容器的方法忽略这些任务。按原部署方式更新后，重新从完整 checkout 运行 `audit` 和 `enable`，重新核对安装接口并固定新的网关镜像 digest。

如果是在镜像已改变之后才运行 `disable`，或本组件的 drop-in 被再次编辑，安全检查可能拒绝自动恢复；应先比对保存的原始服务和当前配置，人工合并。不要通过删除数据、关闭证书校验或强制重建绕过检查。

## iPhone 锁屏实测与归因

1. iPhone 允许 Delta Chat 系统通知，排除专注模式与通知摘要；App 不在前台并锁屏。强制结束 App 应单独记录，不能与普通后台测试混用。
2. 由另一设备发送新消息，记录发送时间、服务器邮件落地时间、真实通知 POST 完成时间和 iPhone 系统通知出现时间，全部带时区。
3. 同时观察持久化任务数量/最老年龄和网关 POST 状态，不输出真实令牌、密码或邮件内容。

| 现象 | 优先排查 |
|---|---|
| 邮件未落地 | SMTP/联邦投递 |
| 邮件落地但无通知任务 | 设备令牌登记、Dovecot hook、metadata |
| 任务持续积压 | 中继到官方端点的网络/TLS/HTTP |
| 官方 POST 快速成功但手机慢 | APNs、iOS 调度、手机网络、客户端状态 |
| 打开 App 才收到 | IMAP 拉取可用，但推送唤醒仍需核查 |

仓库自动化测试覆盖隔离的启用/停用、接口适配、证书/SNI 条件、配置回滚、日志脱敏及恢复门槛，不能代替真实容器部署和 iPhone 锁屏测试。服务器测得 POST 成功也不代表 APNs 或手机已显示通知。
