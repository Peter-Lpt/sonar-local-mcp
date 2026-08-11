package com.sonarlocal;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.sonarsource.sonarlint.core.StandaloneSonarLintEngineImpl;
import org.sonarsource.sonarlint.core.analysis.api.AnalysisResults;
import org.sonarsource.sonarlint.core.analysis.api.ClientInputFile;
import org.sonarsource.sonarlint.core.client.api.common.PluginDetails;
import org.sonarsource.sonarlint.core.client.api.common.analysis.Issue;
import org.sonarsource.sonarlint.core.client.api.common.analysis.IssueListener;
import org.sonarsource.sonarlint.core.client.api.standalone.StandaloneAnalysisConfiguration;
import org.sonarsource.sonarlint.core.client.api.standalone.StandaloneGlobalConfiguration;
import org.sonarsource.sonarlint.core.client.api.standalone.StandaloneRuleDetails;
import org.sonarsource.sonarlint.core.client.api.standalone.StandaloneSonarLintEngine;
import org.sonarsource.sonarlint.core.commons.Language;
import org.sonarsource.sonarlint.core.commons.RuleKey;
import org.sonarsource.sonarlint.core.commons.log.ClientLogOutput;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

/**
 * 本地 Sonar 扫描工具:嵌入 SonarLint 引擎(standalone 模式),无需任何服务端。
 * 输出 issues JSON(rule/severity/type/file/line/message),供 AI 分析。
 *
 * <p>用法: java -jar sonar-local.jar --src &lt;项目目录&gt; [--out &lt;输出json&gt;] [--max-files N]
 *
 * <p>退出码: 0 = 成功; 2 = 参数/输入错误; 1 = 分析过程异常。
 */
public class SonarLocal {

  private static final String TOOL_VERSION = "sonarlint-core-9.8.0.76914";

  public static void main(String[] args) {
    try {
      int exitCode = run(args);
      if (exitCode != 0) {
        System.exit(exitCode);
      }
    } catch (IllegalArgumentException e) {
      System.err.println("ERROR: " + e.getMessage());
      System.exit(2);
    } catch (Exception e) {
      System.err.println("ERROR: unexpected failure: " + e);
      System.exit(1);
    }
  }

  /** 执行分析并返回退出码(0 成功 / 2 输入错误 / 1 运行异常)。 */
  private static int run(String[] args) throws Exception {
    String src = null;
    String out = null;
    String rulesFile = null;
    int maxFiles = 0; // 0 = 不限

    for (int i = 0; i < args.length; i++) {
      switch (args[i]) {
        case "--src" -> src = (i + 1 < args.length) ? args[++i] : null;
        case "--out" -> out = (i + 1 < args.length) ? args[++i] : null;
        case "--rules" -> rulesFile = (i + 1 < args.length) ? args[++i] : null;
        case "--max-files" -> {
          if (i + 1 >= args.length) {
            throw new IllegalArgumentException("--max-files requires an integer value");
          }
          try {
            maxFiles = Integer.parseInt(args[++i]);
          } catch (NumberFormatException e) {
            throw new IllegalArgumentException("--max-files expects an integer, got: " + args[i]);
          }
        }
        default -> { /* ignore unknown flags */ }
      }
    }

    if (src == null || src.isBlank()) {
      System.err.println("usage: java -jar sonar-local.jar --src <dir> [--out <file>] [--max-files N]");
      return 2;
    }
    if (maxFiles < 0) {
      maxFiles = 0;
    }

    Path baseDir = Path.of(src).toAbsolutePath().normalize();
    if (!Files.isDirectory(baseDir)) {
      System.err.println("ERROR: source path is not a directory: " + baseDir);
      return 2;
    }

    // 收集 *.java 源码(排除 target/.git 构建产物),无文件时直接输出空报告
    List<Path> paths = collectJavaFiles(baseDir, maxFiles);
    if (paths.isEmpty()) {
      System.err.println("[sonarlint] no .java files found under " + baseDir + ", writing empty report");
      writeReport(emptyReport(baseDir), out);
      return 0;
    }

    RulesConfig rules = rulesFile != null ? loadRulesConfig(Path.of(rulesFile)) : RulesConfig.NONE;
    List<Map<String, Object>> issues = analyze(baseDir, paths, rules);
    Map<String, Object> report = new LinkedHashMap<>();
    report.put("tool", TOOL_VERSION);
    report.put("project", baseDir.toString());
    report.put("filesAnalyzed", paths.size());
    report.put("issues", issues);
    writeReport(report, out);
    return 0;
  }

  /** 递归收集 .java 文件,跳过常见构建产物目录(target/.git/build/out/.gradle)。 */
  private static final Set<String> SKIP_DIRS = Set.of("target", ".git", "build", "out", ".gradle");

