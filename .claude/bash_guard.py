#!/usr/bin/env python3
"""PreToolUse(Bash) 守卫：拦掉会造成真实损失的命令，放行纯只读的命令。

被 `bash-guard.sh` 以「命令原文进 stdin」的方式调用，用退出码表达决策：

* **2** —— 拦截。stderr 是给模型看的理由，所以每条都必须写清**正确做法**，
  而不只是说「不行」。
* **0 且 stdout 有 JSON** —— 自动放行。
* **0 且无输出** —— 不表态，交回权限系统正常询问。

两件事分开写，各自可单测（`tests/test_bash_guard.py`，跟着 CI 跑）：

`blocked()` 把仓库里那几条「破了代价大、review 时又难发现」的规矩变成机器
拦截 —— 密钥不进 context、依赖只经 uv 装、命令只在项目环境里跑。

`allowed()` 消的是另一类麻烦：Claude Code 的权限前缀规则（`Bash(git log:*)`）
对**含变量展开、命令替换、管道循环的复合命令**一律不生效，一律弹窗，而排查
问题时最常用的偏偏就是这种形态。这里按「拆出每个命令位置的可执行名，全部在
只读白名单里才放行」把那类弹窗消掉。

放行的边界是**只读**，不是「看起来安全」：任何写重定向、任何不认识的命令名、
任何命令位置上的变量，都直接落回询问。宁可多问一次，不可放行一次写操作 ——
hook 的 allow 会绕过 settings.json 的 deny，判错的代价不对称。
"""

from __future__ import annotations

import re
import sys

# --------------------------------------------------------------------------
# 只读白名单
# --------------------------------------------------------------------------

# 凡是能写文件、能执行任意子命令的一律不在此列：python/xargs/tee/env（能跑任意
# 命令）、cp/mv/rm/chmod（写）、make/ssh/uvicorn（有副作用）。它们仍走
# settings.json 的正常规则。
#
# ruff / mypy / alembic 列在这里指的是 `uv run ruff …` 这种形态 —— 裸着跑会被
# blocked() 先一步拦下，理由见 PROJECT_TOOLS。
READONLY = {
    "ls",
    "rg",
    "grep",
    "egrep",
    "fgrep",
    "find",
    "wc",
    "head",
    "tail",
    "sed",
    "awk",
    "tr",
    "cut",
    "sort",
    "uniq",
    "nl",
    "rev",
    "column",
    "comm",
    "diff",
    "jq",
    "yq",
    "echo",
    "printf",
    "pwd",
    "date",
    "basename",
    "dirname",
    "stat",
    "file",
    "which",
    "type",
    "command",
    "uname",
    "sw_vers",
    "hostname",
    "whoami",
    "id",
    "seq",
    "expr",
    "read",
    "true",
    "false",
    "test",
    "[",
    "ps",
    "lsof",
    "netstat",
    "df",
    "du",
    "git",
    "docker",
    "uv",
    "ruff",
    "mypy",
    "alembic",
    "curl",
}

# git 的只读子命令。写类子命令（commit/push/merge/checkout/reset…）不在此列，
# 它们本来就有各自的 allow 规则，走正常流程即可。
GIT_READONLY = {
    "log",
    "show",
    "diff",
    "status",
    "branch",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "blame",
    "shortlog",
    "describe",
    "cat-file",
    "count-objects",
    "reflog",
    "whatchanged",
    "grep",
    "name-rev",
    "merge-base",
    "check-ignore",
    "for-each-ref",
    "stash",
}

# docker 的只读子命令。run/exec/rm/build/compose up 一律不在此列。
DOCKER_READONLY = {
    "ps",
    "images",
    "logs",
    "inspect",
    "stats",
    "version",
    "info",
    "port",
    "top",
    "diff",
    "history",
    "events",
    "config",
}

