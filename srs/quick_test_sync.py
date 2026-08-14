"""
quick_test_sync.py — 主服务器 WebSocket 同步接口快速测试

用法:
    python quick_test_sync.py [WS_URL] [SECRET]

示例:
    python quick_test_sync.py ws://8.138.45.176:9005/sync changeme_change_this_secret_before_production
"""

import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime

import websockets
from websockets.asyncio.client import connect


def sign_payload(secret: str, ts: int, data: dict) -> str:
    raw = f"{ts}.{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
    return hmac.new(
        secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def print_ok(msg):
    print(f"\033[92m✓ {msg}\033[0m")


def print_fail(msg):
    print(f"\033[91m✗ {msg}\033[0m")


def print_info(msg):
    print(f"\033[94mℹ {msg}\033[0m")


def print_section(title):
    print(f"\n{'='*60}")
    print(f"{title}")
    print('='*60)


async def test_connection(ws_url: str, secret: str, server_id: str):
    """测试 WebSocket 连接"""
    print_section("测试 1: WebSocket 连接与认证")

    try:
        print_info(f"连接 {ws_url}...")
        ws = await connect(ws_url, open_timeout=10.0)
        print_ok("WebSocket 连接成功")
    except Exception as e:
        print_fail(f"连接失败: {e}")
        return None

    # 认证
    ts = int(time.time())
    data_body = {"server_id": server_id, "ts": ts}
    sig = sign_payload(secret, ts, data_body)

    auth_msg = {
        "type": "auth",
        "server_id": server_id,
        "timestamp": ts,
        "data": {
            "server_id": server_id,
            "ts": ts,
            "signature": sig,
        },
    }

    print_info("发送认证消息...")
    await ws.send(json.dumps(auth_msg, separators=(",", ":")))

    try:
        resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
        ack = json.loads(resp)

        if ack.get("type") == "auth_ack" and ack.get("data", {}).get("ok"):
            print_ok("认证成功")
            return ws
        else:
            print_fail(f"认证失败: {ack}")
            await ws.close()
            return None
    except asyncio.TimeoutError:
        print_fail("认证超时")
        await ws.close()
        return None
    except Exception as e:
        print_fail(f"认证异常: {e}")
        await ws.close()
        return None


async def test_heartbeat(ws):
    """测试心跳机制"""
    print_section("测试 2: 心跳机制")

    print_info("等待服务器 ping (最多 20 秒)...")

    try:
        ping = await asyncio.wait_for(ws.recv(), timeout=20.0)
        ping_msg = json.loads(ping)

        if ping_msg.get("type") == "ping":
            print_ok(f"收到 ping: timestamp={ping_msg.get('timestamp')}")

            # 回复 pong
            pong = {"type": "pong", "timestamp": int(time.time())}
            await ws.send(json.dumps(pong, separators=(",", ":")))
            print_ok("已回复 pong")
        else:
            print_info(f"收到其他消息: {ping_msg.get('type')}")
    except asyncio.TimeoutError:
        print_fail("未在 20 秒内收到 ping")
        return False

    return True


async def test_send_messages(ws, server_id: str):
    """测试发送各种业务消息"""
    print_section("测试 3: 发送业务消息")

    room_id = f"quick_test_{int(time.time())}"

    # 消息模板
    messages = [
        {
            "name": "room_created",
            "msg": {
                "type": "room_created",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "room_id": room_id,
                    "owner_id": "test_user",
                    "name": "快速测试房间",
                    "max_members": 100,
                },
            },
        },
        {
            "name": "member_joined",
            "msg": {
                "type": "member_joined",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "user_id": "user_test",
                    "role": "member",
                    "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "last_active": "",
                },
            },
        },
        {
            "name": "member_role_changed",
            "msg": {
                "type": "member_role_changed",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "user_id": "user_test",
                    "old_role": "member",
                    "new_role": "admin",
                    "operator_id": "test_user",
                },
            },
        },
        {
            "name": "room_mute_changed",
            "msg": {
                "type": "room_mute_changed",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "room_id": room_id,
                    "allow_speak": False,
                    "operator_id": "test_user",
                },
            },
        },
        {
            "name": "member_left",
            "msg": {
                "type": "member_left",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "user_id": "user_test",
                    "reason": "left",
                },
            },
        },
        {
            "name": "room_deleted",
            "msg": {
                "type": "room_deleted",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": room_id,
                "data": {
                    "room_id": room_id,
                    "deleted_by": "test_user",
                },
            },
        },
        {
            "name": "rooms_sync",
            "msg": {
                "type": "rooms_sync",
                "server_id": server_id,
                "timestamp": int(time.time()),
                "room_id": "",
                "data": {
                    "rooms": [
                        {
                            "room_id": f"sync_room_{int(time.time())}",
                            "owner_id": "test_owner",
                            "name": "同步测试房间",
                            "member_count": 1,
                            "members": [
                                {
                                    "user_id": "test_owner",
                                    "role": "owner",
                                    "status": "normal",
                                    "publish_allowed": True,
                                    "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                }
                            ],
                            "allow_speak": True,
                        }
                    ]
                },
            },
        },
    ]

    for item in messages:
        try:
            await ws.send(json.dumps(item["msg"], separators=(",", ":")))
            print_ok(f"发送 {item['name']}")
            await asyncio.sleep(0.3)  # 稍作延迟
        except Exception as e:
            print_fail(f"发送 {item['name']} 失败: {e}")

    return True


