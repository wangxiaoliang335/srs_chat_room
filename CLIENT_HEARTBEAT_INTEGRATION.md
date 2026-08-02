# 客户端修改说明：翻译心跳机制

> 适用版本：服务端的 SRS / FastAPI 已经更新到 2026-06-15 版本
> 影响范围：申请翻译 / 听翻译的客户端
> 修改人：客户端

---

## 一、为什么要改

服务端发现一个 bug：**客户端关浏览器/断网/杀进程后，翻译子进程不会自动停止**，会一直占用资源、连百度 API、推空流到 SRS。

服务端已经做了 4 个修复：
- BUG1：翻译主进程内部清理（os._exit 强退）
- BUG2：重复申请翻译时先停旧的
- BUG3：调 `/stop` 接口会真杀子进程
- **BUG4：申请翻译的客户端必须发心跳，30 秒没心跳 → 服务端主动停翻译**

**客户端必须配合 BUG4 实现心跳，否则翻译仍会变成孤儿。**

---

## 二、改动清单

| 改动点 | 优先级 | 工作量 |
|---|---|---|
| 1. 申请翻译成功后启动心跳定时器 | **必须** | 10 行 |
| 2. 听翻译的玩家也建议加（用于 listen 端清理，但当前是申请端判断） | 可选 | 5 行 |
| 3. 关闭翻译/页面 unload 时清理定时器 + 主动调 `/stop` | **必须** | 5 行 |
| 4. client_id 唯一性 | **必须** | 1 行 |

---

## 三、新增 HTTP 接口

```
POST /api/v1/translation/heartbeat
Content-Type: application/json

{
  "room_id":     "room_1781448276492",
  "source_user": "22",
  "client_id":   "tab_abc123",        // 必填：当前 tab/会话唯一标识
  "to_lang":     "en",                // 建议传：服务端用 room+source+to_lang 精确定位
  "request_id":  "trans_bbec5cb12eca" // 可选：服务端 /start 返回的 id，如果有就传
}
```

返回：
```json
{"status":"ok","updated":true}    // 成功
{"status":"ok","updated":false}   // 找不到对应 request（仍 200，不要报错）
```

**请求频率：每 5 秒一次**（服务端超时 30 秒，留足网络抖动余量）

---

## 四、推荐代码（以浏览器 JS 为例）

### 4.1 生成稳定的 client_id（页面加载时一次）

```javascript
// 用 sessionId + tabId 保证同一 tab 多次刷新 client_id 不变
// 不同 tab/client 必须不同
function getClientId() {
  const key = "translation_client_id";
  let cid = sessionStorage.getItem(key);
  if (!cid) {
    // 用 crypto.randomUUID 或自造，避免重复
    cid = "cli_" + (crypto.randomUUID?.() || Date.now() + "_" + Math.random().toString(36).slice(2));
    sessionStorage.setItem(key, cid);
  }
  return cid;
}
```

> **多 tab 共享一个 client_id 也可以**（服务端用 `(room_id, source_user, to_lang)` 维度判断），但建议每 tab 一份，便于精细化追踪。

### 4.2 启动翻译后开启心跳

```javascript
let translationHeartbeatTimer = null;
let currentTranslation = null;   // 保存 /start 返回的 {request_id, room_id, source_user, to_lang}

async function startTranslation({roomId, sourceUser, targetUser, toLang, sourceLang}) {
  const res = await fetch("/api/v1/translation/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      room_id: roomId,
      source_user: sourceUser,
      target_user: targetUser,
      to_lang: toLang,
      source_lang: sourceLang || "auto"
    })
  });
  const data = await res.json();
  if (data.status !== "ok") throw new Error("start translation failed");

  currentTranslation = {
    request_id: data.request_id,
    room_id: roomId,
    source_user: sourceUser,
    to_lang: toLang
  };

  // 启动心跳：每 5 秒一次
  startHeartbeat();
}

function startHeartbeat() {
  if (translationHeartbeatTimer) return;  // 已经在跑
  const tick = async () => {
    if (!currentTranslation) return;
    try {
      await fetch("/api/v1/translation/heartbeat", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          room_id: currentTranslation.room_id,
          source_user: currentTranslation.source_user,
          to_lang: currentTranslation.to_lang,
          request_id: currentTranslation.request_id,
          client_id: getClientId()
        })
      });
    } catch (e) {
      // 心跳失败不要紧，不要停止定时器
      console.warn("[translation] heartbeat failed:", e);
    }
  };
  // 立即打一次，再每 5 秒
  tick();
  translationHeartbeatTimer = setInterval(tick, 5000);
}
```