  private static List<Path> collectJavaFiles(Path baseDir, int maxFiles) throws IOException {
    List<Path> paths = new ArrayList<>();
    try (Stream<Path> walk = Files.walk(baseDir)) {
      walk.filter(Files::isRegularFile)
        .filter(p -> p.getFileName().toString().endsWith(".java"))
        .filter(p -> !isUnder(p, baseDir, SKIP_DIRS))
        .limit(maxFiles > 0 ? maxFiles : Long.MAX_VALUE)
        .forEach(paths::add);
    }
    return paths;
  }

  /** 判断 path 的某个祖先目录(不含项目根自身)是否命中 dirNames 之一。
   *  以 baseDir 为基准做相对化,避免项目根恰好叫 build/target 等时被误过滤。 */
  private static boolean isUnder(Path path, Path baseDir, Set<String> dirNames) {
    for (Path part : baseDir.relativize(path)) {
      if (dirNames.contains(part.toString())) {
        return true;
      }
    }
    return false;
  }

  /** 用户通过 --rules 提供的规则覆盖:enabled = 只启用这些规则;params = 规则参数。 */
  private record RulesConfig(List<String> enabled, Map<String, Map<String, String>> params) {
    static final RulesConfig NONE = new RulesConfig(List.of(), Map.of());
    boolean provided() { return !enabled.isEmpty(); }
  }

  private static RulesConfig loadRulesConfig(Path file) throws IOException {
    ObjectMapper om = new ObjectMapper();
    Map<String, Object> root = om.readValue(file.toFile(), Map.class);
    List<String> enabled = new ArrayList<>();
    Object en = root.get("enabled");
    if (en instanceof List<?> l) {
      for (Object o : l) {
        if (o != null && !o.toString().isBlank()) enabled.add(o.toString());
      }
    }
    Map<String, Map<String, String>> params = new LinkedHashMap<>();
    Object pr = root.get("params");
    if (pr instanceof Map<?, ?> m) {
      for (Map.Entry<?, ?> e : m.entrySet()) {
        Map<String, String> p = new LinkedHashMap<>();
        if (e.getValue() instanceof Map<?, ?> vm) {
          for (Map.Entry<?, ?> pe : vm.entrySet()) {
            p.put(String.valueOf(pe.getKey()), pe.getValue() == null ? "" : String.valueOf(pe.getValue()));
          }
        }
        params.put(String.valueOf(e.getKey()), p);
      }
    }
    return new RulesConfig(enabled, params);
  }

  /** 运行分析,任何情况下都会释放引擎资源。 */
  private static List<Map<String, Object>> analyze(Path baseDir, List<Path> paths, RulesConfig rules) throws IOException {
    StandaloneSonarLintEngine engine = null;
    try {
      Path workDir = Files.createTempDirectory("sonarlocal-work");
      workDir.toFile().deleteOnExit();

      Path pluginDir = Path.of(System.getProperty("sonar.plugins.dir", "target/plugins"));
      Path javaPlugin = findJavaPlugin(pluginDir);
      if (javaPlugin == null) {
        throw new IllegalArgumentException(
          "sonar-java-plugin jar not found under " + pluginDir.toAbsolutePath()
            + " (run `mvn package` in sonar-local/ first)");
      }

      // 全局配置:加载 Java 分析器插件,离线分析
      StandaloneGlobalConfiguration globalConfig = StandaloneGlobalConfiguration.builder()
        .addPlugin(javaPlugin)
        .addEnabledLanguage(Language.JAVA)
        .setWorkDir(workDir)
        .setLogOutput((formattedMessage, level) -> {
          if (level == ClientLogOutput.Level.ERROR || level == ClientLogOutput.Level.WARN) {
            System.err.println("[sonarlint] " + formattedMessage);
          }
        })
        .build();

      engine = new StandaloneSonarLintEngineImpl(globalConfig);
      for (PluginDetails pd : engine.getPluginDetails()) {
        System.err.println("[sonarlint] plugin " + pd.name() + " v" + pd.version()
          + " skip=" + pd.skipReason().map(Object::toString).orElse("none"));
      }

      // 只查询一次规则详情,供启用/排除计算与日志统计复用
      Collection<StandaloneRuleDetails> allRuleDetails = engine.getAllRuleDetails();
      List<ClientInputFile> files = paths.stream().map(p -> onDisk(p, baseDir)).toList();
      StandaloneAnalysisConfiguration.Builder configBuilder = StandaloneAnalysisConfiguration.builder()
        .setBaseDir(baseDir)
        .addInputFiles(files);
      if (rules.provided()) {
        // 远程质量配置生效:只启用指定规则,并把其余的插件默认规则全部关闭,
        // 使本地分析结果与远程 SonarQube 质量配置一致。
        // 远程配置可能含本地插件不认识的规则(findbugs/external_* 等)或畸形键,
        // RuleKey.parse 会抛异常,故先与本地规则做交集,再逐个安全解析并跳过。
        List<String> allKeys = allRuleDetails.stream().map(StandaloneRuleDetails::getKey).toList();
        Set<String> known = new HashSet<>(allKeys);
        List<RuleKey> enabled = new ArrayList<>();
        for (String key : rules.enabled()) {
          if (!known.contains(key)) {
            System.err.println("[sonarlint] skip unknown remote rule: " + key);
            continue;
          }
          try {
            enabled.add(RuleKey.parse(key));
          } catch (IllegalArgumentException e) {
            System.err.println("[sonarlint] skip unparseable rule key: " + key);
          }
        }
        List<RuleKey> excluded = allKeys.stream()
          .filter(k -> !rules.enabled().contains(k))
          .map(RuleKey::parse)
          .toList();
        configBuilder.addIncludedRules(enabled);
        configBuilder.addExcludedRules(excluded);
        for (String key : rules.params().keySet()) {
          if (!known.contains(key)) {
            continue;
          }
          try {
            configBuilder.addRuleParameters(RuleKey.parse(key), rules.params().get(key));
          } catch (IllegalArgumentException e) {
            System.err.println("[sonarlint] skip unparseable rule param key: " + key);
          }
        }
        System.err.println("[sonarlint] applying remote rules: enabled=" + enabled.size()
          + ", excluded=" + excluded.size()
          + ", params=" + rules.params().size());
      }
      StandaloneAnalysisConfiguration config = configBuilder.build();

      List<Map<String, Object>> issues = new ArrayList<>();
      IssueListener listener = issue -> issues.add(toMap(issue));

      System.err.println("[sonarlint] analyzing " + files.size() + " java files under " + baseDir + " ...");
      AnalysisResults results = engine.analyze(config, listener, null, null);
      System.err.println("[sonarlint] done: indexed=" + results.indexedFileCount()
        + ", failedFiles=" + results.failedAnalysisFiles().size()
        + ", issues=" + issues.size()
        + ", loadedRules=" + allRuleDetails.size());
      return issues;
    } finally {
      if (engine != null) {
        try {
          engine.stop();
        } catch (RuntimeException e) {
          System.err.println("[sonarlint] warning: engine stop failed: " + e.getMessage());
        }
      }
    }
  }

