#!/usr/bin/env bash
# PreToolUse(Bash) 钩子的薄壳：把命令原文喂给 bash_guard.py，原样转达它的决策。
#
# 判断逻辑一行都不写在这里 —— 放在 Python 里才测得动（tests/test_bash_guard.py
# 跟着 CI 跑）。协议见 bash_guard.py 的模块 docstring。
set -u

command -v python3 >/dev/null 2>&1 || exit 0   # 没有解释器就别把每次调用都变成报错

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

printf '%s' "$cmd" | python3 "${CLAUDE_PROJECT_DIR:-.}/.claude/bash_guard.py"
