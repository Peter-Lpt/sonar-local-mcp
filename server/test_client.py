"""MCP 协议往返测试:初始化 → 工具列表 → 分析 → 过滤/分页 → 源码读取边界。

覆盖要点:
  [1] initialize / tools/list 握手
  [2] analyze_project:返回 JSON 必须完整可解析(回归项:结果过大被截断导致解析失败)
  [3] list_issues:severity 过滤 + offset 分页 + hint 提示
  [4] get_source_code:项目内正常读取;越界路径必须被拒绝
  [5] analyze_code_snippet:片段即席分析

用法: python test_client.py [--src <项目目录>] [--max-files N]
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent


def _ok(res) -> str:
    assert res.content and res.content[0].type == "text", "MCP 返回格式异常"
    return res.content[0].text


def _show(tag: str, text: str, head: int = 220):
    print(f"[{tag}]", text[:head].replace("\n", " ") + (" ..." if len(text) > head else ""))


async def main(project: str, max_files: int, java: str):
    # 与 .mcp.json 一致:通过 env 注入 SONAR_JAVA(不设置则回退 PATH 中的 java)
    env = dict(os.environ)
    if java:
        env["SONAR_JAVA"] = java
    elif not env.get("SONAR_JAVA"):
        print("[warn] SONAR_JAVA 未设置,将使用 PATH 中的 java —— 若低于 JDK 17 分析会失败;")
        print("       建议: python test_client.py --java <JDK17 的 java.exe 路径>")

    params = StdioServerParameters(
        command=sys.executable,
        args=["sonar_mcp_server.py"],
        cwd=str(HERE),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 1) 握手
            init = await session.initialize()
            print("[1] initialize OK:", init.serverInfo.name, init.serverInfo.version)
            tools = await session.list_tools()
            print("[1] tools/list OK:", [t.name for t in tools.tools])

            # 2) 分析:返回必须完整可解析(不因过大截断而报错)
            res = await session.call_tool("analyze_project", {
                "project_path": project, "max_files": max_files,
            })
            report = json.loads(_ok(res))  # 解析失败即断言失败
            print(f"[2] analyze_project OK: files={report['filesAnalyzed']} "
                  f"total={report['total']} shown={report['shown']} "
                  f"truncated={report['truncated']}")
            print(f"[2] summary: {json.dumps(report['summary'], ensure_ascii=False)}")
            assert not report["truncated"] or report["hint"], "截断时必须带 hint"

            # 3) 过滤 + 分页
            res = await session.call_tool("list_issues", {"severity": "CRITICAL", "limit": 5})
            d = json.loads(_ok(res))
            print(f"[3] list_issues(CRITICAL, limit=5): count={d['count']} returned={d['returned']}")
            assert all(i["severity"] == "CRITICAL" for i in d["issues"]), "severity 过滤失效"

            # 3b) 多级别集合 + 最低级别过滤
            res = await session.call_tool("list_issues", {"severity": "BLOCKER,CRITICAL,MAJOR"})
            d = json.loads(_ok(res))
            print(f"[3b] list_issues(set BLOCKER,CRITICAL,MAJOR): count={d['count']}")
            assert all(i["severity"] in ("BLOCKER", "CRITICAL", "MAJOR") for i in d["issues"]), "多级别集合过滤失效"

            res = await session.call_tool("list_issues", {"min_severity": "MAJOR"})
            d = json.loads(_ok(res))
            print(f"[3b] list_issues(min_severity=MAJOR): count={d['count']}")
            assert all(i["severity"] in ("BLOCKER", "CRITICAL", "MAJOR") for i in d["issues"]), "min_severity 过滤失效"

            res = await session.call_tool("list_issues", {"limit": 3, "offset": 0})
            page1 = json.loads(_ok(res))
            res = await session.call_tool("list_issues", {"limit": 3, "offset": 3})
            page2 = json.loads(_ok(res))
            ids1 = [(i["ruleKey"], i["file"], i["line"]) for i in page1["issues"]]
            ids2 = [(i["ruleKey"], i["file"], i["line"]) for i in page2["issues"]]
            assert not set(ids1) & set(ids2), "分页 offset 失效,两页内容重叠"
            print(f"[3] pagination OK: page1={len(ids1)} page2={len(ids2)} no overlap")

            # 3c) C1 回归:过滤后的翻页 hint 必须携带实际过滤条件,否则翻页丢过滤
            res = await session.call_tool("list_issues", {"min_severity": "CRITICAL", "limit": 1, "offset": 0})
            d = json.loads(_ok(res))
            if d["hint"]:
                assert "min_severity" in d["hint"], f"过滤翻页 hint 应携带 min_severity, got: {d['hint']}"
                print(f"[3c] filtered pagination hint carries filter OK: {d['hint'][:80]}")
            else:
                print("[3c] no truncation, hint empty (skip)")

            # 4) 源码读取边界
            first = report["issues"][0]["file"] if report["issues"] else None
            if first:
                res = await session.call_tool("get_source_code", {"file_path": first})
                text = _ok(res)
                print(f"[4] get_source_code(project-internal {first}) OK, len={len(text)}")
                assert not text.startswith('{"error"'), "项目内文件不应被拒绝"
            res = await session.call_tool("get_source_code", {"file_path": "..\\..\\outside.txt"})
            denied = json.loads(_ok(res))
            assert "error" in denied, "越界路径必须被拒绝"
            print(f"[4] get_source_code(outside) denied OK: {denied['error'][:60]}")

            # 5) 代码片段即席分析
            snippet = "public class Demo { private int unused; public static void main(String[] a){ System.out.println(1); } }"
            res = await session.call_tool("analyze_code_snippet", {"code": snippet})
            d = json.loads(_ok(res))
            print(f"[5] analyze_code_snippet OK: total={d['total']} shown={d['shown']}")
            assert d["total"] > 0, "片段应至少命中 1 条规则"

            print("\n== 协议往返全部通过 ==")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"F:\workspace\java\self\blog-project\blog")
    ap.add_argument("--max-files", type=int, default=30)
    ap.add_argument("--java", default="",
                    help="JDK 17+ 的 java 可执行文件路径(注入 SONAR_JAVA,模拟 .mcp.json 的 env)")
    a = ap.parse_args()
    asyncio.run(main(a.src, a.max_files, a.java))
