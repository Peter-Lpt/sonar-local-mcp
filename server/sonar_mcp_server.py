"""本地 Sonar MCP server(stdio)。

供任意 MCP 客户端(Reasonix / Claude / Cursor / Codex 等)经 stdio 调用本地 Sonar
引擎(sonar-local.jar,内嵌 sonarlint-core)离线审查 Java 代码,零服务器、零驻留。

职责:调引擎分析并缓存报告;过滤/分页/汇总(控制返回体大小避免客户端截断);
get_source_code 仅读最近分析项目根内文件;可预期错误返回结构化 {"error":...}。

依赖: pip install -r requirements.txt
用法: python sonar_mcp_server.py(MCP 客户端以 stdio 启动)
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 常量与可配置项
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
# 引擎模块目录(其 target/ 下存放构建产物 jar,由 _find_jar 自动发现)
ENGINE_DIR = BASE_DIR / "engine"
REPORT = BASE_DIR / "reports" / "sonar-report.json"

# ---------------------------------------------------------------------------
# 统一配置:可选配置文件(默认 sonar-local-mcp 根目录的 sonar-local-config.json,可用 SONAR_CONFIG 指定)
# + 环境变量覆盖。优先级:环境变量 > 配置文件 > 默认值。
# 配置文件示例见 sonar-local-config.example.json。
# ---------------------------------------------------------------------------

SONAR_CONFIG_FILE = os.environ.get("SONAR_CONFIG") or str(BASE_DIR / "sonar-local-config.json")


def _load_config_file() -> dict:
    p = Path(SONAR_CONFIG_FILE)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


CONFIG = _load_config_file()


def _resolve(env_key: str, cfg_path: str, default):
    """按 环境变量 -> 配置文件(点分路径) -> 默认值 取配置。"""
    env = os.environ.get(env_key)
    if env is not None and str(env).strip() != "":
        return str(env).strip()
    node = CONFIG
    for part in cfg_path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    if isinstance(node, str):
        node = node.strip()
    return node if node not in (None, "") else default


def _severity_str(value) -> str:
    """把 severity 配置归一化为逗号分隔字符串(支持列表或字符串)。"""
    if isinstance(value, list):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if value else ""


# sonarlint-core 9.8 需要 Java 17+;默认用配置/环境变量指定的 JDK,
# 未设置时回退到系统 PATH 中的 java(可用 SONAR_CONFIG 或 .mcp.json 的 env 注入)
JAVA = _resolve("SONAR_JAVA", "sonar_java", "java")

# 引擎单次分析超时(秒)
ANALYZE_TIMEOUT = int(_resolve("SONAR_TIMEOUT", "timeout_seconds", 900))

# 单个工具返回体的最大字符数。超过该值的内容会被截断并在结果中给出
# "hint",提示调用方用分页参数继续取 —— 防止大 JSON 超出客户端单次结果上限。
MAX_TEXT_CHARS = int(_resolve("SONAR_MAX_TEXT", "max_text_chars", 12000))

# 远程 SonarQube(自建/SonarQube Cloud)连接配置。配置后分析会拉取该服务器的
# 质量配置(quality profile)中启用的规则,用远程规则做本地校验;
# 不配置则走本地插件默认规则(与旧行为一致)。
SONARQUBE_URL = _resolve("SONARQUBE_URL", "sonarqube.url", "").rstrip("/")
SONARQUBE_TOKEN = _resolve("SONARQUBE_TOKEN", "sonarqube.token", "")
# 质量配置定位(二选一):直接给 profile 名称/key,或给 project key 自动解析其生效配置
SONARQUBE_PROFILE = _resolve("SONARQUBE_PROFILE", "sonarqube.profile", "")
SONARQUBE_PROJECT = _resolve("SONARQUBE_PROJECT", "sonarqube.project", "")

# 远程规则缓存 TTL(秒):质量配置变化不频繁,缓存避免每次分析都重复网络拉取
REMOTE_RULES_TTL = int(_resolve("SONAR_RULES_TTL", "remote_rules_ttl_seconds", 900))

# analyze_code_snippet 的代码片段大小上限(字节)
MAX_SNIPPET_BYTES = 1_000_000

SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]

# 用户自定义的默认严重级别过滤(配置文件 severity/min_severity 或环境变量调)
# 作为 analyze_project / list_issues 的默认过滤条件;调用时显式传参可覆盖。
DEFAULT_SEVERITY = _severity_str(_resolve("SONAR_SEVERITY", "severity", ""))
DEFAULT_MIN_SEVERITY = _severity_str(_resolve("SONAR_MIN_SEVERITY", "min_severity", ""))

mcp = FastMCP("sonar-local-mcp")

# ---------------------------------------------------------------------------
# 状态与缓存
# ---------------------------------------------------------------------------

_cached: dict | None = None  # 最近一次分析的完整报告(含 project / issues)

# 远程规则拉取缓存:key = 配置签名(url/token/profile/project),value = (fetched_at, payload)
_remote_rules_cache: dict[str, tuple[float, dict]] = {}

# list_issues 过滤结果缓存(按 缓存id+过滤参数 定位,重新分析后自动失效)
_list_cache_key: tuple | None = None
_list_cache: list | None = None


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

# 引擎 jar 缓存:(路径, mtime)。命中且 mtime 未变则直接用,避免每次分析重复 glob。
_jar_cache: tuple[Path, float] | None = None


def _find_jar() -> Path | None:
    """自动发现引擎 fat jar,避免硬编码版本号。

    搜索顺序:分发位置 bin/ 优先(预构建 jar),其次引擎 target/
    (源码本地 mvn 构建产物)。排除 maven-shade 生成的 original-* 瘦 jar 与旧的
    *-shaded.jar 残留。结果按 (路径, mtime) 缓存:jar 被重建(mtime 变化)时自动重新发现。
    """
    global _jar_cache
    if _jar_cache is not None:
        path, mtime = _jar_cache
        try:
            if path.stat().st_mtime == mtime:
                return path
        except OSError:
            pass
    candidates = [ENGINE_DIR / "bin", ENGINE_DIR / "target"]
    found = None
    for base in candidates:
        if not base.is_dir():
            continue
        jars = [
            p for p in base.glob("sonar-local-mcp-*.jar")
            if not p.name.startswith("original-") and "-shaded" not in p.name
        ]
        if jars:
            found = max(jars, key=lambda p: p.stat().st_mtime)
            break
    if found is None:
        return None
    _jar_cache = (found, found.stat().st_mtime)
    return found


def _engine_ready() -> str | None:
    """返回 None 表示可用;否则返回缺失组件的安装提示。"""
    if _find_jar() is None:
        return (
            f"sonar-local-mcp.jar not found under {ENGINE_DIR / 'bin'} or {ENGINE_DIR / 'target'}\n"
            "请先构建引擎工具(需要 JDK 17 与 Maven):\n"
            "  cd engine && mvn -B package -DskipTests\n"
            "或从发布页下载预构建 jar 放到 engine/bin/ 目录。"
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


# ---------------------------------------------------------------------------
# 远程 SonarQube 规则配置拉取(用于本地用远程规则做校验)
# ---------------------------------------------------------------------------

def _sq_http_get(path_query: str, timeout: int = 30) -> dict:
    """对 SonarQube 服务器发起带 Basic Auth 的 GET,返回解析后的 JSON。失败抛 RuntimeError。"""
    url = f"{SONARQUBE_URL}/api/{path_query}"
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + _b64(SONARQUBE_TOKEN)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise RuntimeError(f"SonarQube API {e.code} for {url}: {body or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach SonarQube {url}: {e.reason}") from None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SonarQube API returned non-JSON for {url}: {e}") from None


def _b64(s: str) -> str:
    return base64.b64encode((s + ":").encode("utf-8")).decode("ascii")


def _resolve_profile_key() -> str | None:
    """解析用户要用的质量配置 key。返回 None 表示无需远程规则。"""
    if not SONARQUBE_URL or not SONARQUBE_TOKEN:
        return None
    if not SONARQUBE_PROFILE and not SONARQUBE_PROJECT:
        return None
    try:
        if SONARQUBE_PROJECT:
            data = _sq_http_get(
                f"qualityprofiles/search?projectKey={urllib.parse.quote(SONARQUBE_PROJECT)}")
            java_profiles = [p for p in data.get("profiles", []) if p.get("language") == "java"]
            if not java_profiles:
                return None
            # 项目生效的 profile 是其 java 默认(isDefault=true)的那个
            for p in java_profiles:
                if p.get("isDefault"):
                    return p.get("key")
            return java_profiles[0].get("key")
        else:
            data = _sq_http_get("qualityprofiles/search")
            for p in data.get("profiles", []):
                if p.get("language") == "java" and (
                    p.get("name") == SONARQUBE_PROFILE
                    or p.get("key") == SONARQUBE_PROFILE
                ):
                    return p.get("key")
    except RuntimeError as e:
        raise RuntimeError("remote rule fetch failed: " + str(e)) from None
    return None


def _remote_signature() -> str:
    """远程规则配置的签名,用于定位缓存。"""
    return "|".join([SONARQUBE_URL, SONARQUBE_TOKEN, SONARQUBE_PROFILE, SONARQUBE_PROJECT])


def _fetch_remote_rules_payload() -> dict | None:
    """拉取远程质量配置的启用规则,返回 payload dict 或 None(未配置/规则为空)。

    按配置签名做 TTL 缓存:TTL 内命中直接返回缓存,避免每次分析都重复网络拉取。
    未配置 SONARQUBE_URL/TOKEN 时无需网络,直接返回 None(走本地默认规则)。
    """
    sig = _remote_signature()
    cached = _remote_rules_cache.get(sig)
    if cached is not None and time.time() - cached[0] < REMOTE_RULES_TTL:
        return cached[1]
    profile = _resolve_profile_key()
    if profile is None:
        return None
    enabled: list[str] = []
    params: dict[str, dict[str, str]] = {}
    page, ps, total = 1, 500, None
    while total is None or (page - 1) * ps < total:
        data = _sq_http_get(
            f"rules/search?qprofile={urllib.parse.quote(profile)}"
            f"&activation=true&languages=java&ps={ps}&p={page}&f=actives")
        total = data.get("total", 0)
        for r in data.get("rules", []):
            key = r.get("key")
            if not key:
                continue
            enabled.append(key)
            # java:S 之外的规则(FindBugs 等)本地插件没有,记录但无法生效
        actives = data.get("actives") or {}
        for key, entries in actives.items():
            for e in entries:
                pa = {p["key"]: str(p.get("value")) for p in e.get("params", []) if p.get("key")}
                if pa:
                    params.setdefault(key, {}).update(pa)
        page += 1
    if not enabled:
        return None
    payload = {"enabled": enabled, "params": params}
    _remote_rules_cache[sig] = (time.time(), payload)
    return payload


def _run_engine(project_path: Path, out_path: Path, max_files: int,
                rules_payload: dict | None = None) -> dict:
    """调用引擎 fat jar 执行离线分析,返回报告 dict。失败时抛 RuntimeError。

    rules_payload 为远程规则 {"enabled", "params"};非 None 时写入临时文件交给引擎,
    运行结束(含异常)后立即清理,避免残留临时目录。
    """
    jar = _find_jar()
    if jar is None:
        raise RuntimeError(
            "engine jar not found, run `mvn -B package -DskipTests` in engine/ first"
        )
    # 内嵌 sonar-java-plugin 跟随 fat jar:bin/ 分发 → bin/plugins;
    # 源码构建(target/)→ target/plugins(maven-dependency-plugin 复制)。
    # sonar-java-plugin 必须与 engine jar 同基础目录,故按 jar 所在目录取 plugins 子目录。
    plugins_dir = str(jar.parent / "plugins")
    cmd = [
        JAVA, f"-Dsonar.plugins.dir={plugins_dir}", "-jar", str(jar),
        "--src", str(project_path),
        "--out", str(out_path),
        "--max-files", str(max_files),
    ]
    rules_file: str | None = None
    if rules_payload is not None:
        fd, rules_file = tempfile.mkstemp(prefix="sonarlocal-rules-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rules_payload, f, ensure_ascii=False)
        cmd += ["--rules", rules_file]
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
    finally:
        if rules_file is not None:
            try:
                os.unlink(rules_file)
            except OSError:
                pass

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


def _severity_rank(sev: str) -> int:
    """严重级别排序:BLOCKER=0 … INFO=4,未知级别排最后。"""
    if sev in SEVERITY_ORDER:
        return SEVERITY_ORDER.index(sev)
    return len(SEVERITY_ORDER)


def _filter_issues(items: list, severities: str = "", min_severity: str = "") -> list:
    """按严重级别集合与最低级别双重过滤。severities 支持逗号分隔。"""
    if not severities and not min_severity:
        return items
    keep = {s.strip().upper() for s in severities.split(",") if s.strip()} if severities else set()
    min_rank = _severity_rank(min_severity.strip().upper()) if min_severity else None
    result = []
    for i in items:
        sev = (i.get("severity") or "").upper()
        if keep and sev not in keep:
            continue
        if min_rank is not None and _severity_rank(sev) > min_rank:
            continue
        result.append(i)
    return result


def _filtered_issues(severity: str, rule: str, min_severity: str) -> list:
    """过滤最近一次分析结果并缓存:翻页只做切片(O(1)),避免每次全量重过滤。

    缓存键含 id(_cached),重新分析替换报告后自动失效。
    """
    global _list_cache_key, _list_cache
    key = (id(_cached), severity, rule, min_severity)
    if key == _list_cache_key and _list_cache is not None:
        return _list_cache
    items = _filter_issues(
        _cached.get("issues", []),
        severity or DEFAULT_SEVERITY,
        min_severity or DEFAULT_MIN_SEVERITY,
    )
    if rule:
        items = [i for i in items if rule in (i.get("ruleKey") or "")]
    _list_cache_key, _list_cache = key, items
    return items


def _summarize(items: list) -> dict:
    by_severity = Counter((i.get("severity") or "UNKNOWN") for i in items)
    by_type = Counter((i.get("type") or "UNKNOWN") for i in items)
    by_rule = Counter((i.get("ruleKey") or "UNKNOWN") for i in items)
    return {
        "bySeverity": {k: by_severity.get(k, 0) for k in SEVERITY_ORDER if by_severity.get(k)},
        "byType": dict(by_type.most_common()),
        "byRule": dict(by_rule.most_common()),
    }


def _hint(count: int, next_offset: int, severity: str = "", min_severity: str = "", rule: str = "") -> str:
    """生成翻页提示,把实际生效的过滤条件写进建议调用,避免翻页丢过滤。"""
    if next_offset >= count:
        return ""
    args = [f"offset={next_offset}", "limit=100"]
    for k, v in (("severity", severity), ("min_severity", min_severity), ("rule", rule)):
        if v:
            args.append(f"{k}={v!r}")
    return (
        f"{count - next_offset} more issue(s) not shown. "
        f"use list_issues({', '.join(args)}) to fetch the next page"
    )


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

@mcp.tool()
def analyze_project(project_path: str, max_files: int = 200, max_issues: int = 500,
                    severity: str = "", min_severity: str = "") -> str:
    """对本地 Java 项目执行 Sonar 引擎离线分析,返回汇总统计 + issues 列表(JSON)。

    返回体有大小上限,超出部分截断并在 "hint" 提示用 list_issues 分页取,始终返回完整可解析 JSON。

    Args:
        project_path: 项目根目录绝对路径。
        max_files: 最多分析文件数(0 = 不限,默认 200)。
        max_issues: 本次最多返回条数(默认 500,超出走分页)。
        severity: 严重级别集合(逗号分隔,可选 BLOCKER/CRITICAL/MAJOR/MINOR/INFO,留空用配置/全部)。
        min_severity: 只保留等于或更严重者(如 MAJOR 输出 BLOCKER/CRITICAL/MAJOR,留空不按此过滤)。
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
        rules_payload = _fetch_remote_rules_payload()
    except RuntimeError as e:
        return _engine_error(str(e))
    try:
        data = _run_engine(src, out_path, max_files, rules_payload)
    except RuntimeError as e:
        return _engine_error(str(e))

    _cached = data
    items = data.get("issues", [])
    # 未显式传过滤条件时,回退到用户配置的默认过滤(SONAR_SEVERITY / SONAR_MIN_SEVERITY)
    eff_sev = severity or DEFAULT_SEVERITY
    eff_min = min_severity or DEFAULT_MIN_SEVERITY
    items = _filter_issues(items, eff_sev, eff_min)
    shown, clipped = _clip(items, max_issues)
    result = {
        "project": data.get("project"),
        "filesAnalyzed": data.get("filesAnalyzed", 0),
        "total": len(items),
        "summary": _summarize(items),
        "issues": shown,
        "shown": len(shown),
        "truncated": clipped or len(items) > max_issues,
        "hint": _hint(len(items), len(shown), severity=eff_sev, min_severity=eff_min),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def list_issues(severity: str = "", rule: str = "", min_severity: str = "",
                limit: int = 100, offset: int = 0) -> str:
    """分页过滤最近一次分析的结果(先调 analyze_project)。

    Args:
        severity: 严重级别集合(逗号分隔,可选 BLOCKER/CRITICAL/MAJOR/MINOR/INFO,留空用配置/全部)。
        rule: 规则 key 子串过滤(如 "java:S106",留空 = 全部)。
        min_severity: 只保留等于或更严重者(如 MAJOR 输出 BLOCKER/CRITICAL/MAJOR,留空不按此过滤)。
        limit: 本次最多返回条数(默认 100,上限 500)。
        offset: 跳过前 N 条(用于翻页)。
    """
    if _cached is None:
        return _no_analysis()

    items = _filtered_issues(severity, rule, min_severity)
    # 未显式传的过滤条件回退到默认;翻页 hint 用有效值,保证翻页结果一致
    eff_sev = severity or DEFAULT_SEVERITY
    eff_min = min_severity or DEFAULT_MIN_SEVERITY
    limit = max(0, min(int(limit), 500))
    offset = max(0, int(offset))
    page = items[offset:offset + limit]
    shown, clipped = _clip(page, len(page))
    # 下一页 offset 必须严格大于当前 offset:当单条超大把 shown 裁到 0 时也至少前进 1
    next_offset = offset + max(len(shown), 1) if offset < len(items) else offset
    hint = _hint(len(items), next_offset, severity=eff_sev, min_severity=eff_min, rule=rule)
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
            rules_payload = _fetch_remote_rules_payload()
        except RuntimeError as e:
            return _engine_error(str(e))
        try:
            data = _run_engine(src_dir, out_path, max_files=10, rules_payload=rules_payload)
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
            "run `cd engine && mvn -B package -DskipTests` first",
            file=sys.stderr,
        )
    java_warning = _java_warning_once()
    if java_warning:
        print(f"[sonar-local-mcp] WARNING: {java_warning}", file=sys.stderr)
    mcp.run(transport="stdio")
