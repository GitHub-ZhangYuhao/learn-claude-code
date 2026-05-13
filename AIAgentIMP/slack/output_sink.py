"""
output_sink.py - 将 Agent 输出实时推送到 Slack Thread

每个 SlackOutputSink 实例绑定一个 (channel, thread_ts) 对，
Agent 输出时通过回调自动发送到对应的 Slack Thread。
"""


class SlackOutputSink:
    """将 Agent 输出推送到 Slack Thread 的回调对象。

    使用方式：
        sink = SlackOutputSink(client, channel, thread_ts)
        _OutputCallbacks[agent_name] = sink
    """

    MSG_PREFIXES = {
        "text": "💬",
        "tool_call": "🔧",
        "tool_result": "📋",
    }

    def __init__(self, slack_client, channel_id: str, thread_ts: str):
        self.client = slack_client
        self.channel = channel_id
        self.thread_ts = thread_ts

    def __call__(self, agent_name: str, msg_type: str, content: str):
        """回调签名：(agent_name, msg_type, content)

        msg_type: "text" | "tool_call" | "tool_result"
        """
        prefix = self.MSG_PREFIXES.get(msg_type, "")
        text = f"*[{agent_name}]* {prefix} {content}"
        if len(text) > 3000:
            text = text[:3000] + "...(truncated)"
        try:
            self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=self.thread_ts,
                text=text,
            )
        except Exception as e:
            print(f"[SlackOutputSink] 发送失败: {e}")
