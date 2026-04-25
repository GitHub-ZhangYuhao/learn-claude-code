import time
import random
import time
import random
from GlobalConfig import *
import json

MAX_RECOVERY_ATTEMPTS = 3
BACKOFF_BASE_DELAY = 1.0  # seconds
BACKOFF_MAX_DELAY = 30.0  # seconds
CONTINUATION_MESSAGE = (
    "输出达到上限。请直接从中断处继续——"
    "不要总结、不要重复。如有需要，可在句中接续发言。"
)


def auto_compact(messages: list) -> list:
    """
    将会话历史压缩为简短的续写摘要。
    """
    conversation_text = json.dumps(messages, default=str)[:80000]
    prompt = (
            "为保证连续性，请总结这段对话。包含：\n"
            "1) 任务概览与成功标准\n"
            "2) 当前状态：已完成工作、涉及文件\n"
            "3) 关键决策与失败方案\n"
            "4) 剩余下一步\n"
            "请简洁但保留关键信息。\n\n"
            + conversation_text
    )
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        summary = response.choices[0].message.content
    except Exception as e:
        summary = f"(压缩失败：{e})。之前上下文已丢失。"

    continuation = (
        "本次会话从被压缩的上一段对话继续。"
        f"此前上下文摘要：\n\n{summary}\n\n"
        "请从中断处继续，不要重新向用户提问。"
    )
    return [{"role": "user", "content": continuation}]

def backoff_delay(attempt: int) -> float:
    """带抖动的指数退避：base * 2^attempt + random(0, 1)。"""
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)
    return delay + jitter

class ErrorRecoveryManager:
    def __init__(self):
        self.current_attempt = 0

    def choose_recovery(self, finish_reason: str | None, error_text: str | None ) -> dict:
        if finish_reason in ["tool_calls", "stop"]:
            return {"kind":"no_action","reason":"没有问题正常执行"}
        if finish_reason == "length":    #OpenAI 库使用 finish_reason == "length" 等于 Antropic使用 stop_reason == "max_tokens":
            return {"kind":"continue", "reason":"输出截断"}
        if error_text and ("prompt" in error_text) and ("long" in error_text):
            return {"kind":"compact", "reason":"上下文过长"}
        if error_text and any(word in error_text for word in ["timeout", "rate", "unavailable", "connection"]):
            return {"kind":"backoff", "reason": "暂时性传输失败"}

        return {"kind":"fail", "reason":"未知 或者 不可恢复的错误"}

    # 根据 recover_decision 做修改 然后选择是 continue 还是 break (0 break ; 1 continue)
    def recovery_by_decision(self,recovery_decision: dict, messages: list, attempt: int = 1) -> (bool, bool, list):
        need_continue = False
        has_error = True

        # 如果超过了最大尝试次数，直接停止
        if self.current_attempt >= MAX_RECOVERY_ATTEMPTS:
            print(f"--[ErrorRecovery]达到最大恢复次数 {self.current_attempt}/{MAX_RECOVERY_ATTEMPTS} 次，停止恢复。--")
            return False, False, messages

        match recovery_decision["kind"]:
            case "no_action":
                print(f"--[ErrorRecovery]没有问题正常执行。--")
                has_error = False
                # 没有错误，直接返回
                return has_error, need_continue, messages
            case "continue":
                messages.append({"role":"user", "content":CONTINUATION_MESSAGE, "partial":True})
                print(f"--[ErrorRecovery]输出截断，执行续写。--")
                need_continue = True
            case "compact":
                messages[:] = auto_compact(messages)
                print(f"--[ErrorRecovery]上下文过长，执行压缩。--")
                need_continue = True
            case "backoff":
                time.sleep(backoff_delay(attempt))
                print(f"--[ErrorRecovery]暂时性传输失败，等待 {backoff_delay(attempt)} 秒后重试。--")
                need_continue = True
            case "fail":
                print(f"--[ErrorRecovery]未知 或者 不可恢复的错误。--")
                need_continue = False

        self.current_attempt += 1
        return has_error, need_continue, messages