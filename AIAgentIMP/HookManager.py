from GlobalConfig import *

# Run hook commands from the workspace root so paths in .hooks.json
# can stay relative to the repository, e.g. "python .hook/foo.py".
HOOK_WORKDIR = WORKDIR
HOOK_CONFIG_PATH = WORKDIR / ".hook/.hooks.json"
HOOK_EVENTS = ("SessionStart","PreToolUse", "PostToolUse")
HOOK_TIMEOUT = 100 # 秒

class HookManager:
    """
    从 .hooks.json 配置加载并执行 Hook。

    Hook 管理器只做三件事：
    - 加载 Hook 定义
    - 为时间允许匹配命令
    - 为调用方汇总阻止/消息结果
    """

    def __init__(self, config_path = HOOK_CONFIG_PATH):
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        #config_path = HOOK_CONFIG_PATH
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[已从 {config_path}] 加载 Hook]")
            except Exception as e:
                print(f"[Hook 配置错误：{e}]")

    def run_hooks(self, event:str, context: dict = None) -> dict:
        """
        执行某事件的所有 Hook。

        返回：{"blocked": bool, "messages": list[str]}
          - blocked: 任何 Hook 返回退出码 1 则为 True
          - messages: 退出码 2 的 Hook 的 stderr 内容（用于注入）
        """
        result = {"blocked": False, "messages": []}

        hooks = self.hooks.get(event, [])

        #遍历当前event的每个hook
        for hook_def in hooks:
            #匹配器检查 (PreToiolUse/ PostToolUse 的工具名过滤)
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                # 如果工具名称不为 * 并且匹配不上 tool_name , 那么跳过这个 hook_def
                if matcher != "*" and matcher != tool_name:
                    continue

            # 如果找不到 command 也直接跳过当前 hook_def
            command = hook_def.get("command", "")
            if not command:
                continue

            #使用 Hook 上下文构建环境变量 (Hook Scripts 入参)
            env = dict(os.environ)
            if context:
                env["Hook_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"]  = json.dumps(
                    context.get("tool_input",""), ensure_ascii=False)[:10000]
                # 这块对应的 Hook 应该是 PostToolUse , 只有PostToolUse才会有tool_output
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"])[:10000]

            try:
                r = subprocess.run(
                    command, shell=True, cwd=HOOK_WORKDIR, env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=HOOK_TIMEOUT
                )

                # 0：继续执行；
                # 1：阻止工具调用
                # 2：注入messages然后执行工具
                match r.returncode:
                # 这里的 returncode 就是退出返回值
                    case 0:
                        #静默继续
                        if r.stdout.strip():
                            print(f" [hook: {event}] {r.stdout.strip()[:100]}")

                        # 可选结构化 stdout: 一个保持教学约定简单扩展点
                        try:
                            # 这里貌似还能修改 tool_input
                            hook_output = json.loads(r.stdout)
                            if "updatedInput" in hook_output and context:
                                context["tool_input"] = hook_output["updatedInput"]
                            if "additionalContext" in hook_output:
                                result["messages"].append(
                                    hook_output["additionalContext"])
                            if "permissionDecision" in hook_output:
                                result["permission_override"] = (
                                    hook_output["permissionDecision"])
                        except (json.JSONDecodeError, TypeError):
                            pass # stdout 不是 JSON -- 对简单 Hook 很正常

                    case 1:
                        #阻止运行
                        result["blocked"] = True
                        reason = r.stderr.strip() or "被hook阻止"
                        result["block_reason"] = reason
                        print(f" [hook:{event}] 已阻止： {reason[:200]}")

                    case 2:
                        #注入消息
                        msg = r.stderr.strip() #不知道为啥这里要用 stderr, 我想用 stdout, 感觉更合理一点
                        if msg:
                            result["messages"].append(msg)
                            print(f" [hook: {event}] 注入：{msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f" [hook:{event}] 超时：（{HOOK_TIMEOUT}s）")
            except Exception as e:
                print(f" [hook:{event}] 错误 {e}")

        return result

def append_hook_result_to_messages(hook_result, tool_id, messages)->dict:
    for msg in hook_result.get("messages",[]):
        result = {"role": "tool", "tool_call_id": tool_id,"content": msg}
        messages.append(result)
    return messages


def hook_should_block_tool_use(pre_tool_hook_result, tool_id, messages):
    if pre_tool_hook_result.get("blocked", False):
        reason = pre_tool_hook_result.get("block_reason", "被 Hook 阻止")
        output = f"工具被 PreToolUse Hook 阻止： {reason}"
        messages.append({
            "role": "tool",
            "tool_call_id": tool_id,
            "content": output,
        })
        return True, messages
    else:
        return False, messages
