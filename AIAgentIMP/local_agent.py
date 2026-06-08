#!/usr/bin/env python3
"""
local_agent.py - 本地 Agent WebSocket 客户端

在每个用户的本地机器上运行，连接到中央 Slack Bot Relay 服务器。
ToolCall 在本地执行，结果通过 WebSocket 回传到 Relay → Slack。

使用方式：
  python local_agent.py

环境变量（在 .env 中配置）：
  SLACK_USER_ID    - 你的 Slack 用户 ID（如 U0123456789）
  WS_SERVER_URL    - Relay 服务器 WebSocket 地址（如 ws://192.168.1.100:8765）
"""

import os
import json
import base64
import asyncio
import threading
from time import sleep
from queue import Queue, Empty

from dotenv import load_dotenv
import websockets

load_dotenv()

# ── 配置 ────────────────────────────────────────────────────────────
SLACK_USER_ID = os.getenv("SLACK_USER_ID", "")
WS_SERVER_URL = os.getenv("WS_SERVER_URL", "ws://localhost:8765")
# 单帧最大字节数，需足够大以容纳图片 base64（默认 1MB 太小会断连）
WS_MAX_MESSAGE_SIZE = 32 * 1024 * 1024  # 32 MB

if not SLACK_USER_ID:
    print("错误: 请在 .env 中设置 SLACK_USER_ID（你的 Slack 用户 ID）")
    print("  获取方式: Slack 个人资料 → 更多 → 复制成员 ID")
    exit(1)

# ── 导入 AgentTeam 基础设施 ─────────────────────────────────────────
from AIAgentIMP import AgentTeamMain
from GlobalConfig import _TeammateManager, _AgentTeam_OutputPrint
from TeammateManager import TeammateManager
from ImageToolManager import save_data_url_image

# WebSocket 连接引用（全局）
_ws_connection = None
_ws_loop: asyncio.AbstractEventLoop = None
# 输出队列：AgentTeam → WebSocket
_output_forward_queue: Queue = Queue()


def _get_agent_names() -> list:
    """获取本地所有 Agent 名称列表。"""
    names = ["Leader"]
    try:
        names += _TeammateManager.member_names()
    except Exception:
        pass
    return names


def _output_monitor():
    """后台线程：消费 _AgentTeam_OutputPrint → 放入转发队列。"""
    while True:
        item = _AgentTeam_OutputPrint.get()
        _output_forward_queue.put(item)


def _route_message_to_agent(data: dict):
    """将 Relay 转发来的用户消息路由到本地 Agent。"""
    text = data.get("text", "")
    target_agent = data.get("target_agent", "Leader")
    user_id = data.get("user_id", "User")
    thread_ts = data.get("thread_ts", "")
    images = data.get("images", [])

    print(f"[local] 收到消息: target={target_agent} text={text[:80]!r} images={len(images)}张")

    # 保存 thread_ts 到全局，供输出回传时使用
    global _current_thread_ts
    _current_thread_ts = thread_ts

    # 入站图片落盘：把 base64 vision 图保存到 uploads/，并把文件路径注入消息文本，
    # 这样 Leader 既能"看到"图（内联 vision），又知道图的本地路径，可通过
    # send_message_to_agent 的 image_paths 参数转交给子 Agent。
    saved_paths = []
    for img in images:
        data_url = (img.get("image_url") or {}).get("url", "") if isinstance(img, dict) else ""
        path = save_data_url_image(data_url, name_prefix="slack")
        if path:
            saved_paths.append(path)
    if saved_paths:
        path_lines = "\n".join(f"- {p}" for p in saved_paths)
        text = (f"{text}\n\n[已保存用户上传的图片到本地，如需让子 Agent 处理，"
                f"请用 send_message_to_agent 的 image_paths 参数传入以下路径]：\n{path_lines}")
        print(f"[local] 已落盘 {len(saved_paths)} 张上传图片到 uploads/")

    _TeammateManager.send_message_to_agent(target_agent, text, f"SlackUser:{user_id}", images=images)


# 当前活跃的 thread_ts（用于输出回传）
_current_thread_ts = ""


async def _ws_client():
    """WebSocket 客户端主循环：连接、注册、收发消息。"""
    global _ws_connection, _ws_loop
    _ws_loop = asyncio.get_event_loop()

    while True:
        try:
            print(f"[local] 正在连接 Relay 服务器: {WS_SERVER_URL}")
            async with websockets.connect(WS_SERVER_URL, max_size=WS_MAX_MESSAGE_SIZE) as ws:
                _ws_connection = ws

                # ── 注册 ──
                agent_names = _get_agent_names()
                await ws.send(json.dumps({
                    "type": "register",
                    "user_id": SLACK_USER_ID,
                    "agents": agent_names,
                }))
                print(f"[local] 已注册: user_id={SLACK_USER_ID}, agents={agent_names}")

                # ── 启动输出转发任务 ──
                output_task = asyncio.create_task(_forward_output(ws))

                # ── 接收 Relay 消息 ──
                try:
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = data.get("type")

                        if msg_type == "registered":
                            print(f"[local] 服务器确认注册成功")

                        elif msg_type == "user_message":
                            # 在独立线程中处理，避免阻塞 WebSocket
                            threading.Thread(
                                target=_route_message_to_agent,
                                args=(data,),
                                daemon=True,
                                name="MessageRouter",
                            ).start()
                finally:
                    output_task.cancel()

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            print(f"[local] 连接断开: {e}，5 秒后重连...")
            _ws_connection = None
            await asyncio.sleep(5)


async def _forward_output(ws):
    """异步任务：从输出队列取数据，通过 WebSocket 发给 Relay。"""
    loop = asyncio.get_event_loop()
    while True:
        # 在线程池中阻塞等待队列
        item = await loop.run_in_executor(None, _output_forward_queue.get)
        agent_name = item.get("agent_name", "Unknown")
        msg_type = item.get("msg_type", "text")
        content = item.get("content", "")
        if not content:
            continue

        payload = {
            "type": "agent_output",
            "thread_ts": _current_thread_ts,
            "agent_name": agent_name,
            "msg_type": msg_type,
            "content": content,
        }

        # image 类型：content 是本地图片路径，读取文件 base64 后一并发送，供 Relay 上传到 Slack
        if msg_type == "image":
            try:
                with open(content, "rb") as f:
                    payload["image_b64"] = base64.b64encode(f.read()).decode("utf-8")
                payload["image_name"] = os.path.basename(content)
                payload["content"] = os.path.basename(content)
            except Exception as e:
                print(f"[local] 读取图片失败 {content}: {e}")
                continue

        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            print(f"[local] 输出发送失败: {e}")


def main():
    # 启动 AgentTeam 主循环（不启用 CLI 输入线程）
    threading.Thread(
        target=AgentTeamMain,
        args=(False,),
        daemon=True,
        name="AgentTeamThread",
    ).start()

    # 启动输出监控线程
    threading.Thread(
        target=_output_monitor,
        daemon=True,
        name="OutputMonitor",
    ).start()

    print(f"[local] AgentTeam 已启动")
    print(f"[local] Slack User ID: {SLACK_USER_ID}")
    print(f"[local] Relay Server: {WS_SERVER_URL}")

    # 运行 WebSocket 客户端（阻塞）
    asyncio.run(_ws_client())


if __name__ == "__main__":
    main()
