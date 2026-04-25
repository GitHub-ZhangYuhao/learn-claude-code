
from dataclasses import dataclass, field
from GlobalConfig import PLAN_REMINDER_INTERVAL

@dataclass
class PlanItem:
    content: str
    status: str = "pending"
    active_form: str = ""

# 计划状态 包含多个计划项 和 距离上次更新轮数
@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0

class TodoManager:
    def __init__(self):
        self.this_turn_used_todo: bool = False
        self.state = PlanningState()

    def update(self, item: list) -> str:
        if len(item) > 12:
            raise ValueError("让对话计划更短（最多12个计划项）")

        normalized = []
        in_progress_count = 0
        for index, raw_item in enumerate(item):
            content = str(raw_item.get("content", "")).strip() # strip 去除字符串两端的空白字符（空格、制表符、换行符等），用于清理数据，避免前后多余的空白影响后续处理
            status = str(raw_item.get("status", "")).lower() # lower 转换为小写，确保状态值统一
            active_form = str(raw_item.get("active_form", "")).strip() # strip 去除字符串两端的空白字符（空格、制表符、换行符等），用于清理数据，避免前后多余的空白影响后续处理

            if not content:
                raise ValueError(f"计划项(Item) {index} 缺少内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"计划项(Item) {index} 状态值无效（{status}）")
            if status == "in_progress":
                in_progress_count += 1

            normalized.append(PlanItem(
                content,
                status,
                active_form))

            # 确保计划中只能有一个步骤处于进行状态
        if in_progress_count > 1:
            raise ValueError("计划中只能有一个步骤处于进行状态")

        self.state.items = normalized       # 更新计划项列表
        self.state.rounds_since_update = 0 # 重置距离上次更新轮数
        return self.render() # 将计划渲染为字符串输出
    def render(self) -> str:
        if not self.state.items:
            return "还没有生成对话计划"

        lines = ["\n"]
        lines.append("对话计划：")
        for item in self.state.items:
            maker = {
                "pending": "[__待办__] ",
                "in_progress": "[__进行中__] ",
                "completed": "[__已完成__] ",
            }[item.status]  # 使用item.status作为键，从字典中获取对应的中文标签
            line = f"{maker} {item.content}"
            if item.status == "in_progress" and item.active_form:
                line += f" ({item.active_form})"
            lines.append(line)

        """
        代码会检查 self.state.items 中的每一个任务
        对于每个任务，判断其 status 属性是否等于 "completed"
        如果是已完成状态，就计数1
        最后将所有计数相加，得到总的已完成任务数
        """
        completed = sum(1 for item in self.state.items if item.status == "completed")
        lines.append(f"\n({completed}/{len(self.state.items)} completed)\n") # (已完成数/总任务数 completed)
        return "\n".join(lines)

    # 记录当前轮次，增加距离上次更新轮数
    def note_round_without_update(self) -> None:
        self.state.rounds_since_update += 1

    # 输出提醒模型更新计划
    def reminder(self) -> str | None:
        if not self.this_turn_used_todo:    #如果没有使用 计划工具 直接返回
            return None
        if not self.state.items:
            return None
        if self.state.rounds_since_update < PLAN_REMINDER_INTERVAL:  #如果距离上次更新轮数小于提醒间隔，不提醒
            return None
        return f"<reminder> 你已经有{self.state.rounds_since_update}轮没有更新计划，如果你觉得任务需要更新计划，建议再继续执行前，先更新计划。 </reminder>"

    def check_used_todo_tool(self, tool_name: str) -> None:
        if tool_name == "todo":
            self.used_todo_tool = True
            self.this_turn_used_todo = True
        else:
            self.used_todo_tool = False

    def post_tool_call(self, messages: list) -> list:

        if self.used_todo_tool:
            self.state.rounds_since_update = 0
            self.this_turn_used_todo = True
        else :
            # 如果没有 使用 计划工具，增加距离上次更新轮数
            self.note_round_without_update()
            reminder = self.reminder()       # 如果超过3轮，将  “<reminder> 再继续执行前，请更新计划。 </reminder>”  加入结果中
            if reminder:
                messages.append({"role":"user","content":reminder})
        return messages

TODO_TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "todo",
        "description": "Rewrite the current session plan for multi-step work.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"]
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Optional present-continuous label."
                            }
                        },
                        "required": ["content", "status"]
                    }
                }
            },
            "required": ["items"]
        }
    }
}]

# 如果只有一个Agent的话，直接创建一个单例即可，如果有多个Agent，每个Agent都需要一个TodoManager实例，用于管理自己的计划
TODO = TodoManager()
