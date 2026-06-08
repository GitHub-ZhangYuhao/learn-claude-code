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
# 尺寸必须能被该值整除（OpenAI Image2 的硬性要求）
SIZE_DIVISOR = 16
# 触发最小分辨率 / 整除限制的图像模型关键字（OpenAI Image2 系列）
_OPENAI_IMAGE2_KEYWORDS = ("image2", "image-2", "gpt-image")


def _is_openai_image2_model(model: str = None) -> bool:
    """判断当前图像模型是否为 OpenAI Image2 系列（需要最小 1024 且能被 16 整除）。"""
    model = (model or os.getenv("image_model") or "").lower()
    return any(k in model for k in _OPENAI_IMAGE2_KEYWORDS)


# Gemini 图像模型支持的宽高比档位（用于图生图：edit 端点忽略 size，只认 aspect_ratio）
_GEMINI_ASPECT_RATIOS = [
    (1, 1), (2, 3), (3, 2), (3, 4), (4, 3),
    (4, 5), (5, 4), (9, 16), (16, 9), (21, 9),
]


def _nearest_aspect_ratio(size_x: int, size_y: int) -> str:
    """把请求尺寸换算成最接近的 Gemini 支持宽高比字符串，如 "21:9"。"""
    size_x, size_y = int(size_x), int(size_y)
    if size_x <= 0 or size_y <= 0:
        return "1:1"
    target = size_x / size_y
    best = min(_GEMINI_ASPECT_RATIOS, key=lambda r: abs(target - r[0] / r[1]))
    return f"{best[0]}:{best[1]}"


def _normalize_size(size_x: int, size_y: int):
    """规整尺寸。

    仅当图像模型为 OpenAI Image2 系列时，强制每边 >= MIN_IMAGE_SIZE 且能被
    SIZE_DIVISOR 整除；否则直接使用传入的 size_x / size_y。

    返回 (size_x, size_y, clamped)，clamped 为可读的调整说明列表。
    """
    size_x, size_y = int(size_x), int(size_y)
    # 非 OpenAI Image2 模型：使用默认尺寸，不做任何限制
    if not _is_openai_image2_model():
        return size_x, size_y, []

    clamped = []

    def _fix(value, label):
        original = value
        if value < MIN_IMAGE_SIZE:
            value = MIN_IMAGE_SIZE
        # 四舍五入到最近的 SIZE_DIVISOR 倍数，并保证不低于最小值
        value = max(MIN_IMAGE_SIZE, round(value / SIZE_DIVISOR) * SIZE_DIVISOR)
        if value != original:
            clamped.append(f"{label} {original}->{value}")
        return value

    return _fix(size_x, "宽"), _fix(size_y, "高"), clamped


# 落盘路径在 generate_image 返回字符串中的行前缀
_SAVED_LINE_PREFIX = "- 已保存图片到路径: "

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

    # 尺寸规整：仅 OpenAI Image2 限制最小 1024 且能被 16 整除，其它模型用默认尺寸
    size_x, size_y, clamped = _normalize_size(size_x, size_y)

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
        lines.append(f"{_SAVED_LINE_PREFIX}{p}")
    for u in urls:
        lines.append(f"- URL: {u}")
    if clamped:
        lines.append(f"（尺寸已自动规整为最小 {MIN_IMAGE_SIZE} 且能被 {SIZE_DIVISOR} 整除: {', '.join(clamped)}）")
    return "\n".join(lines)