# uv 的只读子命令。sync/add/remove/lock/build/publish 会写 .venv、pyproject
# 或 uv.lock，不在此列。
UV_PIP_READONLY = {"list", "show", "freeze", "tree", "check"}
UV_PYTHON_READONLY = {"list", "find", "dir"}

# Alembic 的只读子命令。upgrade/downgrade/revision/stamp 会动数据库或写迁移
# 文件。迁移随 D3 的领域模型才引入，提前列在这里是因为漏了它只会天天弹窗。
ALEMBIC_READONLY = {"current", "history", "heads", "branches", "show"}

# curl 里带上这些就不是「读一下」了：写盘、上传、发数据。
CURL_WRITE_FLAGS = {
    "-o",
    "--output",
    "-O",
    "--remote-name",
    "-T",
    "--upload-file",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-F",
    "--form",
    "--form-string",
    "-K",
    "--config",
}
LOCAL_URL = re.compile(
    r"^(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?:/|$)"
)

KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "while",
    "until",
    "do",
    "done",
    "case",
    "esac",
    "!",
    "{",
    "}",
    "(",
    ")",
    "time",
    "function",
    "select",
    "coproc",
    "[[",
    "]]",
}

# 允许的重定向形态：只有丢弃输出和合并 fd，写文件一律不放行。
REDIRECT_OK = re.compile(r"(?:\d?>>?\s*/dev/null|\d?>&\d|<\s*/dev/\w+)")


# --------------------------------------------------------------------------
# 拦截规则
# --------------------------------------------------------------------------

# 装在项目 .venv 里、必须经 uv run 才拿得到正确解释器与依赖的命令。
PROJECT_TOOLS = {"pytest", "mypy", "ruff", "uvicorn", "celery", "alembic"}

# 会把 .env 正文吐进上下文的命令。`cp .env.example .env` 不在此列 —— 那是写，
# 而且是 README 的第一步。
ENV_READERS = {
    "cat",
    "less",
    "more",
    "head",
    "tail",
    "bat",
    "strings",
    "od",
    "xxd",
    "nl",
    "view",
    "open",
}

# 密钥类文件。git 认下它们等于永久留在公开仓库的历史里。
SECRET_PATH = re.compile(r"(^|[\s\"'=/])(\.env(?!\.example)|.*\.pem|.*\.key|credentials\.json)")


def _read_env_reason(name: str) -> str:
    return f"""守卫拦截：不要用 {name} 读 .env。
  想知道有哪些配置项：读 .env.example（只有键名和说明，没有值）
  想确认某个值是否生效：起服务看日志，或读 src/adpilot/config.py
原因：.env 里是真实凭据，一旦进了对话上下文，它就会躺在会话记录、日志和任何
后续摘要里。本仓库是公开仓库，密钥泄漏没有「以后再清理」这一说。"""


GIT_ADD_SECRET_REASON = """守卫拦截：不要把凭据类文件交给 git。
  要看配置项去 .env.example；要改配置改本机的 .env（它已被 gitignore）
原因：这是公开仓库。git 历史会留住你之后删掉的东西，真要清干净得重写每一个
commit hash。见 CLAUDE.md 不可协商的规矩第 1 条。"""

GIT_ADD_FORCE_REASON = """守卫拦截：不要用 git add -f。
  正常提交用 git add <path>；确实被误忽略了就去改 .gitignore
原因：-f 的作用就是绕过 .gitignore，而这个仓库的 .gitignore 挡着的正是 .env、
*.pem、*.key 这些不能进公开历史的东西。"""

PIP_INSTALL_REASON = """守卫拦截：不要用 pip 装依赖。
  加运行时依赖：uv add <包>
  加开发依赖：uv add --optional dev <包>
  只是想装上已声明的依赖：uv sync --all-extras
原因：pip 装进当前解释器，不写 pyproject.toml 也不更新 uv.lock。本机能跑、CI
装不到 —— 这类差异要到 CI 红了才发现。"""

