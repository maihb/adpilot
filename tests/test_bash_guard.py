"""命令守卫的测试。

守卫本身是一道门禁，所以它也得有门禁 —— 判错的两个方向代价都不小：**漏放**
只是多弹一次窗，**误放**会让一条写命令绕过权限系统（hook 的 allow 优先级高于
settings.json 的 deny），**误拦**则会卡住正常提交（gw-server 上线当天就发生过：
提交说明里写了守卫自己的名字，提交被自己拦下）。

模块从 `.claude/bash_guard.py` 按路径加载：它是给 hook 用的独立脚本，不属于
`adpilot` 包，不该为了测试把它挪进 `src/`。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

GUARD_PATH = Path(__file__).resolve().parents[1] / ".claude" / "bash_guard.py"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bash_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# ---------------------------------------------------------------------------
# 自动放行：只读命令
# ---------------------------------------------------------------------------

READONLY_COMMANDS = [
    "git status",
    "git log --oneline -10",
    "git --no-pager diff src/adpilot/config.py",
    "git show HEAD --stat",
    "ls -la src/adpilot",
    "rg 'SecretStr' src",
    "rg -n 'raw_reports' src | head -20",
    "wc -l src/adpilot/*.py",
    "uv tree",
    # flag 形态的版本查询。曾经漏判过：uv_ok() 只认子命令形态的 `uv version`，
    # 于是 `uv --version` 落回权限系统，把整条复合命令一起拖去弹窗。
    "uv --version",
    "uv -V",
    "uv --help",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy src tests",
    "uv run alembic current",
    # flag 形态的版本/帮助查询。与上面 `uv --version` 同一个坑：gh_ok() 按位置
    # 参数判定，会把这类 flag 一起滤掉，于是查个版本号也要弹窗。
    "gh --version",
    "gh --help",
    "gh pr merge --help",
    "gh run list --limit 5",
    "gh run view 12345 --log-failed",
    "gh pr checks",
    "gh pr diff 7",
    "gh workflow list",
    "gh run list --json conclusion,headBranch | jq '.[0]'",
    "docker compose ps",
    "docker compose logs --tail 50 api",
    "curl -fsS http://localhost:8000/api/health/live",
    # 复合形态才是这套判定存在的理由：前缀规则对它一律不生效，一律弹窗。
    "git log --format=%H -3 | while read -r h; do git show --stat $h; done",
    "for f in $(git ls-files 'src/**.py'); do wc -l $f; done",
    # 「uv 装哪了」的实际形态。一条命令里三个判定点：$() 在**参数**位（命令位由
    # 替换算出来才该落回询问）、2>/dev/null 是丢弃不是写、以及上面那个
    # `uv --version`。任一处判错，整条就退回弹窗。
    "which uv; uv --version; ls -la $(which uv) 2>/dev/null",
]


@pytest.mark.parametrize("cmd", READONLY_COMMANDS)
def test_readonly_commands_are_auto_allowed(cmd: str) -> None:
    assert guard.allowed(cmd), f"应自动放行却没放行：{cmd}"


# ---------------------------------------------------------------------------
# 不自动放行：任何写操作，以及判不出来的形态
# ---------------------------------------------------------------------------

NOT_READONLY_COMMANDS = [
    # 真的在写
    "uv sync --all-extras",
    "uv add httpx",
    "uv lock",
    # 子命令形态带位置参数就是在写 pyproject.toml，跟 `uv --version` 只差两个横杠
    "uv version 0.2.0",
    "uv run ruff format .",
    "uv run ruff check --fix .",
    "uv run pytest",
    "git commit -m '改点东西'",
    "docker compose up -d",
    "echo hi > /tmp/out.txt",
    "rm -rf build",
    "sed -i '' 's/a/b/' README.md",
    "find . -name '*.pyc' -delete",
    # gh 只认「子命令 + 动作」两级：第一级同名，第二级天差地别
    "gh pr merge 7 --squash",
    "gh workflow run ci.yml",
    "gh run rerun 12345",
    "gh issue close 3",
    "gh release create v0.1.0",
    "gh run download 12345",
    # gh auth token 会把凭据打进上下文 —— 撞的是硬规矩 1，不是「判不了」
    "gh auth token",
    "gh auth status --show-token",
    # gh api 的 GET 是读，但同一个子命令也能发 POST，不逐个 flag 猜
    "gh api repos/maihb/adpilot/actions/runs",
    # --web 会开浏览器，不算「看一眼」
    "gh pr view 7 --web",
    # 判不了目标，宁可多问一次
    "$(which ls) -la",
    "python3 -c 'import os; print(os.getcwd())'",
    "curl -X POST http://localhost:8000/api/reports",
    "curl -fsS https://example.com/data.json",
    "uv run python -c 'print(1)'",
    # 沾 .env 的一律不自动放行
    "grep POSTGRES .env",
]


@pytest.mark.parametrize("cmd", NOT_READONLY_COMMANDS)
def test_write_commands_are_not_auto_allowed(cmd: str) -> None:
    assert not guard.allowed(cmd), f"不该自动放行：{cmd}"


# ---------------------------------------------------------------------------
# 拦截：把 CLAUDE.md 里那几条硬规矩变成机器判定
# ---------------------------------------------------------------------------

BLOCKED_COMMANDS = [
    # 密钥不进上下文
    "cat .env",
    "head -5 .env",
    "tail -f .env.local",
    "less deploy/.env",
    # 密钥不进公开仓库的历史
    "git add .env",
    "git add config/.env",
    "git add server.pem",
    "git add -f dist/bundle.js",
    # 依赖只经 uv
    "pip install requests",
    "pip3 install -r requirements.txt",
    "python3 -m pip install httpx",
    # 命令只在项目环境里跑
    "pytest -q",
    "mypy src tests",
    "ruff check .",
    "cd /Users/x/code/adpilot && pytest tests/test_health.py",
    "python -m pytest",
    # 数据卷删了就没了
    "docker compose down -v",
]


@pytest.mark.parametrize("cmd", BLOCKED_COMMANDS)
def test_dangerous_commands_are_blocked(cmd: str) -> None:
    reason = guard.blocked(cmd)
    assert reason, f"应被拦截却放过了：{cmd}"
    assert len(reason.splitlines()) >= 2, "拦截理由必须给出正确做法，不能只说不行"


ALLOWED_THROUGH_COMMANDS = [
    # .env.example 只有键名和说明，本来就是给人读的
    "cat .env.example",
    "cp .env.example .env",
    # 正确姿势不该被自己的守卫挡住
    "uv run pytest -q",
    "uv run pytest -m integration",
    "uv run mypy src tests",
    "uv add --optional dev pytest-cov",
    "docker compose down",
    "git add src/adpilot/config.py",
    # 引号里的字样不是命令 —— 这条曾经在隔壁仓库真实误伤过一次提交
    "git commit -m '给 pytest 夹具加了 offline_client'",
    'echo "记得先 pip install 吗？不，用 uv add"',
]


@pytest.mark.parametrize("cmd", ALLOWED_THROUGH_COMMANDS)
def test_legitimate_commands_pass_through(cmd: str) -> None:
    assert guard.blocked(cmd) is None, f"误拦了正常命令：{cmd}"


def test_heredoc_body_is_not_parsed_as_commands() -> None:
    """heredoc 正文是数据不是命令，拿它当命令解析必然误伤。"""
    cmd = "\n".join(
        [
            "uv run python - <<'PY'",
            "print('pip install 只是这段文本里的字样')",
            "PY",
        ]
    )
    assert guard.blocked(cmd) is None
