#!/usr/bin/env python3
"""SessionStart hook — 打印欢迎信息和工作区摘要。"""
import os
from pathlib import Path

workdir = Path.cwd()
print(f"[SessionStart] Workspace: {workdir}")

# 统计文件数
file_count = sum(1 for _ in workdir.rglob("*") if _.is_file() and ".git" not in str(_))
print(f"[SessionStart] Total files: {file_count}")

# 退出码 0 = 继续
exit(0)
