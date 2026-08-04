# sonar-local-mcp · 本地 Sonar MCP

通过 [MCP(Model Context Protocol)](https://modelcontextprotocol.io) 在 AI 客户端
(Reasonix / Claude Desktop / Cursor 等)中调用**本地 Sonar 引擎**做 Java 代码审查。

零服务器、零驻留:引擎内嵌 `sonarlint-core`(与 SonarLint IDE 插件同源,standalone 离线模式),
不依赖 SonarQube / SonarCloud,要查才跑、跑完退出。所有分析在本地完成,代码不出本机。

## 特性

- **离线分析**:嵌入 `sonarlint-core 9.8` + `sonar-java-plugin`,无需任何服务端;
- **即用即走**:CLI 进程按需启动,无常驻守护进程;
- **返回体可控**:工具结果自动裁剪并支持分页,大项目也不会撑爆客户端导致 JSON 解析失败;
- **安全边界**:`get_source_code` 仅允许读取"最近一次分析项目根目录"内的文件,杜绝任意文件读取;
- **汇总统计**:按严重级别 / 规则类型 / 规则 key 聚合,快速了解问题分布;
- **即席分析**:对一段代码片段直接跑引擎,不落盘项目。

## 架构

```
AI 客户端 (Reasonix / Claude / Cursor)
        │  MCP (stdio, JSON-RPC)
        ▼
sonar_mcp_server.py (FastMCP)
        │  subprocess 调用
        ▼
sonar-local-mcp.jar (Java, 内嵌 sonarlint-core)
        │  输出 issues JSON
        ▼
reports/sonar-report.json + 内存缓存
```

## 目录结构

```
mcp/
├── sonar-local-mcp/                 # 项目根(git 仓库)
│   ├── engine/                       # Java 引擎工具(内嵌 sonarlint-core)
│   │   ├── pom.xml                   # 构建配置(sonarlint-core 9.8 + sonar-java-plugin 7.15)
│   │   └── src/main/java/com/qs/sonar/SonarLocal.java
│   ├── server/
│   │   ├── sonar_mcp_server.py       # MCP server(FastMCP, stdio transport)
│   │   ├── test_client.py            # 协议往返回归测试
│   │   └── requirements.txt          # Python 依赖(mcp SDK)
│   ├── reports/                      # 最近一次分析报告(运行时自动生成)
│   ├── skills/sonar-local-mcp/       # Codex 引导 skill(SKILL.md + agents/openai.yaml)
│   ├── LICENSE                       # MIT
│   └── README.md
```

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| JDK | **17+** | 运行分析引擎的硬性要求(`sonarlint-core 9.8` 要求);17 / 21 / 25 均可 |
| Python | 3.10+ | 运行 MCP server |
| `mcp` SDK | ≥1.0 | MCP Python 库(`pip install -r server/requirements.txt`) |
| Maven | 3.6+ | **仅源码构建时需要**;直接使用预构建 jar 则不需要 |

> **JDK 为什么必须是 17+?** 这是嵌入的 `sonarlint-core 9.8` 库的硬性要求,不是配置项。
> 低于 17(JRE 8/11)无法加载引擎。若你的默认 `java` 版本不足,用 `SONAR_JAVA` 环境变量
> 显式指向 JDK 17+ 的 `java` 可执行文件(见下文接入配置)。
>
> **Maven 不是必须的**:引擎是 fat jar(`java -jar` 直接运行)。从源码构建才需要 Maven;
> 直接下载预构建 jar 可完全跳过 Maven。

## 安装

### 方式 A:直接使用预构建 jar(无需 Maven)

1. 获取引擎 jar:从发布页下载 `sonar-local-mcp-0.0.1.jar` 与
   `sonar-java-plugin-*.jar`,放到 `engine/target/` 目录;
2. 安装 Python 依赖:`python -m pip install -r server/requirements.txt`;
3. 完成,见下方"接入 AI 客户端"。

> MCP server 会自动发现 `engine/target/` 下最新的 `sonar-local-mcp-*.jar`(不硬编码版本号),
> 升级时直接替换 jar 即可,server 代码无需改动。

### 方式 B:从源码构建(需要 JDK 17 + Maven)

```powershell
# 1) 构建引擎工具
cd engine
mvn -B package -DskipTests
# 产物: target/sonar-local-mcp-0.0.1.jar(fat jar)+ target/plugins/sonar-java-plugin-*.jar

# 2) 安装 Python 依赖(一次性)
cd ../server
python -m pip install -r requirements.txt
```

## 快速验证

```powershell
cd server
# PATH 中的 java 若不是 JDK 17+,用 --java 显式指定(与 .mcp.json 的 env 注入等价)
python test_client.py --src <你的Java项目目录> --max-files 30 --java C:\path\to\jdk-17\bin\java.exe
```

测试覆盖:握手 → 工具列表 → 分析(断言返回 JSON 完整可解析)→ 过滤/分页 →
源码读取边界(越界路径必须被拒绝)→ 片段即席分析。

## 命令行用法(不经 MCP)

```powershell
java -jar engine\target\sonar-local-mcp-0.0.1.jar --src <项目目录> [--out report.json] [--max-files N]
```

- `--src`(必填)项目根目录;`--out` 报告输出路径(不填则仅打印到 stdout);
- `--max-files` 最多分析文件数,`0` = 不限(默认 0);
- 退出码:`0` 成功 / `2` 参数或输入错误 / `1` 运行异常。

输出 JSON:`{tool, project, filesAnalyzed, issues:[{ruleKey, severity, type, file, line, message}]}`

## MCP 工具参考

| 工具 | 参数 | 说明 |
|---|---|---|
| `analyze_project` | `project_path`(必填), `max_files=200`, `max_issues=500` | 离线分析项目,返回**汇总统计 + issues 列表**(超限自动裁剪并附 `hint`) |
| `list_issues` | `severity=""`, `rule=""`, `limit=100`, `offset=0` | 分页过滤最近一次分析结果(limit 上限 500) |
| `get_source_code` | `file_path`(必填) | 读取项目根内源码(绝对路径需在项目根内,越界拒绝) |
| `analyze_code_snippet` | `code`(必填), `file_name="Snippet.java"` | 对代码片段即席分析(自动临时目录,文件名防路径逃逸) |

### 返回体与分页

为避免大 JSON 超出客户端单次结果上限导致**截断、解析失败**,所有工具返回体受
`SONAR_MAX_TEXT`(默认 12000 字符)保护:

- `analyze_project` 返回 `total / shown / truncated / hint` + `summary`(bySeverity / byType / byRule);
- 完整 issue 明细通过 `list_issues` 分页获取:`offset` 翻页,`hint` 会给出下一页参数;
- 即使返回上万条 issue,JSON 也始终完整可解析。

## 接入 AI 客户端

以 `.mcp.json`(或客户端对应配置)为例:

```json
{
  "mcpServers": {
    "sonar-local-mcp": {
      "command": "python",
      "args": ["<本仓库绝对路径>\\server\\sonar_mcp_server.py"],
      "env": {
        "SONAR_JAVA": "C:\\path\\to\\jdk-17\\bin\\java.exe"
      }
    }
  }
}
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SONAR_JAVA` | `java`(PATH 查找) | JDK 17+ 的 `java` 可执行文件绝对路径 |
| `SONAR_TIMEOUT` | `900` | 单次分析超时(秒) |
| `SONAR_MAX_TEXT` | `12000` | 单个工具返回体最大字符数 |

## Codex 引导 skill

MCP 是执行层，`skills/sonar-local-mcp/` 是配套的 Codex 引导 skill：装到 `~/.codex/skills/` 后
Codex 会自动发现，负责教 agent 何时用哪个工具、结果截断时怎么翻页、有哪些坑。

- **触发场景**：Java 项目/代码片段质量分析、按 severity/rule 查 issues、读 issue 对应源码；
- **工作流**：`analyze_project` 先看 summary → 截断时按 `hint` 用 `list_issues` 翻页/过滤 →
  `get_source_code` 读源码 → `analyze_code_snippet` 片段审查；
- **CLI 回退**：MCP 不可用时直接运行引擎 fat jar（见"命令行用法"），无需 Python/MCP 依赖；
- **已知坑**：JDK 必须 17+、jar 未构建先 `mvn package`、返回体截断是设计行为。

安装：

```powershell
Copy-Item -Recurse skills\sonar-local-mcp $env:USERPROFILE\.codex\skills\
```

## 安全与边界

- **路径边界**:`get_source_code` 的绝对路径必须位于最近一次分析的项目根目录内
  (`Path.resolve()` 后校验),相对路径仅按项目根解析 —— 无法读取任意文件;
- **文件名防逃逸**:`analyze_code_snippet` 的 `file_name` 只取 basename 并强制 `.java`
  后缀,忽略目录成分;
- **大小边界**:返回体受 `SONAR_MAX_TEXT` 控制;代码片段上限 1 MB;
- **错误可读**:路径不存在、jar 未构建、JDK 缺失、报告损坏等可预期错误一律返回结构化
  `{"error": ...}` 文本(含修复提示),不会以调用异常的形式抛出。

## 常见问题

**Q: 提示 `sonar-local-mcp.jar not found`?**
A: 引擎工具未构建。源码构建:`cd engine && mvn -B package -DskipTests`(需 JDK 17 +
Maven);或按"方式 A"下载预构建 jar 放入 `engine/target/`(无需 Maven)。

**Q: 提示 Java launcher not found / 引擎启动失败?**
A: 默认 `java` 不是 JDK 17。通过 `SONAR_JAVA` 指向 JDK 17+ 的 `java.exe`。

**Q: 分析大项目很慢?**
A: 引擎按需启动,首次需加载插件(数秒)+ 解析全部 `.java` 文件。可用 `max_files` 限制
分析规模,或调大 `SONAR_TIMEOUT`。

**Q: 返回里 issues 被裁剪了?**
A: 这是设计行为:`hint` 字段会给出 `list_issues(offset=..., limit=...)` 的翻页参数,
按严重级别/规则过滤后分页取即可。

## 实现说明(踩坑记录)

1. 官方 `sonarlint-cli` 已归档(2018)且无稳定分发 → 直接嵌入 `sonarlint-core` 引擎;
2. `sonarlint-core 10.x+` 改为 RPC backend 架构、嵌入过重 → 锁定 **9.8 legacy API**
   (`StandaloneSonarLintEngineImpl`);
3. 必须 `addEnabledLanguage(Language.JAVA)`,否则插件 `skip=LanguagesNotEnabled`,0 条规则;
4. `sonar-java-plugin` 需以**独立 jar 文件**加载(`addPlugin(Path)`),不能打进 fat jar
   —— 构建时由 `maven-dependency-plugin` 复制到 `target/plugins/`;
5. `ClientInputFile` 需实现全部抽象方法(含 `contents()` / `inputStream()` / `getClientObject()`)。
6. MCP server 通过 `_find_jar()` 自动发现 `engine/target/` 下最新构建的 jar,升级版本无需改代码。

## 许可证

[MIT](LICENSE)