def generate_image_from_image(prompt: str, image_paths, size_x: int, size_y: int,
                              generate_num: int = 1, image_name: str = None) -> str:
    """
    以一张或多张输入图片为基础，结合 prompt 生成新图片（图生图）并落盘到 generated_images/。

    Args:
        prompt: 修改/生成描述（必选）
        image_paths: 输入图片路径，可以是单个字符串或字符串列表（必选）
        size_x: 宽度（必选，最小 1024）
        size_y: 高度（必选，最小 1024）
        generate_num: 生成数量（可选，默认 1）
        image_name: 文件名（可选，不传则按时间戳自动生成）

    Returns:
        字符串：生成结果说明（保存路径 / URL / 错误信息）
    """
    if not prompt or not str(prompt).strip():
        return "错误：prompt 不能为空"

    # 统一成列表，并校验文件存在
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    if not image_paths:
        return "错误：image_paths 不能为空"

    resolved_paths = []
    for p in image_paths:
        p = os.path.expanduser(str(p).strip())
        if not os.path.isfile(p):
            return f"错误：输入图片不存在: {p}"
        resolved_paths.append(p)

    # 尺寸规整：仅 OpenAI Image2 限制最小 1024 且能被 16 整除，其它模型用默认尺寸
    size_x, size_y, clamped = _normalize_size(size_x, size_y)

    generate_num = max(1, int(generate_num or 1))
    base_name = image_name.strip() if image_name and image_name.strip() else \
        f"image2image_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 图生图尺寸控制：
    # - OpenAI Image2 的 edit 端点支持 size，直接传像素尺寸；
    # - 其它模型（如 Gemini）的 edit 端点忽略 size，只认顶层 aspect_ratio。
    aspect_ratio = None
    edit_kwargs = {}
    if _is_openai_image2_model():
        edit_kwargs["size"] = f"{size_x}x{size_y}"
    else:
        aspect_ratio = _nearest_aspect_ratio(size_x, size_y)
        edit_kwargs["extra_body"] = {"aspect_ratio": aspect_ratio}

    file_handles = []
    try:
        client = _build_image_client()
        file_handles = [open(p, "rb") for p in resolved_paths]
        # 单张图传文件对象，多张图传列表（兼容 OpenAI images.edit 接口）
        image_arg = file_handles[0] if len(file_handles) == 1 else file_handles
        # **edit_kwargs：把字典里的每个 key 展开成独立的关键字参数传给 edit。
        # 即 {"size": ...} -> size=...，{"extra_body": ...} -> extra_body=...，
        # 用解包实现“按需传 size 或 aspect_ratio”，避免传入多余/None 参数。
        response = client.images.edit(
            model=os.getenv("image_model"),
            image=image_arg,
            prompt=prompt,
            n=generate_num,
            **edit_kwargs,
        )
    except Exception as e:
        return f"图生图失败：{e}"
    finally:
        for fh in file_handles:
            try:
                fh.close()
            except Exception:
                pass

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
        return "图生图失败：API 未返回可用的图片数据（无 b64_json 也无 url）"

    if aspect_ratio is not None:
        size_desc = f"宽高比 {aspect_ratio}，实际分辨率由模型决定"
    else:
        size_desc = f"尺寸 {size_x}x{size_y}"
    lines = [f"已基于 {len(resolved_paths)} 张输入图生成 {total} 张图片（{size_desc}）："]
    for p in saved_paths:
        lines.append(f"{_SAVED_LINE_PREFIX}{p}")
    for u in urls:
        lines.append(f"- URL: {u}")
    if clamped:
        lines.append(f"（尺寸已自动规整为最小 {MIN_IMAGE_SIZE} 且能被 {SIZE_DIVISOR} 整除: {', '.join(clamped)}）")
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


# 入站图片（如 Slack 上传）落盘目录
UPLOADS_DIR = Path(__file__).resolve().parent / "uploads"
# data URL 的 mime -> 扩展名
_DATA_URL_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}


def save_data_url_image(data_url: str, name_prefix: str = "upload") -> str | None:
    """把 data:image/...;base64,xxx 形式的图片落盘到 uploads/，返回绝对路径。

    解析失败或不是 data URL（如普通 http url）时返回 None。
    """
    if not data_url or not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, b64 = data_url.split(",", 1)
        mime = header[len("data:"):].split(";")[0].strip().lower()
        ext = _DATA_URL_EXT.get(mime, ".png")
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{name_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        path = UPLOADS_DIR / fname
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return str(path)
    except Exception:
        return None


def load_images_as_vision(paths) -> list:
    """把一组本地图片路径转成 vision 格式列表，自动跳过不存在/加载失败的项。"""
    if isinstance(paths, str):
        paths = [paths]
    images = []
    for p in paths or []:
        try:
            images.append(load_image_as_vision(p))
        except Exception:
            continue
    return images


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
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_from_image",
            "description": (
                "图生图：以一张或多张本地输入图片为基础，结合文字描述生成新图片，"
                "并保存到本地 generated_images/ 目录，返回保存路径。"
                "适用于风格迁移、局部修改、参考图重绘、图片融合等场景。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "希望如何修改/生成的文字描述，越具体越好。",
                    },
                    "image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "输入参考图片的本地路径列表（至少一张）。",
                    },
                    "size_x": {
                        "type": "integer",
                        "description": "输出图片宽度（像素），最小 1024。",
                    },
                    "size_y": {
                        "type": "integer",
                        "description": "输出图片高度（像素），最小 1024。",
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
                "required": ["prompt", "image_paths", "size_x", "size_y"],
            },
        },
    },
]

IMAGE_GENERATION_TOOL_HANDLERS = {
    "generate_image": lambda **kw: generate_image(
        kw["prompt"], kw["size_x"], kw["size_y"],
        kw.get("generate_num", 1), kw.get("image_name"),
    ),
    "generate_image_from_image": lambda **kw: generate_image_from_image(
        kw["prompt"], kw["image_paths"], kw["size_x"], kw["size_y"],
        kw.get("generate_num", 1), kw.get("image_name"),
    ),
}


if __name__ == "__main__":
    # 调用主工具 generate_image 生成一张图片
    # result = generate_image(
    #     prompt="a simple cute cartoon cat sitting, flat design, white background",
    #     size_x=1024,
    #     size_y=1024,
    #     image_name="unittest_image",
    # )
    # print(result)

    # 调用图生图工具 generate_image_from_image：给黑白稿上色
    result_i2i = generate_image_from_image(
        prompt="给这个黑白稿上色",
        image_paths=r"T:\Workspace\learn-claude-code\AIAgentIMP\generated_images\image_Test.png",
        size_x=1091,
        size_y=515,
        image_name="unittest_image2image",
    )
    print(result_i2i)
