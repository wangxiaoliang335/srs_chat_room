#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉流心跳客户端示例
模拟拉流客户端定期发送心跳
用于测试翻译服务容错机制
"""

import requests
import time
import threading
import argparse
import sys
from typing import Optional

# 服务器地址
SERVER_HOST = "localhost"
SERVER_PORT = 8085
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


class PullerHeartbeatClient:
    """拉流心跳客户端"""
    
    def __init__(self, request_id: str, puller_id: str, server_url: str = SERVER_URL):
        self.request_id = request_id
        self.puller_id = puller_id
        self.server_url = server_url.rstrip('/')
        self.heartbeat_interval = 5  # 心跳间隔（秒）
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.source_stream_active = True  # 模拟源流状态检测
    
    def register(self) -> bool:
        """注册拉流者"""
        try:
            response = requests.post(
                f"{self.server_url}/api/v1/translation/register_puller",
                json={
                    "request_id": self.request_id,
                    "puller_id": self.puller_id
                },
                timeout=5
            )
            if response.status_code == 200:
                print(f"✓ 注册成功: request_id={self.request_id}, puller_id={self.puller_id}")
                return True
            else:
                print(f"✗ 注册失败: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"✗ 注册异常: {e}")
            return False
    
    def unregister(self) -> bool:
        """注销拉流者"""
        try:
            response = requests.post(
                f"{self.server_url}/api/v1/translation/unregister_puller",
                json={
                    "request_id": self.request_id,
                    "puller_id": self.puller_id
                },
                timeout=5
            )
            if response.status_code == 200:
                print(f"✓ 注销成功")
                return True
            else:
                print(f"✗ 注销失败: {response.status_code}")
                return False
        except Exception as e:
            print(f"✗ 注销异常: {e}")
            return False
    
    def send_heartbeat(self) -> bool:
        """发送心跳"""
        try:
            response = requests.post(
                f"{self.server_url}/api/v1/translation/heartbeat",
                json={
                    "request_id": self.request_id,
                    "puller_id": self.puller_id,
                    "source_stream_active": self.source_stream_active
                },
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 0:
                    return True
            return False
        except Exception as e:
            print(f"  心跳异常: {e}")
            return False
    
    def heartbeat_loop(self):
        """心跳循环"""
        print(f"[心跳] 启动: 每 {self.heartbeat_interval} 秒发送一次")
        
        while self.running:
            if self.send_heartbeat():
                print(f"[心跳] ✓ 发送成功 (puller={self.puller_id})")
            else:
                print(f"[心跳] ✗ 发送失败")
            
            # 等待下一个心跳周期
            for _ in range(self.heartbeat_interval):
                if not self.running:
                    break
                time.sleep(1)
        
        print(f"[心跳] 已停止")
    
    def start(self, auto_register: bool = True):
        """启动心跳客户端"""
        if self.running:
            print("客户端已在运行中")
            return
        
        # 先注册
        if auto_register:
            if not self.register():
                print("注册失败，无法启动心跳客户端")
                return
        
        self.running = True
        self.thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.thread.start()
        print(f"心跳客户端已启动: request_id={self.request_id}, puller_id={self.puller_id}")
    
    def stop(self, auto_unregister: bool = True):
        """停止心跳客户端"""
        if not self.running:
            return
        
        self.running = False
        
        if self.thread:
            self.thread.join(timeout=2)
        
        # 注销
        if auto_unregister:
            self.unregister()
        
        print("心跳客户端已停止")
    
    def get_puller_status(self) -> dict:
        """获取拉流者状态"""
        try:
            response = requests.get(
                f"{self.server_url}/api/v1/translation/requests/{self.request_id}/pullers",
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
            return {}
        except Exception as e:
            print(f"获取状态异常: {e}")
            return {}


def demo_interactive():
    """交互式演示"""
    print("=" * 60)
    print("拉流心跳客户端 - 交互式演示")
    print("=" * 60)
    
    # 获取输入参数
    request_id = input("请输入翻译请求ID (request_id): ").strip()
    if not request_id:
        request_id = "test_request_001"
        print(f"使用默认 request_id: {request_id}")
    
    puller_id = input("请输入拉流者ID (puller_id): ").strip()
    if not puller_id:
        puller_id = "test_puller_001"
        print(f"使用默认 puller_id: {puller_id}")
    
    server_url = input(f"请输入服务器地址 (默认: {SERVER_URL}): ").strip()
    if not server_url:
        server_url = SERVER_URL
    
    # 创建客户端
    client = PullerHeartbeatClient(request_id, puller_id, server_url)
    
    print("\n可用命令:")
    print("  start   - 启动心跳")
    print("  stop    - 停止心跳")
    print("  status  - 查看状态")
    print("  quit    - 退出程序")
    print()
    
    while True:
        try:
            cmd = input("> ").strip().lower()
            
            if cmd == "start":
                client.start()
            elif cmd == "stop":
                client.stop()
            elif cmd == "status":
                status = client.get_puller_status()
                print(f"状态: {status}")
            elif cmd == "quit" or cmd == "exit" or cmd == "q":
                client.stop()
                break
            elif cmd == "help":
                print("  start   - 启动心跳")
                print("  stop    - 停止心跳")
                print("  status  - 查看状态")
                print("  quit    - 退出程序")
            else:
                print(f"未知命令: {cmd}")
        
        except KeyboardInterrupt:
            print("\n")
            client.stop()
            break


def demo_auto():
    """自动演示"""
    print("=" * 60)
    print("拉流心跳客户端 - 自动演示")
    print("=" * 60)
    
    # 测试参数
    request_id = "test_request_001"
    puller_id = "test_puller_001"
    
    print(f"\n测试参数:")
    print(f"  request_id: {request_id}")
    print(f"  puller_id: {puller_id}")
    print(f"  server_url: {SERVER_URL}")
    
    # 创建并启动客户端
    client = PullerHeartbeatClient(request_id, puller_id, SERVER_URL)
    
    print("\n启动心跳客户端...")
    client.start()
    
    # 运行30秒
    print("\n运行30秒，观察心跳日志...")
    for i in range(30, 0, -5):
        print(f"  剩余 {i} 秒...")
        time.sleep(5)
        
        # 显示状态
        status = client.get_puller_status()
        if status.get('data', {}).get('pullers'):
            for p in status['data']['pullers']:
                print(f"    拉流者状态: id={p['puller_id']}, {p['seconds_ago']}秒前, 存活={p['is_alive']}")
    
    # 停止客户端
    print("\n停止心跳客户端...")
    client.stop()
    
    print("\n演示完成!")


def main():
    parser = argparse.ArgumentParser(description="拉流心跳客户端")
    parser.add_argument('--request-id', '-r', type=str, help='翻译请求ID')
    parser.add_argument('--puller-id', '-p', type=str, help='拉流者ID')
    parser.add_argument('--server-url', '-s', type=str, default=SERVER_URL, help='服务器地址')
    parser.add_argument('--interval', '-i', type=int, default=5, help='心跳间隔（秒）')
    parser.add_argument('--auto', '-a', action='store_true', help='自动演示模式')
    parser.add_argument('--demo', '-d', action='store_true', help='演示模式（运行30秒后自动停止）')
    
    args = parser.parse_args()
    
    if args.demo:
        demo_auto()
        return
    
    if args.request_id and args.puller_id:
        # 命令行指定参数模式
        client = PullerHeartbeatClient(args.request_id, args.puller_id, args.server_url)
        client.heartbeat_interval = args.interval
        
        print(f"启动心跳客户端: request_id={args.request_id}, puller_id={args.puller_id}")
        print(f"服务器: {args.server_url}")
        print(f"心跳间隔: {args.interval} 秒")
        print(f"按 Ctrl+C 停止\n")
        
        try:
            client.start()
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n")
            client.stop()
    else:
        # 交互式模式
        demo_interactive()


if __name__ == "__main__":
    main()