async def test_wrong_signature(ws_url: str, server_id: str):
    """测试错误签名认证"""
    print_section("测试 4: 错误签名认证")

    print_info("测试错误签名...")

    try:
        ws = await connect(ws_url, open_timeout=10.0)

        ts = int(time.time())
        wrong_msg = {
            "type": "auth",
            "server_id": server_id,
            "timestamp": ts,
            "data": {
                "server_id": server_id,
                "ts": ts,
                "signature": "0000000000000000000000000000000000000000000000000000000000000000",
            },
        }

        await ws.send(json.dumps(wrong_msg, separators=(",", ":")))
        resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
        ack = json.loads(resp)

        if ack.get("data", {}).get("ok") == False:
            print_ok(f"错误签名被拒绝: {ack.get('data', {}).get('reason')}")
        else:
            print_fail(f"错误签名未被拒绝: {ack}")

        await ws.close()
    except Exception as e:
        print_fail(f"测试异常: {e}")


async def test_unauthenticated_message(ws_url: str, server_id: str):
    """测试未认证发送业务消息"""
    print_section("测试 5: 未认证发送业务消息")

    print_info("测试未认证消息...")

    try:
        ws = await connect(ws_url, open_timeout=10.0)

        # 直接发送业务消息
        msg = {
            "type": "room_created",
            "server_id": server_id,
            "timestamp": int(time.time()),
            "room_id": "test",
            "data": {"room_id": "test", "owner_id": "test", "name": "test"},
        }

        await ws.send(json.dumps(msg, separators=(",", ":")))

        try:
            resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
            ack = json.loads(resp)

            if ack.get("type") == "error" or ack.get("data", {}).get("reason") == "not authenticated":
                print_ok(f"未认证消息被拒绝")
            else:
                print_info(f"收到响应: {ack}")
        except asyncio.TimeoutError:
            print_info("未收到响应（可能服务器未实现此检查）")

        await ws.close()
    except Exception as e:
        print_fail(f"测试异常: {e}")


async def main():
    # 参数解析
    ws_url = sys.argv[1] if len(sys.argv) > 1 else "ws://8.138.45.176:9005/sync"
    secret = sys.argv[2] if len(sys.argv) > 2 else "changeme_change_this_secret_before_production"
    server_id = sys.argv[3] if len(sys.argv) > 3 else "server_chat_001"

    print(f"\n{'='*60}")
    print("主服务器 WebSocket 同步接口 - 快速测试")
    print('='*60)
    print(f"服务器: {ws_url}")
    print(f"密钥: {secret[:8]}...")
    print(f"服务器ID: {server_id}")

    # 测试 1: 连接与认证
    ws = await test_connection(ws_url, secret, server_id)
    if not ws:
        print_fail("连接失败，退出测试")
        return 1

    # 测试 2: 心跳
    await test_heartbeat(ws)

    # 测试 3: 发送业务消息
    await test_send_messages(ws, server_id)

    # 关闭连接后测试错误场景
    await ws.close()

    # 测试 4: 错误签名
    await test_wrong_signature(ws_url, server_id)

    # 测试 5: 未认证消息
    await test_unauthenticated_message(ws_url, server_id)

    print_section("测试完成")
    print_ok("所有测试完成")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
