#!/usr/bin/env bash
# PostToolUse(Write|Edit) 钩子：刚写过的文件顺手 ruff format 一遍。
#
# 为什么 .md 也要格式化：ruff format 连 Markdown 里的 Python 代码块一起管，
# 所以一篇带代码块的文档同样能让 CI 的 `ruff format --check .` 红。就地格式化
# 掉，比事后回来找哪一处缩进不对便宜得多。
set -u
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v uv >/dev/null 2>&1 || exit 0

file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
case "$file" in
    *.py | *.md) ;;
    *) exit 0 ;;
esac

uv run ruff format "$file" >/dev/null 2>&1 || true
