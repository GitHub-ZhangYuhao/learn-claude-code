#!/usr/bin/env python3
"""
ImageToolManager.py - 图片生成工具

将文生图能力封装成 AgentTeam 标准工具（Schema + Handler），供后续挂载到 Agent。
使用独立的图片 API 客户端（image_api_key / image_base_url / image_model），
与主对话用的 LLM client 互不影响。

设计要点：
- Handler 必须返回字符串（会被当作 tool 消息的 content）。
- 生成的图片落盘到 generated_images/ 目录。
- 尺寸强制每边最小 1024。
- 提供 load_image_as_vision() 辅助，用于后续把生成图作为 vision 输入回喂给 Agent。
"""

import os
import base64
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

# 图片保存目录（相对本模块所在工程根目录，避免受 cwd 影响）
GENERATED_IMAGES_DIR = Path(__file__).resolve().parent / "generated_images"
# 每边最小尺寸
MIN_IMAGE_SIZE = 1024

# 支持的图片扩展名 -> MIME 类型（供 vision 回喂使用）
_MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def _build_image_client() -> OpenAI:
    """构建独立的图片 API 客户端。"""
    return OpenAI(
        api_key=os.getenv("image_api_key"),
        base_url=os.getenv("image_base_url"),
        default_headers={
            "X-TFY-METADATA": "{}",
            "X-TFY-LOGGING-CONFIG": '{"enabled": true}',
        },
    )


def _save_image_data(item, base_name: str, index: int, total: int) -> str:
    """将单条 image 数据落盘，返回保存的绝对路径；失败返回空字符串。"""
    GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    # 多张图加序号后缀
    suffix = f"_{index + 1}" if total > 1 else ""
    file_path = GENERATED_IMAGES_DIR / f"{base_name}{suffix}.png"

    # 优先 b64，其次 url
    if getattr(item, "b64_json", None):
        image_bytes = base64.b64decode(item.b64_json)
        file_path.write_bytes(image_bytes)
        return str(file_path)

    if getattr(item, "url", None):
        # 仅返回 URL（不下载），由调用方决定如何处理
        return ""

    return ""


def generate_image(prompt: str, size_x: int, size_y: int,
                   generate_num: int = 1, image_name: str = None) -> str:
    """
    根据 prompt 生成图片并落盘到 generated_images/。

    Args:
        prompt: 图片描述（必选）
        size_x: 宽度（必选，最小 1024）
        size_y: 高度（必选，最小 1024）
        generate_num: 生成数量（可选，默认 1）
        image_name: 文件名（可选，不传则按时间戳自动生成）

    Returns:
        字符串：生成结果说明（保存路径 / URL / 错误信息）
    """
    if not prompt or not str(prompt).strip():
        return "错误：prompt 不能为空"

    # 尺寸强制最小 1024
    clamped = []
    if size_x < MIN_IMAGE_SIZE:
        clamped.append(f"宽 {size_x}->{MIN_IMAGE_SIZE}")
        size_x = MIN_IMAGE_SIZE
    if size_y < MIN_IMAGE_SIZE:
        clamped.append(f"高 {size_y}->{MIN_IMAGE_SIZE}")
        size_y = MIN_IMAGE_SIZE

    generate_num = max(1, int(generate_num or 1))
    base_name = image_name.strip() if image_name and image_name.strip() else \
        f"image_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        client = _build_image_client()
        response = client.images.generate(
            model=os.getenv("image_model"),
            prompt=prompt,
            n=generate_num,
            size=f"{size_x}x{size_y}",
        )
    except Exception as e:
        return f"图片生成失败：{e}"

    saved_paths = []
    urls = []
    total = len(response.data)
    for i, item in enumerate(response.data):
        path = _save_image_data(item, base_name, i, total)
        if path:
            saved_paths.append(path)
        elif getattr(item, "url", None):
            urls.append(item.url)

    if not saved_paths and not urls:
        return "图片生成失败：API 未返回可用的图片数据（无 b64_json 也无 url）"

    lines = [f"已生成 {total} 张图片（尺寸 {size_x}x{size_y}）："]
    for p in saved_paths:
        lines.append(f"- 已保存图片到路径: {p}")
    for u in urls:
        lines.append(f"- URL: {u}")
    if clamped:
        lines.append(f"（尺寸已自动调整至最小 {MIN_IMAGE_SIZE}: {', '.join(clamped)}）")
    return "\n".join(lines)


def load_image_as_vision(file_path: str) -> dict:
    """
    将本地图片转换为 OpenAI vision 格式的 image_url dict。
    供后续把生成的图作为多模态输入回喂给 Agent 使用。
    """
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"图片文件不存在: {file_path}")
    ext = Path(file_path).suffix.lower()
    mime = _MIME_MAP.get(ext, "image/png")
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


# 落盘路径在 generate_image 返回字符串中的行前缀
_SAVED_LINE_PREFIX = "- 已保存: "


def extract_saved_paths(tool_output: str) -> list:
    """从 generate_image 的返回字符串中解析出已落盘的图片路径。"""
    if not tool_output:
        return []
    paths = []
    for line in tool_output.splitlines():
        line = line.strip()
        if line.startswith(_SAVED_LINE_PREFIX):
            paths.append(line[len(_SAVED_LINE_PREFIX):].strip())
    return paths


def build_vision_feedback_message(tool_output: str) -> dict | None:
    """
    根据 generate_image 的返回结果构建一条带图片的 user 消息（vision 回喂）。
    让 Agent 能"看到"自己刚生成的图，便于评估/迭代。无可用图片则返回 None。
    """
    paths = extract_saved_paths(tool_output)
    images = []
    for p in paths:
        try:
            images.append(load_image_as_vision(p))
        except FileNotFoundError:
            continue
    if not images:
        return None
    text = {"type": "text", "text": "以下是你刚刚生成的图片，请查看效果。如果不符合预期，可以调整 prompt 重新生成。"}
    return {"role": "user", "content": [text] + images}


# ---------------- 工具 Schema 与 Handler ----------------

IMAGE_GENERATION_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "根据文字描述生成图片并保存到本地 generated_images/ 目录。"
                "返回保存路径。适用于需要生成插画、UI、概念图等场景。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "图片内容的文字描述，越具体越好。",
                    },
                    "size_x": {
                        "type": "integer",
                        "description": "图片宽度（像素），最小 1024。",
                    },
                    "size_y": {
                        "type": "integer",
                        "description": "图片高度（像素），最小 1024。",
                    },
                    "generate_num": {
                        "type": "integer",
                        "description": "可选，生成数量，默认 1。",
                    },
                    "image_name": {
                        "type": "string",
                        "description": "可选，保存文件名（不含扩展名），不传则按时间戳自动生成。",
                    },
                },
                "required": ["prompt", "size_x", "size_y"],
            },
        },
    }
]

IMAGE_GENERATION_TOOL_HANDLERS = {
    "generate_image": lambda **kw: generate_image(
        kw["prompt"], kw["size_x"], kw["size_y"],
        kw.get("generate_num", 1), kw.get("image_name"),
    ),
}


if __name__ == "__main__":
    # 调用主工具 generate_image 生成一张图片
    result = generate_image(
        prompt="a simple cute cartoon cat sitting, flat design, white background",
        size_x=1024,
        size_y=1024,
        image_name="unittest_image",
    )
    print(result)
