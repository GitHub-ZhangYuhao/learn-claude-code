---
name: image-to-material
description: 在本地 ComfyUI 服务上使用 Chord 工作流，将单张贴图转换为完整的 PBR 材质贴图集（basecolor、normal、roughness、metalness、height）。当用户希望把一张图片/贴图转换为 PBR 贴图，或从照片生成材质贴图时使用。
---

# 图片转材质 Skill（Image To Material）

你可以在本地 ComfyUI 服务上运行一个固定的 ComfyUI 工作流（**Chord** 模型），
把一张输入贴图转换为完整的 PBR 材质贴图集。

该工作流会生成五张贴图：
- **basecolor**（基础色）
- **normal**（法线）
- **roughness**（粗糙度）
- **metalness**（金属度）
- **height**（高度，通过 `ChordNormalToHeight` 从法线推导得到）

## 前置条件

- 本地 **ComfyUI** 服务正在运行，且可通过 `127.0.0.1:8900` 访问。
- ComfyUI 已安装 Chord 节点：`ChordLoadModel`、
  `ChordMaterialEstimation`、`ChordNormalToHeight`。
- ComfyUI 可访问检查点模型 `chord_v1.safetensors`。

## Skill 包含的文件

- `scripts/image_to_material.py` — 运行脚本（仅依赖标准库，无需额外依赖）。
- `scripts/workflow_api.json` — 固定的 ComfyUI API 工作流。节点 `10`（`LoadImage`）
  为输入节点，其 `image` 字段会在运行时由脚本自动设置。
- `GeneratedImage/` — 默认输出目录（位于 Skill 根目录）。

## 如何运行

```bash
python scripts/image_to_material.py --input "C:/path/to/texture.png"
```

执行流程：
1. 将输入图片上传到 ComfyUI（`/upload/image`）。
2. 用上传后的文件名替换 `scripts/workflow_api.json` 中节点 `10` 的输入。
3. 提交工作流（`/prompt`）并轮询 `/history/{prompt_id}` 直到完成。
4. 将全部五张贴图下载到 `./GeneratedImage`（位于本 Skill 目录下）。

输出文件命名为 `<输入图名>_<贴图类型>.png`，例如
`brick_basecolor.png`、`brick_normal.png` 等。

## 参数选项

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-i`, `--input` | （必填） | 输入贴图图片的路径。 |
| `-o`, `--output` | `./GeneratedImage` | PBR 贴图的输出目录。 |
| `-s`, `--server` | `127.0.0.1:8900` | ComfyUI 服务地址 `host:port`。 |
| `-w`, `--workflow` | `scripts/workflow_api.json` | 要使用的工作流 API JSON。 |

### 示例

```bash
# 默认服务地址（127.0.0.1:8900）和默认输出目录
python scripts/image_to_material.py -i "T:/textures/wood.jpg"

# 自定义输出目录
python scripts/image_to_material.py -i wood.jpg -o "D:/out/wood_pbr"

# 不同的 ComfyUI 地址
python scripts/image_to_material.py -i wood.jpg -s 127.0.0.1:8188
```

## 给助手的注意事项

- 运行前务必确认输入图片路径存在。
- 如果无法连接 ComfyUI，脚本会以退出码 `2` 退出并给出明确提示；
  此时应提醒用户启动 ComfyUI / 检查端口。
- 若要更改生成哪些贴图，需同时修改 `scripts/workflow_api.json` 和
  `scripts/image_to_material.py` 中的 `SAVE_NODES` 映射。
- 不要在 `scripts/workflow_api.json` 中硬编码新的输入文件名；占位符
  `PLACEHOLDER_INPUT_IMAGE` 会在运行时自动被替换。
