import datetime
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

_MainAgent_Skills : SkillRegistry = None
_MainAgent_InputQueue = Queue(maxsize=1)
_MainAgent_IdleStatus = True
_MainAgent_Lock = threading.Lock()