DOWN_VOLUMES_REASON = """守卫拦截：docker compose down -v 会删掉数据卷。
  只停容器：docker compose down
  确实要清库重来：先说清楚要清哪个，再单独删那一个卷
原因：-v 连 postgres_data / mongo_data / rabbitmq_data 一起删，导进去的报表和
归一化结果全没了，而原始快照本来是拿来重跑归一化的。"""


def _bare_tool_reason(name: str) -> str:
    return f"""守卫拦截：不要直接跑 {name}，走 uv run。
  uv run {name} ...
  常用的几条见 CLAUDE.md「常用命令」
原因：依赖装在 uv 管理的 .venv 里，裸命令用的是系统解释器 —— 要么报模块找不
到，要么用上另一个版本的工具，两种都跟 CI 的结论对不上。"""


# --------------------------------------------------------------------------
# 命令解析
# --------------------------------------------------------------------------


def strip_heredocs(text: str) -> str:
    """去掉 heredoc 的正文，只留调用行。

    正文里的内容不是命令，把它交给下面的解析只会误伤 —— 提交说明里写了
    `pytest` 字样就被自己的守卫拦下，这种事真发生过。
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", line)
        out.append(line)
        if match:
            delimiter = match.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
        i += 1
    return "\n".join(out)


def strip_quotes(text: str) -> str:
    """剥掉引号内的文本，防止其中的 | ; & 被当成分隔符。"""
    return re.sub(r"'[^']*'|\"[^\"]*\"", " ", text)


def substitutions(text: str) -> tuple[str, list[str]]:
    """摘出 $(...) 与 `...` 的内部内容，返回 (剥净的主串, [内部串...])。"""
    inner: list[str] = []
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("$(", j):
                    depth, j = depth + 1, j + 2
                    continue
                if text[j] == ")":
                    depth -= 1
                elif text[j] == "(":
                    depth += 1
                j += 1
            inner.append(text[i + 2 : j - 1])
            out.append(" __SUBST__ ")
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                return "".join(out) + text[i:], inner
            inner.append(text[i + 1 : j])
            out.append(" __SUBST__ ")
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out), inner


def head_token(segment: str) -> str | None:
    """取一段里命令位置的 token，取不到返回 None（纯赋值、纯关键字）。"""
    tokens = segment.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in KEYWORDS:
            i += 1
            continue
        if tok in ("for", "case", "select"):
            return None  # `for f in *.py` 这段没有命令位置，循环体在后面的段里
        if re.fullmatch(r"[A-Za-z_]\w*=.*", tok):  # FOO=bar 前缀赋值
            i += 1
            continue
        return tok
    return None


def commands(cmd: str) -> list[list[str]]:
    """把一条 bash 命令拆成若干「命令位置开头的 token 列表」。

    引号内容与 heredoc 正文在这之前已经剥掉，所以这里看到的每个 token 都真的
    出现在命令行上，不是某段文本里的字样。
    """
    result: list[list[str]] = []
    main, inner = substitutions(strip_heredocs(cmd))
    for piece in [main, *inner]:
        for segment in re.split(r"\|\||&&|[|;&\n]", strip_quotes(piece)):
            tokens = segment.split()
            if not tokens:
                continue
            head = head_token(segment)
            if head is None:
                continue
            result.append(tokens[tokens.index(head) :])
    return result


def _base(name: str) -> str:
    """取命令名本身，去掉路径与 python3 之类的版本后缀。"""
    return name.rsplit("/", 1)[-1]


def _unwrap(tokens: list[str]) -> list[str]:
    """剥掉 `uv run` 前缀，返回真正被执行的那条命令。

    `uv run -- pytest` 与 `uv run pytest` 都要认得出；带其它选项的
    （`uv run --with X`）不认，交回上层按「判不了」处理。
    """
    if len(tokens) >= 2 and _base(tokens[0]) == "uv" and tokens[1] == "run":
        rest = tokens[2:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return rest
    return tokens


# --------------------------------------------------------------------------
# 拦截判定
# --------------------------------------------------------------------------


def blocked(cmd: str) -> str | None:
    """返回拦截理由；不该拦就返回 None。"""
    for tokens in commands(cmd):
        name = _base(tokens[0])
        rest = tokens[1:]

        if name in ENV_READERS and any(
            arg.startswith(".env") and not arg.startswith(".env.example")
            for arg in (a.rsplit("/", 1)[-1] for a in rest)
        ):
            return _read_env_reason(name)

        if name == "git" and rest[:1] == ["add"]:
            args = rest[1:]
            if any(a in ("-f", "--force") for a in args):
                return GIT_ADD_FORCE_REASON
            if any(SECRET_PATH.search(a) for a in args):
                return GIT_ADD_SECRET_REASON

        if name in ("pip", "pip3") and rest[:1] in (["install"], ["uninstall"]):
            return PIP_INSTALL_REASON
        if name.startswith("python") and rest[:2] == ["-m", "pip"]:
            return PIP_INSTALL_REASON

        if (
            name == "docker"
            and rest[:2] == ["compose", "down"]
            and any(a in ("-v", "--volumes") for a in rest[2:])
        ):
            return DOWN_VOLUMES_REASON

        # 命令位置上就是工具名本身，说明没经过 uv run —— `uv run pytest` 的
        # 命令位置是 uv，走不到这里。
        if name in PROJECT_TOOLS:
            return _bare_tool_reason(name)
        if (
            name.startswith("python")
            and rest[:1] == ["-m"]
            and len(rest) > 1
            and rest[1] in PROJECT_TOOLS
        ):
            return _bare_tool_reason(rest[1])

    return None


# --------------------------------------------------------------------------
# 只读判定
# --------------------------------------------------------------------------


def git_ok(tokens: list[str]) -> bool:
    """git 的参数级判定：子命令必须只读，且不得带 -C（deny 里禁的）。"""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-C":
            return False
        if tok in ("--no-pager", "-P", "--paginate"):
            i += 1
            continue
        if tok == "-c":  # -c key=val
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub, rest = tok, tokens[i + 1 :]
        if sub == "config":
            return any(r.startswith("--get") for r in rest)
        if sub == "remote":
            return all(r in ("-v", "--verbose", "show", "get-url") for r in rest)
        if sub == "branch":  # 不带写类开关才是只读
            return not any(
                r in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move") for r in rest
            )
        if sub == "stash":  # 只放行 list/show，push/pop 会动工作区
            return bool(rest) and rest[0] in ("list", "show")
        return sub in GIT_READONLY
    return False


def uv_ok(rest: list[str]) -> bool:
    """uv 的参数级判定。`uv run X` 由调用方先剥掉，走不到这里。"""
    if not rest:
        return False
    sub = rest[0]
    args = rest[1:]
    if sub == "pip":
        return bool(args) and args[0] in UV_PIP_READONLY
    if sub == "python":
        return bool(args) and args[0] in UV_PYTHON_READONLY
    if sub == "lock":
        return any(a in ("--check", "--dry-run") for a in args)
    if sub == "version":
        # `uv version 1.2.3` 会改写 pyproject.toml，只有纯查询才是读。
        return all(a.startswith("-") for a in args)
    return sub in ("tree", "help")


def ruff_ok(rest: list[str]) -> bool:
    """ruff 的参数级判定：默认写、显式只读才放行。"""
    if not rest:
        return False
    sub = rest[0]
    if sub == "check":
        return not any(a in ("--fix", "--fix-only", "--unsafe-fixes") for a in rest)
    if sub == "format":
        # 裸 ruff format 会原地改写文件，带 --check / --diff 才是只看。
        return any(a in ("--check", "--diff") for a in rest)
    return sub in ("rule", "linter", "config", "version", "--version", "help")


def curl_ok(rest: list[str]) -> bool:
    """只放行「打本机的 GET」：本地服务探测无副作用，出网或写盘的不放。

    URL 必须是**字面**的本地地址。变量拼出来的（curl "$BASE/x"）判不了目标，
    一律落回询问 —— 引号内容在解析前就被剥掉了，这里根本看不到它。
    """
    seen_local = False
    for i, tok in enumerate(rest):
        # -o /dev/null 是丢弃输出，不是写盘 ——「只看状态码」的标准写法。
        if tok in ("-o", "--output"):
            if i + 1 >= len(rest) or rest[i + 1] != "/dev/null":
                return False
            continue
        if tok in CURL_WRITE_FLAGS or tok.startswith("--data"):
            return False
        if tok in ("-X", "--request"):
            if i + 1 >= len(rest) or rest[i + 1].upper() not in ("GET", "HEAD"):
                return False
            continue
        # 只认「明确写出来的 URL」，其余 token（选项及其取值）一概不管。不要去猜
        # 哪个 token 是某个选项的取值再跳过它：引号在解析前已被剥掉，
        # `-w '%{http_code}' URL` 会变成 `-w URL`，跳过取值就把 URL 一起吃了，
        # 于是「一个本地 URL 都没有」→ 整条命令落回询问。
        if "://" in tok or LOCAL_URL.match(tok):
            if not LOCAL_URL.match(tok):
                return False
            seen_local = True
    return seen_local


def cmd_ok(tokens: list[str]) -> bool:
    tokens = _unwrap(tokens)
    if not tokens:
        return False
    name = _base(tokens[0])
    if name not in READONLY:
        return False
    rest = tokens[1:]
    if name == "git":
        return git_ok([name, *rest])
    if name == "uv":
        return uv_ok(rest)
    if name == "ruff":
        return ruff_ok(rest)
    if name == "mypy":
        # --install-types 会去装包，其余形态只写 .mypy_cache。
        return not any(a == "--install-types" for a in rest)
    if name == "alembic":
        return any(a in ALEMBIC_READONLY for a in rest)
    if name == "curl":
        return curl_ok(rest)
    if name == "docker":
        if not rest:
            return False
        if rest[0] == "compose":
            return len(rest) > 1 and rest[1] in DOCKER_READONLY
        return rest[0] in DOCKER_READONLY
    if name in ("sed", "awk") and any(
        r == "-i" or r.startswith("-i.") or r == "--in-place" for r in rest
    ):
        return False  # 原地编辑是写
    if name == "find":
        # -exec 能跑任意命令，-delete 直接删文件
        return not any(
            r in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint") for r in rest
        )
    return True


def allowed(cmd: str) -> bool:
    """整条命令是否全部由只读命令构成。"""
    if "<<" in cmd:
        return False  # heredoc 体内容不好判定，交回权限系统
    if ".env" in cmd.replace(".env.example", ""):
        return False  # 沾 .env 的一律不自动放行，由 blocked() 或人来决定

    main, inner = substitutions(cmd)
    for piece in [main, *inner]:
        bare = strip_quotes(piece)
        if REDIRECT_OK.sub(" ", bare).count(">"):
            return False  # 写重定向
        for segment in re.split(r"\|\||&&|[|;&\n]", bare):
            tokens = segment.split()
            if not tokens:
                continue
            head = head_token(segment)
            if head is None:
                continue
            if head == "__SUBST__":
                return False  # 命令名本身由替换算出来，判不了，交回权限系统
            if not cmd_ok(tokens[tokens.index(head) :]):
                return False
    return True


def main() -> int:
    cmd = sys.stdin.read()
    if not cmd.strip():
        return 0

    reason = blocked(cmd)
    if reason:
        print(reason, file=sys.stderr)
        return 2

    if allowed(cmd):
        print(
            '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"permissionDecision":"allow",'
            '"permissionDecisionReason":"守卫判定为纯只读命令"}}'
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
