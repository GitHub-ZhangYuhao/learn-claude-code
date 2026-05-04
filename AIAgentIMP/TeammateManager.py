import time
from queue import Queue
from time import sleep

from GlobalConfig import *
from DefaultToolManager import BASIC_TOOLS, BASIC_TOOL_HANDLERS
from SystemPromptBuilder import SystemPromptBuilder
import threading
from dataclasses import dataclass, field

@dataclass
class AgentProperty:
    name: str                       = ""
    role: str                       = ""
    historyMessages: list           = field(default_factory=list)   #只能再Agent循环过程中管理，不可再外部修改
    thread: threading.Thread        = None
    isIdleStatus: bool              = True                          #只能再Agent循环过程中管理，不可再外部修改
    inputQueue: Queue               = field(default_factory=Queue)  #外部传入此轮需要处理的输入。
    outputQueue: Queue              = field(default_factory=Queue)  #只能再Agent循环过程中管理，不可再外部修改




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
            "description": "生成一个常驻型队友，该队友在独立线程中运行.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "prompt": {"type": "string"}
                },
                "required": ["name", "role"]
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



class TeammateManager:
    def __init__(self):
        self.dir = TEAM_DIR
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}
        self.agent_Properties = {}

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


    def spawn(self, name: str, role: str, prompt: str = None) -> str:
        member = self._find_member(name)
        if member:
            #if member["status"] not in ["idle", "shutdown"]:
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
            args=(name, role),
            daemon=True,
            name = f"AgentThread_{name}"
        )
        self.agent_Properties[name] = AgentProperty(name=name, role=role, thread=thread, isIdleStatus=True)
        if prompt:
            self.send_message_to_agent(name, prompt)
        self.threads[name] = thread
        thread.start()
        return f"生成了'[{name}]' (角色:{role}), 请等待 {name} 完成工作。"

    def is_agent_loop_run(self, name: str, prompt:str = None) -> bool:
        isIdle = self.agent_Properties[name].isIdleStatus
        hasInput = (not self.agent_Properties[name].inputQueue.empty())
        return isIdle and hasInput

    # 队列提取所有输入的消息，并且转化为LLM的输入的消息体
    def parse_agent_input_queue(self, name: str) -> list:
        message_block = []
        while not self.agent_Properties[name].inputQueue.empty():
            msg = self.agent_Properties[name].inputQueue.get()
            message_block.append({"role":"user", "content": msg})
        return message_block

    def begin_agent_single_loop(self, name: str):
        # 确保Agent状态为idle, 然后设置为running
        assert self.agent_Properties[name].isIdleStatus, f"Agent[{name}] 已经在运行中,不在idle状态,不能开始新的循环"
        self.agent_Properties[name].isIdleStatus = False
    def end_agent_single_loop(self, name: str, message: str = None, messages: list = None):
        if self.agent_Properties[name].isIdleStatus == False:
            self.agent_Properties[name].isIdleStatus = True
            if message:
                self.agent_Properties[name].outputQueue.put(message)
            if messages:
                self.agent_Properties[name].historyMessages = messages


    def _teammate_loop(self, name: str, role: str):
        # 线程循环，执行团队成员的任务
        # 线程名称为 AgentThread_成员名
        # 线程为守护线程，程序退出时会自动终止
        while True:
            # 等待成员状态为 idle，并且有消息队列传入
            while not self.is_agent_loop_run(name):
                time.sleep(2) #如果没有消息，或者成员状态不是 idle，等待2秒

            # 历史消息
            messages = list()
            messages += self.agent_Properties[name].historyMessages
            while not self.agent_Properties[name].inputQueue.empty():
                messages += self.parse_agent_input_queue(name)

            #标记该Agent开始工作。
            self.begin_agent_single_loop(name)

            systemPromptBuilder = SystemPromptBuilder()
            sys_prompt = (f"你是一个团队成员，你的名字是{name}，"
                          f"你的角色是 {role}，你的任务是根据团队的需求，完成任务。"
                          f"在{WORKDIR}工作区工作")
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
                except Exception as e:
                    # 编辑该Agent的状态为 idle，将错误信息添加到消息队列中
                    self.end_agent_single_loop(name, str(e), messages)
                    break

                msg = response.choices[0].message.content
                if msg != "":
                    messages.append({"role": "assistant", "content": msg})
                    print(f"\n [AgentTeam消息]:({name}) :\n---\n{msg}\n---\n")

                if response.choices[0].finish_reason != "tool_calls":
                    break
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

            # Agent 单论对话执行完毕，更新成员状态为 idle
            self.end_agent_single_loop(name, messages[-1]["content"], messages)
            member = self._find_member(name)
            if member and member["status"] != "shutdown":
                member["status"] = "idle"
                self._save_config()

    def list_all(self) -> str:
        if not self.agent_Properties:
            return "当前团队没有成员"
        lines = [f"当前Agent团队成员：\n"]
        for agent_name, agent_prop in self.agent_Properties.items():
            lines.append(f"- [{agent_name}] 状态:({'idle' if agent_prop.isIdleStatus else 'running'}) : {agent_prop.role}  \n")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]

    def _teammate_tools(self) -> list:
        return BASIC_TOOLS

    def _teammate_tools_handler(self) -> dict:
        TOOL_HANDLERS = BASIC_TOOL_HANDLERS.copy()
        return TOOL_HANDLERS

    # 向团队成员发送消息
    def send_message_to_agent(self, agent_name: str, prompt: str) -> str:
        # 检查成员是否存在
        if agent_name in self.agent_Properties:
            self.agent_Properties[agent_name].inputQueue.put(prompt)
            return f"已向 {agent_name} 发送消息：{prompt}"
        else:
            return f"成员 {agent_name} 不存在"


    def join_every_threads(self) -> None:
        for thread in self.threads.values():
            thread.join()



if __name__ == "__main__":
    import random
    tm = TeammateManager()
    def ProducerPrompt(num:int , queue:Queue):
        time.sleep(random.randint(10, 20))
        msg = "你好"  #input(">> 消息:")
        queue.put(msg)

    AgentName = "PythonAgent"
    # 先生成一个Agent，它持久占用一个线程。
    tm.spawn(AgentName, "Python开发大师")

    threads = []
    for i in range(2):
        t = threading.Thread(target=ProducerPrompt, args=(i, tm.agent_Properties[AgentName].inputQueue))
        t.start()
        t.join()
        threads.append(t)

    outputMsg = []
    while True:
        msg = tm.agent_Properties[AgentName].outputQueue.get()
        outputMsg.append(msg)
        print(f"收到来自 [{AgentName}] 的消息：\n{msg}")
        sleep(3)
        print(tm.list_all())
