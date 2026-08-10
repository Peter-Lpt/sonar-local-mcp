# Third-Party Notices

本项目(sonar-local-mcp)自身代码以 [MIT](LICENSE) 许可发布。
此外,本项目嵌入并重新分发以下第三方开源组件,按其各自许可证条款使用与分发:

## sonarlint-core (SonarLint Core)

- **Home**: https://github.com/SonarSource/sonarlint-core
- **License**: GNU Lesser General Public License v3.0 (LGPL-3.0)
- **Copyright**: © 2009-2024 SonarSource SA
- **分发方式**:构建时经 maven-shade 合并进 fat jar `engine/target/sonar-local-mcp-*.jar`。
- **许可副本**:见 [LICENSES/LGPL-3.0.txt](LICENSES/LGPL-3.0.txt)。
- **重链接**:LGPL-3.0 要求允许使用者以 LGPL 源码重链接。本项目从源码构建(`cd engine && mvn package`)
  即可调用本仓库 pom 中声明的 sonarlint-core 源码重新生成 jar;如需替换该库,请以等价版本重新构建。

## sonar-java-plugin (SonarJava / Java Code Quality and Security)

- **Home**: https://github.com/SonarSource/sonar-java
- **License**: GNU Lesser General Public License v3.0 (LGPL-3.0)
- **Copyright**: © 2009-2024 SonarSource SA
- **分发方式**:以独立 jar 形式随发布提供(`engine/target/plugins/sonar-java-plugin-*.jar`),
  运行时经 `addPlugin(Path)` 加载。
- **许可副本**:见 [LICENSES/LGPL-3.0.txt](LICENSES/LGPL-3.0.txt)。

## 其他依赖(各自许可证)

构建产物 fat jar 还包含若干 Apache-2.0 / 其他许可的第三方库(如 Jackson、FastDoubleParser 等),
其许可副本保留在 jar 的 `META-INF/`(license.txt / NOTICE / LICENSE 等)。

## 说明

- 若仅限内网私有使用、不对外分发,上述义务可相应减免;一旦对外分发,请保留本文件、
  LGPL-3.0 许可副本与版权声明,并遵循 LGPL-3.0 的再许可与重链接要求。
- 本文件不构成法律意见;具体分发前请咨询法务。