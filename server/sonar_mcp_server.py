"""本地 Sonar MCP Server(stdio transport)。

在 AI 客户端(Reasonix / Claude / Cursor 等)中通过 MCP 调用本地 Sonar 引擎做
离线代码审查,零服务器、零驻留:引擎内嵌于 sonar-local.jar,要查才跑、跑完退出。

本 server 的职责:
  1. 调用引擎 fat jar(sonar-local-mcp.jar)执行分析并把报告缓存到内存与 reports/ 目录
  2. 过滤 / 分页 / 汇总结果 —— 单次工具返回体有大小上限,超出的部分截断并给出
     获取更多数据的提示,避免大 JSON 被客户端截断导致解析失败
  3. 安全边界:get_source_code 只能读取"最近一次分析项目根目录"内的文件
  4. 可预期错误(路径不存在、jar 未构建、报告缺失等)一律返回结构化 {"error": ...}
     文本而不是抛异常,让客户端拿到的是可读信息而非调用失败

依赖:  python -m pip install -r requirements.txt
用法:  python sonar_mcp_server.py   (由 MCP 客户端以 stdio 方式启动)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 常量与可配置项
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
# 引擎模块目录(其 target/ 下存放构建产物 jar,由 _find_jar 自动发现)
ENGINE_DIR = BASE_DIR / "sonar-local-mcp"
REPORT = BASE_DIR / "reports" / "sonar-report.json"

# sonarlint-core 9.8 需要 Java 17+;默认用环境变量 SONAR_JAVA 指定的 JDK,
# 未设置时回退到系统 PATH 中的 java(Windows 上可用 .mcp.json 的 env 注入)
JAVA = os.environ.get("SONAR_JAVA", "java")

# 引擎单次分析超时(秒)
ANALYZE_TIMEOUT = int(os.environ.get("SONAR_TIMEOUT", "900"))

# 单个工具返回体的最大字符数。超过该值的内容会被截断并在结果中给出
# "hint",提示调用方用分页参数继续取 —— 防止大 JSON 超出客户端单次结果上限。
MAX_TEXT_CHARS = int(os.environ.get("SONAR_MAX_TEXT", "12000"))

# analyze_code_snippet 的代码片段大小上限(字节)
MAX_SNIPPET_BYTES = 1_000_000

SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]

mcp = FastMCP("sonar-local-mcp")

# ---------------------------------------------------------------------------
# 状态与缓存
# ---------------------------------------------------------------------------

_cached: dict | None = None  # 最近一次分析的完整报告(含 project / issues)


def _no_analysis() -> str:
    return json.dumps(
        {"error": "no analysis yet, call analyze_project first"},
        ensure_ascii=False,
    )


def _engine_error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 引擎调用
# ---------------------------------------------------------------------------

def _find_jar() -> Path | None:
    """在引擎 target/ 目录自动发现最新构建的 fat jar,避免硬编码版本号。

    排除 maven-shade 生成的 original-* 瘦 jar 与旧的 *-shaded.jar 残留。
    """
    target = ENGINE_DIR / "target"
    if not target.is_dir():
        return None
    jars = [
        p for p in target.glob("sonar-local-mcp-*.jar")
        if not p.name.startswith("original-") and "-shaded" not in p.name
    ]
    if not jars:
        return None
    return max(jars, key=lambda p: p.stat().st_mtime)


def _engine_ready() -> str | None:
    """返回 None 表示可用;否则返回缺失组件的安装提示。"""
    if _find_jar() is None:
        return (
            f"sonar-local-mcp.jar not found under {ENGINE_DIR / 'target'}\n"
            "请先构建引擎工具(需要 JDK 17 与 Maven):\n"
            "  cd sonar-local-mcp && mvn -B package -DskipTests\n"
            "或从发布页下载预构建 jar 放到 target/ 目录。"
        )
    return _java_warning_once()


# java 版本探测结果缓存(每次 server 进程只探测一次)
_java_warning: str | None = None
_java_probed = False


def _java_warning_once() -> str | None:
    global _java_warning, _java_probed
    if not _java_probed:
        _java_warning = _probe_java()
        _java_probed = True
    return _java_warning


def _probe_java() -> str | None:
    """探测 JAVA 可执行文件是否可用且 >= 17,返回警告信息或 None。"""
    try:
        proc = subprocess.run(
            [JAVA, "-version"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot run java launcher {JAVA!r}: {e} (set SONAR_JAVA to a JDK 17+ java executable)"
    text = proc.stdout + proc.stderr
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return f"cannot determine java version from {JAVA!r}: {text[:120]}"
    # JDK 8 报告 "1.8.0_xxx",取第二段;JDK 9+ 报告 "17.0.8" 直接取第一段
    major = int(m.group(2) if m.group(1) == "1" else m.group(1))
    if major < 17:
        return (
            f"{JAVA} is Java {major}(<17): sonarlint-core 9.8 requires JDK 17+. "
            f"set SONAR_JAVA to a JDK 17+ java executable"
        )
    return None


def _run_engine(project_path: Path, out_path: Path, max_files: int) -> dict:
    """调用引擎 fat jar 执行离线分析,返回报告 dict。失败时抛 RuntimeError。"""
    jar = _find_jar()
    if jar is None:
        raise RuntimeError(
            "engine jar not found, run `mvn -B package -DskipTests` in sonar-local-mcp/ first"
        )
    cmd = [
        JAVA, "-jar", str(jar),
        "--src", str(project_path),
        "--out", str(out_path),
        "--max-files", str(max_files),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=ANALYZE_TIMEOUT,
            cwd=str(ENGINE_DIR),
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Java launcher not found: {JAVA!r}\n"
            "请通过 SONAR_JAVA 环境变量指向 JDK 17+ 的 java 可执行文件,"
            "例如 .mcp.json 中 \"env\": {\"SONAR_JAVA\": \"C:\\\\path\\\\to\\\\jdk-17\\\\bin\\\\java.exe\"}"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"analysis timed out after {ANALYZE_TIMEOUT}s") from None

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        lines = stderr.splitlines()
        tail = " | ".join(lines[-8:]) if lines else "unknown error"
        hint = ""
        if any(k in stderr for k in ("UnsupportedClassVersionError", "JNI error", "checkAndLoadMain")):
            hint = (
                " [java version too old?] sonarlint-core 9.8 requires JDK 17+; "
                "set SONAR_JAVA to a JDK 17+ java executable"
            )
        raise RuntimeError(f"engine failed (rc={proc.returncode}): {tail[-2000:]}{hint}")

    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"failed to read engine report: {e}") from None


# ---------------------------------------------------------------------------
# 结果裁剪:保证返回体 JSON 长度可控
# ---------------------------------------------------------------------------

def _json_size(items: list) -> int:
    return len(json.dumps({"issues": items}, ensure_ascii=False))


def _clip(items: list, limit: int, max_chars: int = MAX_TEXT_CHARS) -> tuple[list, bool]:
    """按 limit 与 max_chars 双重约束截断,返回 (截断后的条目, 是否发生了截断)。"""
    if limit <= 0:
        return [], False
    shown = items[:limit]
    if _json_size(shown) <= max_chars:
        return shown, False
    lo, hi = 0, len(shown)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_size(shown[:mid]) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    return shown[:lo], True


def _summarize(items: list) -> dict:
    by_severity = Counter((i.get("severity") or "UNKNOWN") for i in items)
    by_type = Counter((i.get("type") or "UNKNOWN") for i in items)
    by_rule = Counter((i.get("ruleKey") or "UNKNOWN") for i in items)
    return {
        "bySeverity": {k: by_severity.get(k, 0) for k in SEVERITY_ORDER if by_severity.get(k)},
        "byType": dict(by_type.most_common()),
        "byRule": dict(by_rule.most_common()),
    }


def _hint(count: int, next_offset: int, filtered: bool) -> str:
    """生成翻页提示。next_offset 由调用方计算并保证严格前进(否则死循环)。"""
    if next_offset >= count:
        return ""
    return (
        f"{count - next_offset} more issue(s) not shown. "
        f"use list_issues(offset={next_offset}, limit=..."
        + (", severity=..., rule=...)" if filtered else ")")
        + " to fetch the next page"
    )


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_project(project_path: str, max_files: int = 200, max_issues: int = 500) -> str:
    """对本地 Java 项目执行 Sonar 引擎离线分析,返回汇总统计 + issues 列表(JSON)。

    返回体有大小上限,超出部分会被截断并在 "hint" 中提示用 list_issues 分页获取,
    因此本工具始终返回完整可解析的 JSON,不会因结果过大而报错。

    Args:
        project_path: 项目根目录绝对路径。
        max_files: 最多分析文件数(0 = 不限,默认 200)。
        max_issues: 报告内最多携带的 issue 条目数(默认 500,超出走分页)。
    """
    ready = _engine_ready()
    if ready:
        return _engine_error(ready)

    src = Path(project_path)
    if not src.is_dir():
        return _engine_error(f"project_path is not an existing directory: {project_path}")

    if max_files < 0:
        max_files = 0
    if max_issues < 0:
        max_issues = 0

    global _cached
    out_path = REPORT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = _run_engine(src, out_path, max_files)
    except RuntimeError as e:
        return _engine_error(str(e))

    _cached = data
    items = data.get("issues", [])
    shown, clipped = _clip(items, max_issues)
    result = {
        "project": data.get("project"),
        "filesAnalyzed": data.get("filesAnalyzed", 0),
        "total": len(items),
        "summary": _summarize(items),
        "issues": shown,
        "shown": len(shown),
        "truncated": clipped or len(items) > max_issues,
        "hint": _hint(len(items), len(shown), filtered=False),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def list_issues(severity: str = "", rule: str = "", limit: int = 100, offset: int = 0) -> str:
    """分页过滤最近一次分析的结果(先调 analyze_project)。

    Args:
        severity: BLOCKER/CRITICAL/MAJOR/MINOR/INFO,留空 = 全部。
        rule: 规则 key 子串(如 "java:S106"),留空 = 全部。
        limit: 本次最多返回条数(默认 100,上限 500)。
        offset: 跳过前 N 条(用于翻页)。
    """
    if _cached is None:
        return _no_analysis()

    items = _cached.get("issues", [])
    if severity:
        items = [i for i in items if (i.get("severity") or "").upper() == severity.upper()]
    if rule:
        items = [i for i in items if rule in (i.get("ruleKey") or "")]

    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    page = items[offset:offset + limit]
    shown, clipped = _clip(page, len(page))
    # 下一页 offset 必须严格大于当前 offset:当单条超大把 shown 裁到 0 时也至少前进 1
    next_offset = offset + max(len(shown), 1) if offset < len(items) else offset
    hint = _hint(len(items), next_offset, filtered=bool(severity or rule))
    return json.dumps(
        {
            "count": len(items),
            "offset": offset,
            "returned": len(shown),
            "truncated": clipped or len(items) > offset + len(shown),
            "issues": shown,
            "hint": hint,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_source_code(file_path: str) -> str:
    """读取源码文件内容(安全边界:仅限最近一次分析的项目根目录内)。

    Args:
        file_path: 相对上次分析项目根的路径(如 "src/main/java/A.java"),
                   或位于项目根内的绝对路径。越界访问会被拒绝。
    """
    if _cached is None:
        return _no_analysis()

    project = _cached.get("project")
    if not project:
        return _engine_error("analysis report has no project root, re-run analyze_project")

    root = Path(project).resolve()
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()

    if p != root and not p.is_relative_to(root):
        return _engine_error(
            f"access denied: {file_path} is outside the analyzed project root {root}"
        )
    if not p.is_file():
        return _engine_error(f"file not found: {file_path}")

    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _engine_error(f"failed to read {file_path}: {e}")


@mcp.tool()
def analyze_code_snippet(code: str, file_name: str = "Snippet.java") -> str:
    """对一段代码片段执行 Sonar 引擎即席分析(写到临时文件后分析)。

    Args:
        code: Java 源码片段。
        file_name: 用于分析的文件名,仅取 basename 且强制 .java 后缀
                   (忽略目录部分,避免路径逃逸)。
    """
    ready = _engine_ready()
    if ready:
        return _engine_error(ready)

    if not code or not code.strip():
        return _engine_error("code must not be empty")
    if len(code.encode("utf-8")) > MAX_SNIPPET_BYTES:
        return _engine_error(f"code too large (>{MAX_SNIPPET_BYTES} bytes)")

    # 安全:只取文件名部分并强制 .java,忽略任何目录成分
    name = Path(file_name).name
    if not name.endswith(".java"):
        name += ".java"

    with tempfile.TemporaryDirectory(prefix="sonarlocal-snippet-") as tmp:
        src_dir = Path(tmp) / "src"
        src_dir.mkdir()
        (src_dir / name).write_text(code, encoding="utf-8")
        out_path = Path(tmp) / "report.json"
        try:
            data = _run_engine(src_dir, out_path, max_files=10)
        except RuntimeError as e:
            return _engine_error(str(e))

    items = data.get("issues", [])
    shown, clipped = _clip(items, 200)
    return json.dumps(
        {"total": len(items), "shown": len(shown), "truncated": clipped, "issues": shown},
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    if _find_jar() is None:
        print(
            f"[sonar-local-mcp] WARNING: engine jar not found under {ENGINE_DIR / 'target'} — "
            "run `cd sonar-local-mcp && mvn -B package -DskipTests` first",
            file=sys.stderr,
        )
    java_warning = _java_warning_once()
    if java_warning:
        print(f"[sonar-local-mcp] WARNING: {java_warning}", file=sys.stderr)
    mcp.run(transport="stdio")
