# UE Web Remote Control 能力清单

## 连接信息

| 项目 | 值 |
|------|-----|
| 地址 | `http://localhost:30010` |
| 协议 | HTTP REST API |
| 认证 | 无（passphrase 已关闭） |

## 前提条件

UE 编辑器需启用以下插件：
- `Remote Control`（基础插件）
- `Remote Control API`（HTTP API 路由）
- `Web Remote Control`（Web 界面，可选）
- `Python Editor Script Plugin`（Python 脚本执行）

## 可用 API 端点

### 1. `PUT /remote/object/call` — 调用函数

调用 UE 中任意对象的方法。

```bash
curl -X PUT http://localhost:30010/remote/object/call \
  -H "Content-Type: application/json" \
  -d '{"objectPath":"/Script/PythonScriptPlugin.Default__PythonScriptLibrary","functionName":"ExecutePythonScript","parameters":{"PythonScript":"print(\"Hello\")"}}'
```

常用对象：
| ObjectPath | 功能 |
|-----------|------|
| `/Script/PythonScriptPlugin.Default__PythonScriptLibrary` | 执行 Python 脚本 |
| `/Script/UnrealEd.Default__EditorActorSubsystem` | 场景 Actor 管理 |
| `/Script/UnrealEd.Default__EditorUtilitySubsystem` | 工具/编辑器功能 |
| `/Script/UnrealEd.Default__EditorLevelLibrary` | Level 操作 |

### 2. `PUT /remote/object/describe` — 描述对象

获取对象的属性列表、函数列表、参数类型等完整 Schema。

```bash
curl -X PUT http://localhost:30010/remote/object/describe \
  -H "Content-Type: application/json" \
  -d '{"ObjectPath":"/Script/UnrealEd.Default__EditorActorSubsystem"}'
```

### 3. `PUT /remote/object/property` — 读/写属性

读取或修改对象的属性值（GET 模式读，PUT 模式写）。

### 4. `PUT /remote/batch` — 批量执行

一次请求发送多个操作，减少网络往返。

### 5. `PUT /remote/search/assets` — 搜索资产

按名称、类型搜索 UE 内容资产。

```bash
curl -X PUT http://localhost:30010/remote/search/assets \
  -H "Content-Type: application/json" \
  -d '{"Filter":"BP_","ClassFilter":"Blueprint","MaxResults":10}'
```

### 6. `/remote/presets` — 预设管理

- `GET /remote/presets` — 列出所有预设
- `GET /remote/preset/:name` — 获取单个预设
- `PUT /remote/preset/transient` — 创建临时预设

预设用于暴露特定 Actor/属性/函数，方便快速访问。

### 7. `PUT /remote/object/thumbnail` — 获取缩略图

获取对象的预览缩略图。

## 能做什么

通过 Remote Control API + Python 脚本，可以远程控制 UE 编辑器执行：

- **执行任意 Python 代码**（通过 `ExecutePythonScript`）
- **创建/删除 Actor**（通过 `EditorActorSubsystem`）
- **读取/修改 Actor 属性**（位置、旋转、缩放、材质等）
- **搜索/加载/创建蓝图资产**
- **操作 Level**（新建、保存、加载）
- **调用 Blueprint 函数**
- **批量操作**（batch 模式）
- **获取对象元数据和缩略图**

## Python 脚本示例

```python
# skills/yh-test/scripts/ue_hello_world.py
import requests

def ue_exec(code: str, url: str = "http://localhost:30010") -> dict:
    return requests.put(
        f"{url}/remote/object/call",
        json={
            "objectPath": "/Script/PythonScriptPlugin.Default__PythonScriptLibrary",
            "functionName": "ExecutePythonScript",
            "parameters": {"PythonScript": code},
        },
        timeout=10,
    ).json()

# 执行多行 Python
ue_exec("""
import unreal
editor = unreal.EditorActorSubsystem()
actors = editor.get_all_level_actors()
print(f"Level has {len(actors)} actors")
""")
```

## 参考

- ue-cli 工具：`C:\Users\Administrator\AppData\Roaming\npm\node_modules\@banaba\ue-cli`
- [Remote Control API HTTP Reference](https://dev.epicgames.com/documentation/unreal-engine/remote-control-api-http-reference-for-unreal-engine)
- [Remote Control Quick Start](https://dev.epicgames.com/documentation/unreal-engine/remote-control-quick-start-for-unreal-engine)
- [Python Settings](https://dev.epicgames.com/documentation/unreal-engine/python-settings-in-the-unreal-engine-project-settings)
- [Scripting the Unreal Editor Using Python](https://dev.epicgames.com/documentation/unreal-engine/scripting-the-unreal-editor-using-python)
