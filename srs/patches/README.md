# 聊天室 SRS 媒体补丁

媒体源码目录 `trunk/` 由构建环境提供。当前补丁基于上游 SRS
[`v6.0-d6`](https://github.com/ossrs/srs/releases/tag/v6.0-d6)（6.0.134）。

- `0001` 保留 RTMP 流名中的点号。HTTP-FLV 请求只去掉末尾的媒体后缀，失败的挂载也不会留下无效的流记录。
- `0002` 回移[上游退出崩溃修复](https://github.com/ossrs/srs/commit/da5683e4784f4acf0e6eee8dce91fa52a7e770b4)，
  避免活跃推流还在清理时提前销毁全局对象。

本目录所在项目的 Dockerfile 在编译前自动应用这两份补丁。Linux 本机构建时，从 `srs/` 运行以下命令，
并按部署环境传入原有的 `configure` 参数。

```bash
bash ./apply-media-patches.sh && (cd trunk && ./configure && make -j2)
```

重复执行会识别已应用的补丁。更换上游版本或补丁无法干净应用时，构建会停止。更新 SRS 时先核对上游是否已合入相应修复，
更新此处的补丁和基线，再用 Git、Python 3 和支持 AAC 的 FFmpeg 验证新二进制。

```bash
python3 tests/srs_media_regression.py --srs trunk/objs/srs
```

测试在回环地址启动临时 SRS。它验证同一房间的两路带点号流能分别解码出各自的音频，
以及仍有推流和播放连接时收到 `SIGTERM` 能以状态码 0 退出，重启后可重新推流和播放。
