#!/usr/bin/env python3
"""image_to_material.py

Run the Chord "Image -> PBR Material" ComfyUI workflow against a local
ComfyUI server. Takes a single input texture and produces five PBR maps:
basecolor, normal, roughness, metalness, height.

Uses only the Python standard library (urllib) so it is runnable without
extra dependencies.

Example:
    python image_to_material.py --input "C:/textures/brick.png"
    python image_to_material.py -i brick.png -o ./GeneratedImage --server 127.0.0.1:8900
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Skill 根目录（scripts/ 的上一级），输出统一放在 skill 根目录下的 GeneratedImage
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DEFAULT_WORKFLOW = os.path.join(SCRIPT_DIR, "workflow_api.json")
DEFAULT_OUTPUT_DIR = os.path.join(SKILL_DIR, "GeneratedImage")
DEFAULT_SERVER = "127.0.0.1:8900"

# Node ids in workflow_api.json.
LOAD_IMAGE_NODE = "10"
# SaveImage node id -> map type name (used to prefix saved files locally).
SAVE_NODES = {
    "4": "basecolor",
    "5": "normal",
    "6": "roughness",
    "7": "metalness",
    "9": "height",
}


def _http_request(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def upload_image(server, image_path):
    """Upload an image to ComfyUI's input folder via /upload/image (multipart)."""
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        file_bytes = f.read()

    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary = "----imageToMaterial" + uuid.uuid4().hex

    body = bytearray()
    body.extend(("--" + boundary + "\r\n").encode())
    body.extend(
        ('Content-Disposition: form-data; name="image"; filename="%s"\r\n' % filename).encode()
    )
    body.extend(("Content-Type: %s\r\n\r\n" % content_type).encode())
    body.extend(file_bytes)
    body.extend("\r\n".encode())
    # overwrite=true so re-running with the same filename is deterministic.
    body.extend(("--" + boundary + "\r\n").encode())
    body.extend('Content-Disposition: form-data; name="overwrite"\r\n\r\n'.encode())
    body.extend("true\r\n".encode())
    body.extend(("--" + boundary + "--\r\n").encode())

    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
    raw = _http_request(
        "http://%s/upload/image" % server, data=bytes(body), headers=headers, method="POST"
    )
    info = json.loads(raw.decode())
    # ComfyUI returns name + optional subfolder. LoadImage expects "subfolder/name".
    name = info["name"]
    subfolder = info.get("subfolder", "")
    return ("%s/%s" % (subfolder, name)) if subfolder else name


def queue_prompt(server, workflow, client_id):
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    headers = {"Content-Type": "application/json"}
    raw = _http_request(
        "http://%s/prompt" % server, data=payload, headers=headers, method="POST"
    )
    return json.loads(raw.decode())["prompt_id"]


def wait_for_history(server, prompt_id, timeout=600, poll_interval=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = _http_request("http://%s/history/%s" % (server, prompt_id))
        history = json.loads(raw.decode())
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            # 服务端工作流执行失败：立即抛出带错误信息的异常，避免误判为超时
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                raise RuntimeError(
                    "ComfyUI workflow execution failed: %s" % json.dumps(messages, ensure_ascii=False)
                )
            if status.get("completed") or "outputs" in entry:
                return entry
        time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for prompt %s to finish." % prompt_id)


def download_image(server, image_meta, dest_path):
    params = urllib.parse.urlencode(
        {
            "filename": image_meta["filename"],
            "subfolder": image_meta.get("subfolder", ""),
            "type": image_meta.get("type", "output"),
        }
    )
    raw = _http_request("http://%s/view?%s" % (server, params))
    with open(dest_path, "wb") as f:
        f.write(raw)


def _log(msg):
    """即时打印并 flush，保证进程被中途结束时也能看到进度，便于调试。"""
    print(msg, flush=True)


def run(input_image, output_dir, server, workflow_path):
    """Run the workflow end to end, printing each step immediately."""
    if not os.path.isfile(input_image):
        raise FileNotFoundError("Input image not found: %s" % input_image)

    os.makedirs(output_dir, exist_ok=True)

    with open(workflow_path, "r", encoding="utf-8") as f:
        workflow = json.load(f)

    _log("[1/4] Uploading input image: %s" % input_image)
    uploaded_name = upload_image(server, input_image)
    workflow[LOAD_IMAGE_NODE]["inputs"]["image"] = uploaded_name

    client_id = uuid.uuid4().hex
    _log("[2/4] Queuing workflow on http://%s ..." % server)
    prompt_id = queue_prompt(server, workflow, client_id)

    _log("[3/4] Waiting for generation (prompt_id=%s) ..." % prompt_id)
    entry = wait_for_history(server, prompt_id)
    outputs = entry.get("outputs", {})

    _log("[4/4] Downloading PBR maps to: %s" % output_dir)
    base_name = os.path.splitext(os.path.basename(input_image))[0]
    saved = []
    for node_id, map_type in SAVE_NODES.items():
        node_out = outputs.get(node_id, {})
        images = node_out.get("images", [])
        if not images:
            _log("  ! No output for %s (node %s)" % (map_type, node_id))
            continue
        for idx, image_meta in enumerate(images):
            ext = os.path.splitext(image_meta["filename"])[1] or ".png"
            suffix = "" if len(images) == 1 else "_%d" % idx
            dest = os.path.join(output_dir, "%s_%s%s%s" % (base_name, map_type, suffix, ext))
            download_image(server, image_meta, dest)
            saved.append(dest)
            _log("  - %s" % dest)

    if not saved:
        raise RuntimeError("Workflow finished but no images were produced.")
    _log("Done. %d files saved." % len(saved))
    return saved


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a texture into PBR maps via the Chord ComfyUI workflow."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to the input texture image.")
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for PBR maps (default: ./GeneratedImage).",
    )
    parser.add_argument(
        "-s",
        "--server",
        default=DEFAULT_SERVER,
        help="ComfyUI server address host:port (default: 127.0.0.1:8900).",
    )
    parser.add_argument(
        "-w",
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help="Path to the workflow API JSON (default: bundled workflow_api.json).",
    )
    args = parser.parse_args(argv)

    try:
        run(args.input, args.output, args.server, args.workflow)
    except urllib.error.URLError as exc:
        print(
            "ERROR: Could not reach ComfyUI at %s (%s).\n"
            "Make sure ComfyUI is running and listening on that address."
            % (args.server, exc),
            file=sys.stderr, flush=True,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the CLI user.
        print("ERROR: %s" % exc, file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    main()
