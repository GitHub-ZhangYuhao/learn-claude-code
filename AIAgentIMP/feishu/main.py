#!/usr/bin/env python3
"""
feishu/main.py — 飞书前端入口

替代 AIAgentIMP.py 的 stdin 交互，通过飞书群聊接收用户消息、展示 Agent 输出。
零改动现有代码，通过 monkey-patch 拦截 print / agentprint 输出。

使用方法:
    1. 在飞书开发者后台创建自建应用，开启机器人能力
    2. 申请权限: im:message.p2p_msg:readonly, im:message.group_at_msg:readonly, im:message:send_as_bot
    3. 事件订阅: 长连接模式, 添加 im.message.receive_v1
    4. 在飞书群里为每个 Agent 添加一个「自定义机器人」，设置对应的名字和头像
    5. 在 .env 中配置:
        FEISHU_APP_ID=cli_xxxxxxxxxx
        FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxx
        FEISHU_WEBHOOK_Leader=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
        FEISHU_WEBHOOK_CodeReviewer=https://open.feishu.cn/open-apis/bot/v2/hook/yyy
    6. pip install lark-oapi
    7. python feishu/main.py
"""

import os
import re
import sys
import logging
import threading
from pathlib import Path
from time import sleep

# 将父目录加入 sys.path，使得 AIAgentIMP/TeammateManager 等模块可被导入
_PARENT_DIR = str(Path(__file__).resolve().parent.parent)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(_PARENT_DIR, ".env"), override=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("main_feishu")

# ============================================================
# 1. 检查飞书配置
# ============================================================
_FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
_FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

