# 开发服聊天室部署配置

在现有 SRS Compose 配置后依次应用 `dev-chat.compose.yaml`、`dev-chat-public.compose.yaml`
和 `dev-chat-hardened.compose.yaml`。HTTPS 网关加载 `dev-chat-https.conf`，媒体代理使用 `dev-chat-media.conf`。

从 `dev-chat-srs.conf.template` 生成权限为 `0600` 的配置文件，由 `SRS_AUTH_CONFIG` 指定文件路径。
用实际 WebRTC 公网地址替换 `@@SRS_RTC_CANDIDATE@@`，用独立的 URL 安全随机值填入管理口令和回调密钥。
`CHAT_API_ENV_FILE` 同步配置 `SRS_HTTP_API_USERNAME=chat-media-control`、`SRS_HTTP_API_PASSWORD` 和
`SRS_MEDIA_HOOK_SECRET`。轮换时同步更新 SRS 配置和聊天室服务的环境文件。

用 `SRS_LOG_REDACTOR` 指定 `srs-log-redact.sh` 的路径。用 `CHAT_ANDROID_REGISTRY_DIR` 挂载受控的设备登记目录，
在聊天室服务设置 `CHAT_ANDROID_DEVICE_REGISTRY=/run/chat-android/devices.json` 与 `CHAT_ANDROID_PROOF_AUDIENCE`。
第三方应用登记通过聊天室服务的 `SRS_THIRD_PARTY_APPS` 配置。凭证和设备登记文件保存在服务器上。

媒体代理的开发环境容量与 `dev-chat-srs.conf.template` 的 `max_connections 100` 配套：

| 入口 | 同时连接上限 | 超限行为 |
| --- | --- | --- |
| RTMPS `1936` | 每个 TCP 来源 IP 16，全局 40 | 关闭新连接，不连接 SRS |
| HTTP-FLV | 全局 40 个进行中的请求 | 返回 HTTP 429，不连接 SRS |
| RTMPS TLS 握手 | 最长 5 秒 | 关闭未完成握手的连接 |

RTMPS 使用实际 TCP 对端地址计数；同一 NAT 出口的设备共享 16 条额度。代理没有启用 real-IP
或 PROXY protocol 信任。FLV 的 TCP 对端是 HTTPS 网关，因此只设置固定键的全局额度；客户端
更换 `Host`、`X-Forwarded-For` 或 `X-Real-IP` 不会获得新额度。额度释放后可立即接入新客户端。
未达容量时，无效 FLV 票据仍由聊天室鉴权接口拒绝并返回 403；容量耗尽时先返回 429。

两种媒体入口最多占用 SRS 的 80 条 TCP 连接，剩余 20 条供管理 API、撤销操作和短时 RTC
信令请求共同使用。RTC 音视频通过 UDP 传输，建立会话时的 HTTP 信令仍会暂时占用 TCP 额度；
这 20 条是容量余量，并非 SRS 中独立保留的管理配额。受信任网络内绕过代理的连接、内部服务
及突发信令也会使用它。调整媒体额度时须同时复核 SRS 总额度及这些控制请求的并发预算。

本地代理回归测试直接加载此 Nginx 配置，在独立的 Docker 内部网络运行临时后端，不开放主机
端口、不访问开发服。需要 Python 3.10+、Docker、OpenSSL，以及 `nginx:1.27-alpine` 和
`python:3.12-alpine` 镜像（缺失时 Docker 会拉取）：

```sh
python srs/tests/media_proxy_regression.py
```

Windows 如未把 OpenSSL 加入 PATH，可追加
`--openssl "C:/Program Files/Git/usr/bin/openssl.exe"`。测试验证真实来源 IP 额度、固定键全局额度、
两种媒体同时占满 80 条时的转发、超限不触达后端、关闭后的容量恢复、FLV 鉴权和 TLS 握手超时；
协议编解码与真实票据由 SRS / 聊天室对应测试覆盖。

更新已有开发实例使用 `rollout-dev-media.py`。在 `/opt/coupon-srs-dev/releases/<release>/` 准备
不可变的镜像、媒体配置和 JSON 发布计划；计划包括 `revision`、三服务当前 `expected_images`
镜像 ID、新 `images`（仅 `srs` 和 `chat-api`）、`proxy_config` 与 `proxy_sha256`。
两个镜像映射均只接受完整 `sha256:` ID；旧 Compose 标签也会核对其仍指向原镜像。
新镜像需带相同 Git revision 的 `org.opencontainers.image.revision` 标签。脚本需要宿主机
Python 3、PyYAML、Docker Compose，默认只预检；添加 `--apply` 才会切换。

```sh
python3 rollout-dev-media.py /opt/coupon-srs-dev/releases/<release>/plan.json
python3 rollout-dev-media.py /opt/coupon-srs-dev/releases/<release>/plan.json --apply
```

入口会锁定该开发实例、检查来源及配置摘要、验证 Nginx 配置，并连续三次确认没有媒体连接或
活跃发布流。查询失败不视为无流；等待超时不重启。配置、端口、密钥、数据卷保持原设置，
仅更新两个镜像和媒体代理配置路径。回滚运行中的 SRS 同样重新确认无流；查询失败不重启，
只有原生进程已经停止时才跳过其管理查询。发布失败恢复原 Compose 并验证旧服务健康，成功记录
`receipt.json`；`compose-before.yaml` 和旧镜像用于恢复，发布记录含私有配置，按 `0600` 保管。
