---
name: sonar-local-mcp
description: Run local offline Sonar analysis on Java projects and code snippets via the sonar-local-mcp engine (embedded sonarlint-core), without a SonarQube server. Use when the user asks to analyze or review Java code quality, find bugs/vulnerabilities/code smells in a Java project, check a Java code snippet, list/filter Sonar issues by severity or rule (e.g. java:S106), or read the source file behind a reported issue. Prefer the MCP tools when registered; fall back to the engine CLI otherwise.
---

# Sonar 本地代码分析

通过 sonar-local-mcp（内嵌 sonarlint-core 9.8）在本机离线分析 Java 代码，无需 SonarQube 服务器、无常驻进程。优先使用 MCP 工具；MCP 不可用时用 CLI 回退。

## 工作流

1. **项目分析**：调用 `analyze_project(project_path, max_files=200, max_issues=500)`。返回 JSON：`summary`（bySeverity / byType / byRule）、`issues`、`total / shown / truncated / hint`。
2. **分页与过滤**：结果截断（`truncated=true` 或存在 `hint`）时，用 `list_issues(offset, limit=100, severity="", rule="")` 按 `hint` 给出的参数翻页；按严重级别 / 规则 key 过滤。先看 summary 再按需取明细，不要一次拉回全部 issues。
3. **读取源码**：`get_source_code(file_path)` 读取最近一次分析项目根目录内的文件（相对或绝对路径均可）。越界会被拒绝，这是硬性沙箱，不要尝试绕过。
4. **片段审查**：`analyze_code_snippet(code, file_name="Snippet.java")` 对一段代码即时分析，不落盘项目。
5. **完整报告**：最近一次分析结果（未截断）缓存在 sonar-local-mcp 仓库根目录下的 `reports/sonar-report.json`，需要原始数据时直接读该文件。

## 工具速查

| 工具 | 用途 |
|---|---|
| `analyze_project` | 全项目离线分析，返回汇总 + 分页 issues |
| `list_issues` | 分页 / 过滤最近一次分析结果 |
| `get_source_code` | 读项目根内源码（路径沙箱强制） |
| `analyze_code_snippet` | 对代码片段即时分析 |

## CLI 回退（MCP 不可用时）

引擎是独立 fat jar，无需 Python / MCP 依赖：

```
cd <sonar-local-mcp仓库目录>\engine
java -jar target\sonar-local-mcp-0.0.1.jar --src <项目目录> [--out report.json] [--max-files N]
```

- 默认 `java` 可能低于 17，必须显式用 JDK 17+（如 `C:\path\to\jdk-17\bin\java.exe`）或设 `SONAR_JAVA`。
- `--src` 必填；`--out` 不填则打印到 stdout；`--max-files 0` = 不限制。
- 退出码：0 成功 / 2 参数或输入错误 / 1 运行异常。
- 插件目录默认相对 cwd 的 `target/plugins`，所以先 `cd engine` 再运行；PowerShell 下不要用 `-Dsonar.plugins.dir=...` 裸传参（会被拆坏），需要指定时用引号包裹。

## 已知坑

- **JDK 17+**：sonarlint-core 9.8 硬性要求；默认 `java` 版本不足时必须把 `SONAR_JAVA` 指向 JDK 17+ 的 java.exe（MCP 配置用 `env` 注入）。
- **jar 未构建**：报 `sonar-local-mcp.jar not found` 时先执行 `cd engine && mvn -B package -DskipTests`。
- **首次分析慢**：首次需加载插件（数秒），大项目可用 `max_files` 限制范围；单次超时默认 900s（`SONAR_TIMEOUT`）。
- **返回体截断是设计行为**：上限默认 12000 字符（`SONAR_MAX_TEXT`），按 `hint` 翻页即可，不要重复调用 `analyze_project`。
- **文件越界**：`get_source_code` 只能读最近一次分析的项目根目录内文件。