if not _FEISHU_APP_ID or not _FEISHU_APP_SECRET:
    logger.error("请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    sys.exit(1)

# 加载 Webhook 配置: FEISHU_WEBHOOK_{AgentName} = webhook_url
_WEBHOOK_PREFIX = "FEISHU_WEBHOOK_"
_agent_webhooks = {}
for _k, _v in os.environ.items():
    if _k.startswith(_WEBHOOK_PREFIX) and _v:
        _agent_webhooks[_k[len(_WEBHOOK_PREFIX):]] = _v

if _agent_webhooks:
    logger.info("已加载 Agent Webhook 配置: %s", list(_agent_webhooks.keys()))
else:
    logger.warning("未配置任何 FEISHU_WEBHOOK_* 环境变量，所有 Agent 将通过主 Bot 发送卡片")

# ============================================================
# 2. 导入已有模块（触发 Agent/Tool/Skill 模块级初始化）
# ============================================================
import AIAgentIMP
import TeammateManager as tm_module
from GlobalConfig import _MainAgent_InputQueue
from AIAgentIMP import (
    _TeammateManager,
    agent_loop,
    begin_main_agent_single_loop,
    end_main_agent_single_loop,
)
from adapter import FeishuAdapter

# ============================================================
# 3. 初始化飞书适配器
# ============================================================
_feishu = FeishuAdapter(
    app_id=_FEISHU_APP_ID,
    app_secret=_FEISHU_APP_SECRET,
    agent_webhooks=_agent_webhooks,
)
_feishu.set_teammate_manager(_TeammateManager)


# ============================================================
# 4. Monkey-patch: 子 Agent 输出 → 飞书卡片
#
#    替换 TeammateManager 模块的 agentprint 函数。
#    _teammate_loop 内部通过 LOAD_GLOBAL 解析 agentprint，
#    替换模块级引用后，所有子 Agent 线程的调用都会走到这里。
# ============================================================
_original_agentprint = tm_module.agentprint
_sub_agent_tool_buf = {}  # {agent_name: [tool_summary_strings]}


def _patched_agentprint(name: str, text: str):
    """包装 agentprint: 保留终端彩色输出 + 发送飞书卡片"""
    _original_agentprint(name, text)
    logger.debug("[agentprint 拦截] agent=%s text=%s", name, text[:100])

    try:
        # 工具调用信息 → 缓存摘要（含参数）
        if "[AgentTeam工具调用]" in text and "使用工具：" in text:
            match = re.search(r"使用工具：\n(\S+)\s*:\s*参数：(.*)", text, re.DOTALL)
            if match:
                tool_name = match.group(1)
                tool_args_str = match.group(2).strip()
                if len(tool_args_str) > 120:
                    tool_args_str = tool_args_str[:120] + "..."
                _sub_agent_tool_buf.setdefault(name, []).append(
                    f"⚙️ {tool_name}: {tool_args_str}"
                )
                logger.info("[子Agent工具] %s → %s", name, tool_name)
            return

        # 工具调用结果 → 追加到最近一条工具摘要
        if "[AgentTeam工具调用]" in text and "工具调用结果" in text:
            match = re.search(r"工具调用结果：\n\s*(.*)", text, re.DOTALL)
            if match:
                result_text = match.group(1).strip()
                short = result_text[:150] + ("..." if len(result_text) > 150 else "")
                buf = _sub_agent_tool_buf.get(name, [])
                if buf:
                    buf[-1] += f"\n    → {short}"
            return

        # Agent 正文消息 → 提取内容，附带工具摘要，发卡片
        if "[AgentTeam消息]" in text:
            match = re.search(r"---\n(.*?)\n---", text, re.DOTALL)
            content = match.group(1).strip() if match else text

            tools = _sub_agent_tool_buf.pop(name, [])
            tools_summary = "\n".join(tools) if tools else ""
            logger.info("[子Agent发卡片] %s → %s", name, content[:80])
            _feishu.send_card(name, content, tools_summary)
            return

        # catch-all: 未匹配任何模式的输出，也发卡片（避免静默丢弃）
        clean = text.strip()
        if clean and clean != "None":
            tools = _sub_agent_tool_buf.pop(name, [])
            tools_summary = "\n".join(tools) if tools else ""
            logger.info("[子Agent catch-all] %s → %s", name, clean[:80])
            _feishu.send_card(name, clean[:2000], tools_summary)

    except Exception as e:
        logger.error("[agentprint patch 异常] %s", e, exc_info=True)


# 替换模块级函数引用
tm_module.agentprint = _patched_agentprint


# ============================================================
# 5. Monkey-patch: Leader 输出 → 飞书卡片
#
#    将 AIAgentIMP 模块命名空间中的 print 替换为自定义版本。
#    agent_loop() 内部的 print() 通过 LOAD_GLOBAL 在模块 __dict__
#    中找到这个替换后的版本，而其他模块的 print 不受影响。
#
#    输出识别规则:
#    - "> \n使用工具：{name} : 参数：{args}" → 工具调用摘要，缓存
#    - 紧跟工具调用后的 print(output[:200])  → 工具原始输出，跳过
#    - 其他非空文本                          → Leader assistant 回复，发卡片
# ============================================================
_original_print = print
_leader_tool_buf = []
_leader_tool_output_pending = False


def _leader_print(*args, **kwargs):
    """拦截 AIAgentIMP 模块内的 print() 调用"""
    global _leader_tool_output_pending
    # 始终保留终端输出
    _original_print(*args, **kwargs)

    if not args:
        return
    raw = str(args[0])
    text = raw.strip()
    if not text or text == "None":
        return

    logger.debug("[leader print 拦截] text=%s", text[:100])

    try:
        # 工具调用信息: "> \n使用工具：bash : 参数：{'command': '...'}"
        if raw.startswith("> \n使用工具：") or (text.startswith(">") and "使用工具：" in text):
            match = re.search(r"使用工具：(\S+)\s*:\s*参数：(.*)", text, re.DOTALL)
            if match:
                tool_name = match.group(1)
                tool_args_str = match.group(2).strip()
                if len(tool_args_str) > 120:
                    tool_args_str = tool_args_str[:120] + "..."
                _leader_tool_buf.append(f"⚙️ {tool_name}: {tool_args_str}")
            _leader_tool_output_pending = True
            return

        # 工具执行结果（紧跟工具调用后） → 追加到最近一条工具摘要
        if _leader_tool_output_pending:
            _leader_tool_output_pending = False
            short = text[:150] + ("..." if len(text) > 150 else "")
            if _leader_tool_buf:
                _leader_tool_buf[-1] += f"\n    → {short}"
            return

        # Leader assistant 回复 → 附带已缓存的工具摘要一起发卡片
        tools_summary = ""
        if _leader_tool_buf:
            tools_summary = "\n".join(_leader_tool_buf)
            _leader_tool_buf.clear()
        _feishu.send_card("Leader", text, tools_summary)

    except Exception as e:
        logger.error("[leader print patch 异常] %s", e, exc_info=True)


# 注入到 AIAgentIMP 模块的命名空间
AIAgentIMP.print = _leader_print


# ============================================================
# 6. 主 Agent 消息循环线程
# ============================================================

def _flush_leader_tool_buf():
    """agent_loop 结束后，刷新剩余的工具摘要缓存"""
    if _leader_tool_buf:
        tools_summary = "\n".join(_leader_tool_buf)
        _leader_tool_buf.clear()
        _feishu.send_card("Leader", "✅ 执行完毕", tools_summary)


def _main_agent_loop():
    """
    从 _MainAgent_InputQueue 取消息 → 执行 agent_loop

    复制自 AIAgentIMP.py 的 __main__ 逻辑，
    但用飞书替代 stdin 作为消息来源。
    """
    history = []
    while True:
        if not _MainAgent_InputQueue.empty():
            user_query_stream = _MainAgent_InputQueue.get()
            history.append(user_query_stream)

            begin_main_agent_single_loop()
            try:
                agent_loop(history)
            except Exception as e:
                logger.error("[agent_loop 异常] %s", e, exc_info=True)
                _feishu.send_card("Leader", f"⚠️ 执行出错：{e}")
            finally:
                _flush_leader_tool_buf()
                end_main_agent_single_loop()

        sleep(1)


# ============================================================
# 7. 入口
# ============================================================

if __name__ == "__main__":
    logger.info("========================================")
    logger.info("  飞书 Agent 系统启动")
    logger.info("  App ID: %s", _FEISHU_APP_ID[:8] + "...")
    logger.info("========================================")

    # 启动主 Agent 循环线程
    agent_thread = threading.Thread(
        target=_main_agent_loop,
        daemon=True,
        name="MainAgentLoopThread",
    )
    agent_thread.start()

    # 飞书 WebSocket 长连接（阻塞主线程）
    try:
        _feishu.start()
    except KeyboardInterrupt:
        logger.info("[飞书] 收到 Ctrl+C，退出")
