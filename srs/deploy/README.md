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