  private static Map<String, Object> emptyReport(Path baseDir) {
    Map<String, Object> report = new LinkedHashMap<>();
    report.put("tool", TOOL_VERSION);
    report.put("project", baseDir.toString());
    report.put("filesAnalyzed", 0);
    report.put("issues", List.of());
    return report;
  }

  private static void writeReport(Map<String, Object> report, String out) throws IOException {
    String json = new ObjectMapper().writerWithDefaultPrettyPrinter().writeValueAsString(report);
    if (out != null) {
      Path outPath = Path.of(out);
      if (outPath.getParent() != null) {
        try {
          Files.createDirectories(outPath.getParent());
        } catch (java.nio.file.FileAlreadyExistsException e) {
          // 父目录已存在(如 macOS 的 /tmp 是符号链接),忽略
        }
      }
      Files.writeString(outPath, json, StandardCharsets.UTF_8);
      System.err.println("[sonarlint] report written to " + outPath.toAbsolutePath());
    } else {
      // 指定 --out 时只写文件(MCP 路径恒用 --out),避免把完整报告再序列化一份到 stdout 被丢弃
      System.out.println(json);
    }
  }

  private static Map<String, Object> toMap(Issue issue) {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("ruleKey", issue.getRuleKey());
    m.put("severity", issue.getSeverity() != null ? issue.getSeverity().name() : null);
    m.put("type", issue.getType() != null ? issue.getType().name() : null);
    m.put("file", issue.getInputFile() != null ? issue.getInputFile().relativePath() : null);
    m.put("line", issue.getStartLine());
    m.put("message", issue.getMessage());
    return m;
  }

  private static ClientInputFile onDisk(Path path, Path baseDir) {
    Path abs = path.toAbsolutePath().normalize();
    String rel = baseDir.relativize(abs).toString().replace('\\', '/');
    return new ClientInputFile() {
      @Override public String getPath() { return abs.toString(); }
      @Override public boolean isTest() { return false; }
      @Override public Charset getCharset() { return StandardCharsets.UTF_8; }
      @Override public String relativePath() { return rel; }
      @Override public URI uri() { return abs.toUri(); }
      @Override public InputStream inputStream() throws IOException {
        return Files.newInputStream(abs);
      }
      @Override public String contents() throws IOException {
        return Files.readString(abs, StandardCharsets.UTF_8);
      }
      @Override public <G> G getClientObject() { return null; }
    };
  }

  private static Path findJavaPlugin(Path pluginDir) throws IOException {
    if (!Files.isDirectory(pluginDir)) {
      return null;
    }
    try (Stream<Path> s = Files.list(pluginDir)) {
      return s.filter(p -> p.getFileName().toString().matches("sonar-java-plugin-.*\\.jar"))
        .findFirst().orElse(null);
    }
  }
}