### 4.3 停止翻译时清理

```javascript
async function stopTranslation() {
  // 1. 停心跳
  if (translationHeartbeatTimer) {
    clearInterval(translationHeartbeatTimer);
    translationHeartbeatTimer = null;
  }
  // 2. 主动调 /stop（推荐，不要完全依赖服务端心跳兜底）
  if (currentTranslation?.request_id) {
    try {
      await fetch("/api/v1/translation/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({request_id: currentTranslation.request_id})
      });
    } catch (e) {
      console.warn("[translation] stop request failed:", e);
    }
  }
  currentTranslation = null;
}
```

### 4.4 页面关闭 / 切走时兜底

```javascript
// 用户关 tab / 刷页面 / 关浏览器时
window.addEventListener("beforeunload", () => {
  // 同步请求：浏览器允许少量时间（受浏览器策略限制，可能不是 100% 到达服务端，但能到最好）
  if (currentTranslation?.request_id) {
    const url = "/api/v1/translation/stop";
    const body = JSON.stringify({request_id: currentTranslation.request_id});
    navigator.sendBeacon?.(url, new Blob([body], {type: "application/json"}));
  }
  // 即使 sendBeacon 失败，服务端 30 秒心跳超时也会兜底清理
});

// 切到后台时降频（可选）
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    // 可以继续发，5s 一次即可；或者停掉等回前台再发
    // 建议：保持发送（请求很轻，服务端 30s 才超时）
  }
});
```

---

## 五、QA 验证步骤

修改后，QA 同学请做以下验证：

1. **正常申请翻译**
   - 听得到翻译
   - 服务端日志 `translation_fastapi.log` 每 5 秒看到 `[API] Heartbeat ignored` 或 `update_requester_heartbeat`

2. **关浏览器**（X 掉 tab）
   - 30 秒后，服务端日志出现 `[TranslationManager] Stopped translation: xxx, reason=requester_heartbeat_timeout`
   - 服务端进程：`ps -ef | grep audio_translation_service_websocket` 看不到新翻译子进程
   - SRS 流 `translation_XX_en` 在 1 分钟内被自动清理

3. **断网测试**
   - 客户端断网 → 5 秒后心跳失败但定时器继续
   - 30 秒后服务端认为客户端离线 → 主动停翻译
   - 网络恢复后客户端发翻译请求 → 重新启动翻译

4. **重复申请**
   - 用户 A 申请翻译（不发心跳） → 等 30 秒
   - 用户 A 再申请 → 服务端日志应看到 `Killing stale translation before new start`
   - 旧的翻译子进程被 SIGTERM，新子进程被启动

5. **调 /stop**
   - 翻译中客户端调 /stop → 服务端日志 `Translation Sent SIGTERM to PID xxx`
   - 子进程 1 秒内退出

---

## 六、注意事项

1. **client_id 必须稳定**：建议存 `sessionStorage`，这样同一 tab 刷新 client_id 不变，刷新期间心跳不丢。
2. **request_id 建议存**：服务端 /start 返回的 `request_id`，传过去能更精准定位。
3. **不要在心跳里塞业务逻辑**：心跳频率高（每 5s），不要做任何同步操作。
4. **失败不要紧**：心跳失败（网络抖动、服务端重启）一律忽略，定时器继续跑。30s 超时是给网络抖动的余量。
5. **不需要每个端都发**：**只有"申请翻译"的那一方发心跳**就行。听翻译的玩家（puller）走的是另一套机制（pullers 心跳，15s 超时）。

---

## 七、FAQ

**Q1: 之前没发心跳也能用，为什么现在必须？**
A: 之前服务端没有兜底，客户端关浏览器翻译永远不关。修复后服务端会主动停，没心跳就被认为是"客户端死了"。

**Q2: 多个 tab 申请同一个 source_user 的翻译会怎样？**
A: 服务端 `get_request_by_source(room, user, to_lang)` 会找到已有的 request，**复用同一个翻译**。多个 tab 共用 client_id 也是 OK 的。
如果想做"任一 tab 关了就停"，那每个 tab 各自 client_id，最后一个关 → 30s 后停。

**Q3: 同一 tab 内 5 秒一次会不会太频繁？**
A: 不会。心跳 body 几十字节，POST 请求 < 1KB。服务端 30s 超时 → 即使偶尔丢几个心跳也安全。

**Q4: 听翻译的那一端需要改吗？**
A: 短期不需要。**只有"申请翻译"的那一方**要发心跳。后续服务端会加 puller 自动清理。
