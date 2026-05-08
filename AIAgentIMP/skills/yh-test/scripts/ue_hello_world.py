#!/usr/bin/env python3
"""通过 UE Web Remote Control HTTP API 远程在 UE 中执行 Python 代码。"""

import os
import requests


def ue_exec(code: str, params=None, url="http://localhost:30010"):
    if params:
        for k, v in params.items():
            code = code.replace(f"{{{k}}}", str(v))
    return requests.put(
        f"{url}/remote/object/call",
        json={
            "objectPath": "/Script/PythonScriptPlugin.Default__PythonScriptLibrary",
            "functionName": "ExecutePythonScript",
            "parameters": {"PythonScript": code},
        },
        timeout=10,
    ).json()


if __name__ == "__main__":
    code = "{code}"
    params = {"code": os.environ.get("UE_CODE", "print('Hello YH')")}
    resp = ue_exec(code, params)
    print(f"UE response: {resp}")

'''
示例： 使用如下方式添加环境变量
$env:UE_CODE='print("HelloYHHHHHH")'; python ue_hello_world.py
'''