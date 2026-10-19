# 实验报告批改 MCP Server

把本项目的批改能力通过 [Model Context Protocol](https://modelcontextprotocol.io) 暴露为工具，
供 Cursor / Claude Desktop / 其它 MCP 客户端直接调用。

## 暴露的工具

| 工具 | 说明 |
| --- | --- |
| `extract_report(path, max_chars=6000)` | 解析 PDF/DOCX/TXT/MD → 学生信息 + 逐页元数据 + 正文 |
| `get_default_rubric()` | 读取内置默认评分标准 |
| `grade_report(path, criteria, provider, api_key, model, base_url, engine, use_vision)` | 批改单份报告，`engine` ∈ `simple/agent/multi/framework` |
| `check_similarity(paths[], threshold=0.8)` | 多份报告两两 TF-IDF 查重 |
| `build_summary_excel(items_json, out_path)` | 评分结果导出为分组 Excel |

`api_key` 留空时自动读取对应环境变量（`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `CUSTOM_API_KEY`）。

## 依赖

```bash
pip install "mcp>=1.2"          # 1.x(FastMCP) 与 2.x(MCPServer) 均兼容
```

## 本地运行（stdio）

```bash
python -m mcp_server.server
```

## 在 Cursor 中配置

`.cursor/mcp.json`（或全局 `~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "lab-report-grader": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "D:/自动批改程序",
      "env": { "DEEPSEEK_API_KEY": "sk-..." }
    }
  }
}
```

## 在 Claude Desktop 中配置

`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "lab-report-grader": {
      "command": "python",
      "args": ["D:/自动批改程序/mcp_server/server.py"],
      "env": { "DEEPSEEK_API_KEY": "sk-..." }
    }
  }
}
```

> Windows 上若 `python` 不在 PATH，请用 Python 可执行文件的完整路径，例如
> `C:/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe`。

配置后重启客户端，即可让 agent 调用「解析报告 → 批改 → 查重 → 导出 Excel」全流程。
