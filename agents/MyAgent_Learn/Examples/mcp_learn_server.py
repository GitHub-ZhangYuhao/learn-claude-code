#!/usr/bin/env python3
"""
mcp_learn_server.py - Teaching MCP Server

Provides a set of simple and fun example tools for learning MCP integration.
Communicates via stdio using the JSON-RPC 2.0 protocol.
"""

import json
import sys
import os
import time
import random
import platform
import hashlib
from datetime import datetime

# ─── Tool Definitions ────────────────────────────────────

TOOLS = [
    {
        "name": "get_time",
        "description": "Get the current date and time (supports various formats)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "Time format: 'iso', 'chinese', 'unix'",
                    "default": "iso"
                }
            },
            "required": []
        }
    },
    {
        "name": "random_number",
        "description": "Generate random numbers within a specified range",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min": {"type": "integer", "description": "Minimum value", "default": 1},
                "max": {"type": "integer", "description": "Maximum value", "default": 100},
                "count": {"type": "integer", "description": "How many to generate", "default": 1}
            },
            "required": []
        }
    },
    {
        "name": "system_info",
        "description": "Get system info (platform, Python version, environment variables, etc.)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "description": "Detail level: 'basic', 'full'",
                    "default": "basic"
                }
            },
            "required": []
        }
    },
    {
        "name": "hash_string",
        "description": "Compute the hash of a string (MD5, SHA256)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to hash"},
                "algorithm": {
                    "type": "string",
                    "description": "Hash algorithm: 'md5', 'sha256'",
                    "default": "sha256"
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "chinese_zodiac",
        "description": "Look up the Chinese zodiac sign for a given year",
        "inputSchema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Year"}
            },
            "required": ["year"]
        }
    },
    {
        "name": "word_count",
        "description": "Count characters, words, and lines in text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to analyze"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "unit_convert",
        "description": "Simple unit conversion (temperature, length, weight)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "Value to convert"},
                "from_unit": {"type": "string", "description": "Source unit"},
                "to_unit": {"type": "string", "description": "Target unit"}
            },
            "required": ["value", "from_unit", "to_unit"]
        }
    },
    {
        "name": "quote_of_day",
        "description": "Get a daily quote (randomly selected)",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

# ─── Tool Handlers ───────────────────────────────────────

QUOTES = [
    {"text": "学而时习之，不亦说乎", "author": "Confucius"},
    {"text": "三人行，必有我师焉", "author": "Confucius"},
    {"text": "千里之行，始于足下", "author": "Laozi"},
    {"text": "知之为知之，不知为不知，是知也", "author": "Confucius"},
    {"text": "天行健，君子以自强不息", "author": "Book of Changes"},
    {"text": "工欲善其事，必先利其器", "author": "Confucius"},
    {"text": "海纳百川，有容乃大", "author": "Lin Zexu"},
    {"text": "路漫漫其修远兮，吾将上下而求索", "author": "Qu Yuan"},
]

ZODIAC = ["Monkey", "Rooster", "Dog", "Pig", "Rat", "Ox", "Tiger", "Rabbit", "Dragon", "Snake", "Horse", "Goat"]

def handle_get_time(args):
    fmt = args.get("format", "iso")
    now = datetime.now()
    if fmt == "iso":
        result = now.isoformat()
    elif fmt == "chinese":
        result = now.strftime("%Y-%m-%d %H:%M:%S")
    elif fmt == "unix":
        result = str(int(time.time()))
    else:
        result = now.strftime(fmt)
    return result

def handle_random_number(args):
    mn = args.get("min", 1)
    mx = args.get("max", 100)
    count = args.get("count", 1)
    if count == 1:
        return str(random.randint(mn, mx))
    return str([random.randint(mn, mx) for _ in range(count)])

def handle_system_info(args):
    detail = args.get("detail", "basic")
    info = {
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "cwd": os.getcwd(),
    }
    if detail == "full":
        info["processor"] = platform.processor()
        info["node"] = platform.node()
        info["path"] = os.environ.get("PATH", "")[:500]
    return json.dumps(info, ensure_ascii=False, indent=2)

def handle_hash_string(args):
    text = args.get("text", "")
    algo = args.get("algorithm", "sha256")
    if algo == "md5":
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
    else:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{algo}({text!r}) = {h}"

def handle_chinese_zodiac(args):
    year = args.get("year", 2024)
    idx = year % 12
    z = ZODIAC[idx]
    return f"The Chinese zodiac sign for {year} is {z}"

def handle_word_count(args):
    text = args.get("text", "")
    char_count = len(text)
    # Simple text analysis
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en_words = len(text.split())
    line_count = text.count('\n') + 1 if text else 0
    return json.dumps({
        "total_chars": char_count,
        "chinese_chars": cn_chars,
        "words": en_words,
        "lines": line_count,
    }, ensure_ascii=False, indent=2)

def handle_unit_convert(args):
    value = args.get("value", 0)
    from_unit = args.get("from_unit", "").lower()
    to_unit = args.get("to_unit", "").lower()

    conversions = {
        ("celsius", "fahrenheit"): lambda v: v * 9/5 + 32,
        ("fahrenheit", "celsius"): lambda v: (v - 32) * 5/9,
        ("celsius", "kelvin"): lambda v: v + 273.15,
        ("kelvin", "celsius"): lambda v: v - 273.15,
        ("meter", "foot"): lambda v: v * 3.28084,
        ("foot", "meter"): lambda v: v / 3.28084,
        ("kilometer", "mile"): lambda v: v * 0.621371,
        ("mile", "kilometer"): lambda v: v / 0.621371,
        ("kilogram", "pound"): lambda v: v * 2.20462,
        ("pound", "kilogram"): lambda v: v / 2.20462,
    }

    key = (from_unit, to_unit)
    if key in conversions:
        result = conversions[key](value)
        return f"{value} {from_unit} = {result:.4f} {to_unit}"
    return f"Unsupported conversion: {from_unit} -> {to_unit}. Supported pairs: {list(conversions.keys())}"

def handle_quote_of_day(args):
    q = random.choice(QUOTES)
    return f'"{q["text"]}" -- {q["author"]}'

HANDLERS = {
    "get_time": handle_get_time,
    "random_number": handle_random_number,
    "system_info": handle_system_info,
    "hash_string": handle_hash_string,
    "chinese_zodiac": handle_chinese_zodiac,
    "word_count": handle_word_count,
    "unit_convert": handle_unit_convert,
    "quote_of_day": handle_quote_of_day,
}

# ─── JSON-RPC 2.0 Server Loop ────────────────────────────

def run_server():
    """Main server loop: read JSON-RPC requests from stdin, write responses to stdout."""
    request_id = 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params", {})

        response = None

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-learn-server", "version": "1.0.0"},
                }
            }

        elif method == "notifications/initialized":
            # Notification, no response needed
            continue

        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOLS}
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            handler = HANDLERS.get(tool_name)

            if handler:
                try:
                    result_text = handler(tool_args)
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}]
                        }
                    }
                except Exception as e:
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -1, "message": str(e)}
                    }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                }

        elif method == "shutdown":
            break

        if response and msg_id is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    run_server()
