---
name: python_encoding_utf8
description: 使用 pathlib 读写文件时必须指定 encoding="UTF-8"
type: feedback
---
在 Python 中使用 pathlib 的 read_text() 和 write_text() 时，必须显式指定 encoding="UTF-8" 参数，否则 Windows 系统默认使用 GBK 编码，会导致读取/写入中文内容时崩溃。今后凡遇到类似代码都要按此规范处理。
