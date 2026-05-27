from datetime import datetime
import json
import os
import platform
import re
import subprocess
import threading
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
from queue import Queue
from SkillManager import SkillRegistry


load_dotenv(override=True)

PLAN_REMINDER_INTERVAL = 3  # Todo 工具提醒间隔，单位轮数
WORKDIR = Path.cwd()
client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY"))
MODEL = os.environ["MODEL_ID"]
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
SKILLS_DIR = WORKDIR / "skills"
SUB_AGENT_DIR = WORKDIR / ".agent"

CONTEXT_LIMIT = 10

_MainAgent_Skills : SkillRegistry = None
_MainAgent_InputQueue = Queue(maxsize=3)
_MainAgent_IdleStatus = True
_MainAgent_Lock = threading.Lock()
_MainAgent_HOOKS = None
_SESSION_CONTEXT = threading.local()

_TeammateManager = None
_SubAgentLoader = None



# 外部前端输出回调 {agent_name: callable(agent_name, msg_type, content)}
from queue import Queue
_AgentTeam_OutputPrint: Queue = Queue()
