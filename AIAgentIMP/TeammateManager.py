import time

from GlobalConfig import *
from DefaultToolManager import BASIC_TOOLS, BASIC_TOOL_HANDLERS
from SystemPromptBuilder import SystemPromptBuilder
import threading

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

TEAMMATE_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "spawn_teammate",
            "description": "Spawn a persistent teammate that runs in its own thread.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "prompt": {"type": "string"}
                },
                "required": ["name", "role", "prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_teammates",
            "description": "List all teammates with name, role, status.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

#TODO: 完成消息传输
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type:str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"错误：无效的消息类型 {msg_type}。 有效类型为：{VALID_MSG_TYPES}"
        msg = {
            "type"      : msg_type,
            "from"      : sender,
            "content"   : content,
            "timestamp" : time.time(),
        }
        if extra:
            msg.update(extra)



class TeammateManager:
    def __init__(self):
        self.dir = TEAM_DIR
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self):
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding= "utf-8"))
        return {"team_name": "default" , "members": []}

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    # 保存配置, 将config写入文件config.json,
    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=4), encoding= "utf-8")


    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            # if member["status"] not in ["idle", "shutdown"]:
            #     return f"错误：{name} 当前为 {member['status']} 状态"
            member["status"] = "running"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        # 更新配置文件，持久化保存成员信息
        self._save_config()
        # 启动线程，执行团队成员的循环
        # 线程名称为 AgentThread_成员名
        # 线程为守护线程，程序退出时会自动终止
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
            name = f"AgentThread_{name}"
        )
        self.threads[name] = thread
        thread.start()
        return f"生成了'{name}' (角色:{role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        # 线程循环，执行团队成员的任务
        # 线程名称为 AgentThread_成员名
        # 线程为守护线程，程序退出时会自动终止
        systemPromptBuilder = SystemPromptBuilder()
        sys_prompt = (f"你是一个团队成员，你的名字是{name}，"
                      f"你的角色是 {role}，你的任务是根据团队的需求，完成任务。"
                      f"在{WORKDIR}工作区工作")
        messages = [{"role":"user", "content": prompt}]
        messages = systemPromptBuilder.setup_system_prompt(messages, sys_prompt)
        teammate_tools = self._teammate_tools()
        teammate_tools_handler = self._teammate_tools_handler()
        for _ in range(50):
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=messages,
                    tools=teammate_tools,
                    max_tokens=1000,
                )
            except Exception:
                break

            msg = response.choices[0].message.content
            if msg != "":
                messages.append({"role": "assistant", "content": msg})
                print(f"\n [AgentTeam消息]:({name}) :\n---\n{msg}\n---\n")

            if response.choices[0].finish_reason != "tool_calls":
                return
            # 遍历所有的 tool_call
            for ToolCall in response.choices[0].message.tool_calls:
                tool_name = ToolCall.function.name
                tool_args = json.loads(ToolCall.function.arguments)
                handler = teammate_tools_handler.get(tool_name)
                output = handler(**tool_args) if handler else f"Unknow Tool: {tool_name}"
                print(f"> \n [AgentTeam工具调用]:({name}) :使用工具：\n{tool_name} : 参数：{tool_args}")
                print(f"> \n [AgentTeam工具调用]:({name}) :工具调用结果：\n {output[:200]}")
                # 检查是否是用来 计划 工具
                # 将 toolcall 添加到 messages 历史中
                result = {"role": "tool", "tool_call_id": ToolCall.id, "content": output}
                messages.append(result)

        # 任务完成，更新成员状态为 idle
        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            member["status"] = "idle"
            self._save_config()

    def list_all(self) -> str:
        if not self.config["members"]:
            return "当前团队没有成员"
        lines = [f"团队名称：{self.config["team_name"]}"]
        for m in self.config["members"]:
            lines.append(f"{m['name']} ({m['role']}) 状态： ({m['status']})")
        return "\n".join(lines)


    def _teammate_tools(self) -> list:
        return BASIC_TOOLS + []

    def _teammate_tools_handler(self) -> dict:
        TOOL_HANDLERS = BASIC_TOOL_HANDLERS.copy()
        return TOOL_HANDLERS

    def join_every_threads(self) -> None:
        for thread in self.threads.values():
            thread.join()




if __name__ == "__main__":
    tm = TeammateManager()
    tm.spawn("TestAgent", "Python开发大师", "在项目的 TestTemp文件夹下创建一个文件 Test.py，内容为 打印HelloWorld")
    tm.join_every_threads()