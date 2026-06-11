# -*- coding: utf-8 -*-
"""
websocket 测试公共夹具与路径配置
================================
本文件用途：保证测试既能以 `channel_pdd.*` 形式导入 websocket 服务内部模块，
又能以 `common.*` 形式导入公共库。为此把「websocket 服务目录」与「仓库根目录」
均加入 sys.path（与 main.py 中的 sys.path 注入口径一致）。
"""
import os
import sys

# websocket 服务目录（本文件父目录的父目录 = tests 的父目录 = websocket/）
_WS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 仓库根目录 = websocket 目录的父目录（用于 import common.*）
_REPO_ROOT = os.path.dirname(_WS_DIR)

for _path in (_WS_DIR, _REPO_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)
