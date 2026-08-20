#!/usr/bin/env bash
# Stop 钩子：改完一轮就把 CI 的门禁在本地先跑一遍。
#
# 存在的理由：这几条都跑在 CI 里（.github/workflows/ci.yml），但 CI 的反馈要等到
# 推上去之后。同一轮对话里跑掉，问题还在手边。
#
# 为什么 .md 也要管：`ruff format` 连 Markdown 里的 Python 代码块一起格式化，所以
# 一篇带代码块的文档同样能让 `format --check` 红。只改了文档就只跑这一条，不必陪
# 跑 mypy 和测试。
set -u
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
command -v uv >/dev/null 2>&1 || exit 0

changed() { [ -n "$(git status --porcelain -- "$1" 2>/dev/null)" ]; }

py_changed=0
md_changed=0
changed '*.py' && py_changed=1
changed '*.md' && md_changed=1
[ "$py_changed" -eq 1 ] || [ "$md_changed" -eq 1 ] || exit 0

LOG=/tmp/adpilot-stop-check.log
: >"$LOG"

parts=()
pass=1

run() {
    local label=$1
    shift
    if "$@" >>"$LOG" 2>&1; then
        parts+=("✅$label")
    else
        parts+=("❌$label")
        pass=0
    fi
}

run "format" uv run ruff format --check .
if [ "$py_changed" -eq 1 ]; then
    run "ruff" uv run ruff check .
    run "mypy" uv run mypy src tests
    run "imports" uv run lint-imports
    run "pytest" uv run pytest
fi

IFS=' ' summary="${parts[*]}"
if [ "$pass" -eq 1 ]; then
    printf '{"systemMessage":"Stop hook: %s"}\n' "$summary"
else
    printf '{"systemMessage":"Stop hook: %s  详情 %s"}\n' "$summary" "$LOG"
fi
