# 08 - 第二层 Agent — SubCrew 与沙箱

> 本篇是 XiaoPaw"代码执行引擎"的实现。学完本篇，你会理解 Sub-Crew 是怎么在 Docker 沙箱里安全执行代码的，包括 MCP 协议、Docker 挂载原理，以及那个让无数人踩坑的 inode 问题。

---

## 本节学习目标

读完这一篇，你应该能够回答以下问题：

1. 什么是"零编排架构"？它和传统编排有什么区别？
2. MCP 协议是什么？AI 和 MCP Server 是怎么通信的？
3. 为什么必须用 `MCPServerHTTP` 而不是 `MCPServerSSE`？
4. Docker 的 bind mount（绑定挂载）是什么？为什么会因为 `rm -rf` 出问题？
5. Sub-Crew 在子线程运行时，ContextVar 是怎么传递的？
6. 沙箱权限不对会有什么后果？怎么排查？
7. Sub-Crew 的完整执行链路是怎样的？

如果你还没读 06、07 篇，强烈建议先读——本篇是它们的延续。

---

## 一、SubCrew 的设计理念

### 1.1 用"外卖店"类比理解零编排

想象两种开外卖店的方式：

**传统编排（中央调度）**：
- 店长（Orchestrator）说："先接单，再做菜，再打包，再送餐"
- 每一步都要店长安排，店长要懂所有流程
- 店长不在，店就开不了

**零编排（声明式）**：
- 每个厨师自带菜谱（SKILL.md）
- 顾客点单 → 服务员（SkillLoader）喊对应厨师 → 厨师照菜谱做
- 没有店长，每个厨师自己知道怎么做菜

XiaoPaw 用的是零编排：
```
传统编排：
  Orchestrator → "先搜索，再处理，最后回复"
  显式声明步骤（Orchestrator 要懂所有流程）

零编排：
  MainCrew → skill_loader(skill_name="baidu_search")
  SkillLoader 读 SKILL.md → 构造 SubCrew → 执行
  没有人显式编排，技能自己声明自己（SKILL.md 就是菜谱）
```

### 1.2 两层 Crew 的对比

| 特性 | Main Crew | Sub-Crew |
|------|-----------|----------|
| 职责 | 理解意图、选技能 | 执行单一技能 |
| 工具 | skill_loader | MCP 沙箱工具 |
| 运行环境 | 主进程 | Docker 容器 |
| 最大迭代 | 50 次 | 20 次（更严格，防失控） |
| 模型 | qwen3-max | qwen3-max |
| 上下文 | 历史对话 + 记忆 | 技能说明 + 任务上下文 |
| 执行线程 | 主线程 | 子线程（独立 event loop） |

**为什么 Sub-Crew 的 max_iter 更小（20 vs 50）？**
- Sub-Crew 是执行具体代码的，出问题影响大
- 20 次足够执行大部分技能（搜索、生成文档等）
- 防止 Sub-Crew 死循环烧钱

---

## 二、MCP 协议基础

### 2.1 用"点外卖"类比理解 MCP

MCP（Model Context Protocol，模型上下文协议）是一个让 AI 调用外部工具的标准协议。可以类比成"外卖平台"：

- **没有 MCP 时**：每个 AI 要自己写代码对接每个工具（百度搜索、文件系统、Shell...），N 个 AI × M 个工具 = N×M 个适配代码
- **有了 MCP 后**：工具方实现一个 MCP Server，AI 方实现一个 MCP Client，统一协议对接，N + M 个代码

```
AI Agent                     MCP Server（外卖平台）
   │                            │
   │ ── initialize ──────────→ │  "我是 AI，能连吗？"
   │ ←── capabilities ──────── │  "能，我提供这些能力"
   │                            │
   │ ── tools/list ──────────→ │  "你有哪些工具？"
   │ ←── tools ─────────────── │  "file_operations, execute_command..."
   │                            │
   │ ── tools/call ──────────→ │  "帮我执行 execute_command('ls')"
   │ ←── result ─────────────── │  "执行结果：file1.py file2.py"
```

### 2.2 MCP 的每步消息详解

#### 第 1 步：initialize（建立连接）

```json
// AI → MCP Server
{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "clientInfo": {"name": "crewai", "version": "0.5.0"}
  }
}

// MCP Server → AI
{
  "protocolVersion": "2024-11-05",
  "serverInfo": {"name": "aio-sandbox", "version": "1.0.0"},
  "capabilities": {"tools": {}}
}
```

**通俗解释**：AI 说"我是 CrewAI，协议版本 2024-11-05，能连吗？"，沙箱说"能，我是 AIO-Sandbox，提供 tools 能力"。

#### 第 2 步：tools/list（请求工具列表）

```json
// AI → MCP Server
{"jsonrpc": "2.0", "method": "tools/list", "params": {}}

// MCP Server → AI
{
  "tools": [
    {
      "name": "file_operations",
      "description": "读写文件、创建目录、删除文件",
      "inputSchema": {"type": "object", "properties": {...}}
    },
    {
      "name": "execute_command",
      "description": "执行 Shell 命令（受沙箱限制）",
      "inputSchema": {...}
    }
  ]
}
```

**通俗解释**：AI 问"你有哪些工具？"，沙箱回答"我有 file_operations、execute_command 等工具，参数格式如下"。

#### 第 3 步：tools/call（调用工具）

```json
// AI → MCP Server
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "execute_command",
    "arguments": {"command": "python search.py --query Python"}
  }
}

// MCP Server → AI
{
  "content": [{"type": "text", "text": "搜索结果：Python 3.12..."}]
}
```

**通俗解释**：AI 说"帮我执行 python search.py"，沙箱执行后返回"搜索结果是 Python 3.12..."。

### 2.3 AIO-Sandbox 的 MCP 工具

AIO-Sandbox 运行在 Docker 容器里，通过 MCP 协议提供以下能力：

| 工具 | 功能 | 通俗解释 |
|------|------|---------|
| `file_operations` | 文件读写、创建目录、删除文件 | 像文件管理器 |
| `str_replace_editor` | 文件内容替换（类似 diff） | 像编辑器的查找替换 |
| `execute_command` | 执行 Shell 命令（受沙箱限制） | 像终端，但有限制 |
| `list_directory` | 列出目录内容 | 像执行 `ls` |

### 2.4 为什么用 MCPServerHTTP 而不是 MCPServerSSE？

这是初学者最容易踩的坑之一。**必须用 `MCPServerHTTP`**，不能用 `MCPServerSSE`。

```python
# ✅ 正确：用 MCPServerHTTP（Streamable HTTP）
from crewai.mcp import MCPServerHTTP
sandbox_mcp = MCPServerHTTP(url="http://localhost:8030/mcp")

# ❌ 错误：用 MCPServerSSE（Server-Sent Events）
# sandbox_mcp = MCPServerSSE(url="http://localhost:8030/mcp")
```

**为什么？**

```
MCPServerHTTP（Streamable HTTP）：
  - 用 POST 请求通信
  - 发送请求 → 等待响应 → 返回
  - AIO-Sandbox 的 /mcp 端点支持这种方式
  - 正常工作

MCPServerSSE（Server-Sent Events）：
  - 用 GET 请求 + 持续事件流
  - 期望服务器持续推送事件
  - AIO-Sandbox 不支持这种方式
  - 沙箱几秒后关连接
  - CrewAI 卡在等 tools/list 响应
  - 5 分钟后超时
```

**症状**：用 MCPServerSSE 会让测试卡住 5 分钟才超时，错误信息不明显。如果你遇到"测试一直跑不完"，先检查这里。

---

## 三、SubCrew 实现

### 3.1 build_skill_crew 函数 —— 构建 Sub-Crew

```python
# 文件路径：xiaopaw/agents/skill_crew.py
"""Sub-Crew —— Skill 在沙箱中的执行单元（零编排协作）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from crewai import Agent, Crew, Process, Task
from crewai.hooks import ToolCallHookContext, before_tool_call, unregister_before_tool_call_hook
# 必须用 MCPServerHTTP（Streamable HTTP），不是 MCPServerSSE
from crewai.mcp import MCPServerHTTP

from xiaopaw.llm.aliyun_llm import AliyunLLM

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent / "config"
_DEFAULT_SANDBOX_MCP_URL = "http://localhost:8030/mcp"

# 需要转成 JSON 字符串的字段（LLM 有时传 dict 而不是 str）
_STRING_CONTENT_FIELDS = {"content", "file_text", "new_str"}


def _normalize_subcrew_tool_input(tool_input: dict) -> None:
    """Sub-Crew 工具参数归一化（原地修改）。

    为什么要归一化？
    Sub-Crew 的 LLM 有时把 dict 传给需要 str 的参数
    （比如 file_operations 的 content，str_replace_editor 的 file_text）。
    Pydantic 拒绝非字符串值 → 反复重试 → 烧时间预算。

    本函数把这些字段转成 JSON 字符串。

    参数：
        tool_input: 工具参数字典（原地修改）
    """
    for field in _STRING_CONTENT_FIELDS:
        val = tool_input.get(field)
        if isinstance(val, (dict, list)):
            # json.dumps 把 dict/list 转成 JSON 字符串
            # ensure_ascii=False 保留中文
            tool_input[field] = json.dumps(val, ensure_ascii=False)


def _format_cfg(cfg: dict, **kwargs) -> dict:
    """格式化配置中的模板变量。

    参数：
        cfg: 原始配置字典
        **kwargs: 模板变量（如 skill_name="baidu_search"）

    返回：
        dict: 格式化后的新字典

    示例：
        >>> _format_cfg({"role": "{skill_name} 执行者"}, skill_name="baidu_search")
        {'role': 'baidu_search 执行者'}
    """
    result = {}
    for k, v in cfg.items():
        if isinstance(v, str):
            # str.format 替换 {xxx} 占位符
            result[k] = v.format(**kwargs)
        else:
            result[k] = v    # 非字符串原样保留
    return result
```

### 3.2 构建 Sub-Crew 的完整代码

```python
def build_skill_crew(
    skill_name: str,                  # 技能名，如 "baidu_search"
    skill_instructions: str,          # 技能指令（SKILL.md 正文）
    session_id: str = "",             # 会话 ID
    sandbox_mcp_url: str = _DEFAULT_SANDBOX_MCP_URL,  # 沙箱地址
    sub_agent_model: str = "qwen3-max",  # Sub-Crew 用的 LLM
    max_iter: int = 20,               # 最大迭代次数
    allowed_tools: list[str] | None = None,  # 允许的工具（未实现）
) -> Crew:
    """构建 Sub-Crew。

    流程（8 步）：
    1. 校验沙箱 URL（防止 5min 超时）
    2. 创建 MCP 连接
    3. 准备会话目录
    4. 加载 Agent/Task 配置
    5. 格式化配置（替换模板变量）
    6. 创建 Agent（挂载 MCP）
    7. 创建 Task + 注册 Hook
    8. 创建 Crew

    参数：
        skill_name: 技能名
        skill_instructions: SKILL.md 正文（已替换占位符）
        session_id: 会话 ID
        sandbox_mcp_url: 沙箱 MCP 地址
        sub_agent_model: Sub-Crew 用的 LLM 模型名
        max_iter: 最大迭代次数（默认 20）
        allowed_tools: 允许的工具列表（未实现，预留）

    返回：
        Crew: 构建好的 CrewAI Crew 实例

    异常：
        ValueError: 沙箱 URL 为空或格式错误时抛出
    """
    # ── 1. 校验沙箱 URL ──
    # 为什么要校验？空或畸形 URL 会导致 httpx.UnsupportedProtocol，
    # 被 anyio TaskGroup 吞掉后表现为 5 分钟超时（错误信息不明显）
    if not sandbox_mcp_url or not sandbox_mcp_url.startswith(("http://", "https://")):
        raise ValueError(
            f"build_skill_crew: sandbox_mcp_url must be an http(s) URL, got "
            f"{sandbox_mcp_url!r}. Empty or malformed URLs cause httpx.UnsupportedProtocol "
            f"deep inside Sub-Crew, which manifests as a 5-minute TestAPI timeout. "
            f"Pass a valid URL (e.g. http://localhost:8030/mcp) or skip skill execution."
        )

    # ── 2. 创建 MCP 连接 ──
    # MCPServerHTTP 会在第一次调用时自动连接（lazy）
    sandbox_mcp = MCPServerHTTP(url=sandbox_mcp_url)
    skill_llm = AliyunLLM(model=sub_agent_model, region="cn", temperature=0.3)

    # ── 3. 准备会话目录 ──
    # 沙箱里的路径（不是宿主机路径！）
    session_dir = f"/workspace/sessions/{session_id}" if session_id else "/workspace"

    # ── 4. 加载 Agent/Task 配置 ──
    agents_cfg = yaml.safe_load((_CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))
    tasks_cfg = yaml.safe_load((_CONFIG_DIR / "tasks.yaml").read_text(encoding="utf-8"))

    # ── 5. 格式化配置（替换模板变量）──
    # 把 agents.yaml 里的 {skill_name} 等占位符替换成实际值
    agent_cfg = _format_cfg(
        agents_cfg["skill_agent"],
        skill_name=skill_name,
        skill_name_upper=skill_name.upper(),    # 如 "BAIDU_SEARCH"
        session_dir=session_dir,
        skill_instructions=skill_instructions,  # SKILL.md 正文塞进 backstory
    )
    agent_cfg["max_iter"] = max_iter

    # ── 6. 创建 Agent（挂载 MCP）──
    skill_agent = Agent(
        **agent_cfg,
        llm=skill_llm,
        mcps=[sandbox_mcp],    # ★ 挂载沙箱 MCP（Sub-Crew 的核心）
        verbose=True,
    )

    # ── 7. 创建 Task + 注册 Hook ──
    task_cfg = _format_cfg(tasks_cfg["skill_task"], session_dir=session_dir)
    skill_task = Task(**task_cfg, agent=skill_agent)

    # 注册工具调用前 Hook（参数归一化）
    @before_tool_call
    def _subcrew_tool_hook(context: ToolCallHookContext) -> bool | None:
        """Sub-Crew 工具调用前的 Hook。

        职责：
        1. 归一化工具参数（dict → JSON 字符串）
        2. 通过 CrewObservabilityAdapter 触发安全检查（如果 adapter 存在）

        什么时候触发？
        Sub-Crew 的 Agent 每次要调用 MCP 工具前。
        """
        _normalize_subcrew_tool_input(context.tool_input)
        return None    # 返回 None 表示"继续执行"

    # ── 8. 创建 Crew ──
    crew = Crew(
        agents=[skill_agent],
        tasks=[skill_task],
        process=Process.sequential,    # 顺序执行
        verbose=True,
        step_callback=_make_subcrew_step_callback(),    # 触发 Hook 事件
    )

    # 保存 hook 引用用于后续注销（防止内存泄漏）
    crew._subcrew_tool_hook = _subcrew_tool_hook
    return crew
```

### 3.3 Sub-Crew 的 step_callback

```python
def _make_subcrew_step_callback() -> Callable[[Any], Awaitable[None]]:
    """生成 Sub-Crew 的 step_callback。

    与 Main Crew 的 step_callback 的区别：
    - 不发送"思考中"消息（Sub-Crew 没有直接与用户通信的渠道）
    - 仍然触发 AFTER_TURN + pending_deny 重抛

    什么时候触发？
    Sub-Crew 的 Agent 每完成一个 step 后触发。

    返回：
        async 回调函数
    """
    from crewai.agents.parser import AgentAction, AgentFinish
    from xiaopaw.hook_framework.crew_adapter import get_current_adapter

    async def _callback(step_output: Any) -> None:
        """真正的回调函数。

        参数：
            step_output: 本步输出（AgentAction 或 AgentFinish）
        """
        # 从 ContextVar 取 adapter（由 copy_context 传递过来）
        adapter = get_current_adapter()
        if not adapter:
            return    # 没有 adapter，直接返回

        # 提取步骤输出文本
        step_text = ""
        if isinstance(step_output, AgentAction):
            # Agent 还在行动中
            step_text = str(step_output.text or step_output.thought or "")
        elif isinstance(step_output, AgentFinish):
            # Agent 完成了
            step_text = str(getattr(step_output, "output", "") or "")

        # 触发 AFTER_TURN 事件（loop_detector 用它检测循环）
        adapter.dispatch_after_turn(output=step_text[:2000])

        # pending_deny 重抛（与 Main Crew 逻辑一致，详见 06 篇）
        if adapter._pending_deny:
            pending = adapter._pending_deny
            adapter._pending_deny = None
            raise pending

    return _callback
```

---

## 四、Docker 沙箱详解

### 4.1 沙箱架构图

```
┌───────────────────────────────────────────────────────┐
│  宿主机（你的电脑/服务器）                              │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │  XiaoPaw 主进程                                  │  │
│  │  ├── MainCrew（主 Agent）                        │  │
│  │  └── SkillLoader → MCP HTTP 调用                 │  │
│  │       │                                          │  │
│  │       └──→ http://localhost:8030/mcp            │  │
│  └───────┼─────────────────────────────────────────┘  │
│          │  端口映射 8030:8030                         │
│          │                                            │
│  ┌───────▼─────────────────────────────────────────┐  │
│  │  Docker 容器 (AIO-Sandbox)                      │  │
│  │  ├── MCP Server (监听端口 8030)                  │  │
│  │  ├── 文件系统: /workspace                        │  │
│  │  │   ├── user.md (用户记忆文件)                  │  │
│  │  │   ├── agent.md (助手记忆文件)                 │  │
│  │  │   ├── sessions/                               │  │
│  │  │   │   └── session-xxx/                        │  │
│  │  │   │       ├── uploads/      (用户上传文件)    │  │
│  │  │   │       └── outputs/      (技能输出文件)    │  │
│  │  │   └── soul.md, memory.md ...                  │  │
│  │  ├── 技能脚本: /mnt/skills (只读挂载)            │  │
│  │  │   └── baidu_search/scripts/search.py         │  │
│  │  ├── 进程隔离 (不能影响宿主机)                    │  │
│  │  └── 网络限制 (不能访问内网)                      │  │
│  └─────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**通俗解释**：
- 宿主机是你的电脑
- Docker 容器像一个"虚拟小电脑"跑在你电脑里
- 主进程通过 HTTP 跟容器通信（端口 8030）
- 容器里有文件系统、能执行代码，但和宿主机隔离

### 4.2 Docker Compose 配置

```yaml
# 文件路径：sandbox-docker-compose.yaml
services:
  aio-sandbox:
    image: registry.cn-hangzhou.aliyuncs.com/aio/aio-sandbox:latest
    container_name: xiaopaw-aio-sandbox
    ports:
      - "8030:8030"    # MCP 端口映射（宿主机:容器）
    volumes:
      # 技能目录挂载（只读，防止容器修改技能脚本）
      - ./xiaopaw/skills:/mnt/skills:ro
      # 工作区挂载（读写，用户记忆文件在这里）
      - ./data/workspace:/workspace
    environment:
      - WORKSPACE_DIR=/workspace
    restart: unless-stopped    # 崩溃自动重启
```

**逐行解释**：
- `image`：用哪个 Docker 镜像
- `ports: "8030:8030"`：把容器的 8030 端口映射到宿主机的 8030 端口（这样 `localhost:8030` 能访问容器）
- `volumes`：目录挂载（详见 4.3）
- `:ro`：read-only，只读挂载
- `restart: unless-stopped`：除非手动停止，否则崩溃自动重启

### 4.3 挂载机制详解

Docker 的 volume 挂载让宿主机和容器共享文件：

```
宿主机路径                    容器内路径               作用
───────────────              ─────────              ────
./xiaopaw/skills/     →      /mnt/skills/          技能脚本（只读）
./data/workspace/     →      /workspace/           用户文件（读写）

为什么这样挂载？
- 技能脚本只读：防止容器里的代码改技能脚本（安全）
- 工作区读写：用户记忆文件、上传文件、输出文件都要写

Sub-Crew 在沙箱里执行时：
  cd /mnt/skills/baidu_search/scripts/
  python search.py --query "Python 新特性"
  结果写到 /workspace/sessions/xxx/outputs/
  → 宿主机 data/workspace/sessions/xxx/outputs/ 同步可见
```

**通俗类比**：
- 技能脚本挂载（只读）：像图书馆借书，你能看但不能改
- 工作区挂载（读写）：像你自己的笔记本，能看能写

### 4.4 inode 与 bind mount 的坑 ★★★

这是让无数人踩坑的问题，一定要理解。

#### 什么是 inode？

inode（index node）是 Unix 文件系统的"文件身份证号"。每个文件/目录都有一个唯一的 inode 号。文件名只是给人类看的，系统内部用的是 inode。

```bash
# 查看文件的 inode 号
ls -i data/workspace
# 输出：1234567 data/workspace
#         ↑ 这就是 inode 号
```

#### 为什么 `rm -rf` 会出问题？

Docker 的 bind mount（绑定挂载）绑定的是**目录的 inode**，不是路径。

```
初始状态：
  宿主机: data/workspace/ (inode=12345)
                    ↓ 绑定
  容器:   /workspace/ (指向 inode=12345)

执行 rm -rf data/workspace + mkdir data/workspace：
  宿主机: data/workspace/ (inode=67890) ← 新目录，新 inode！
  容器:   /workspace/ (还指向 inode=12345) ← 旧 inode，已删除！

结果：容器写文件 → 写到已删除的 inode（实际不存在）
      宿主机写文件 → 写到新 inode（容器看不到）
      双方互相看不到对方的文件！
```

**图解**：

```
【初始状态】
宿主机 inode=12345  ←──bind mount──→  容器 /workspace
     data/workspace/                     /
     ├── user.md                        ├── user.md
     └── sessions/                      └── sessions/

【错误操作：rm -rf data/workspace && mkdir data/workspace】
宿主机 inode=67890（新建的）           容器 /workspace（还指向 inode=12345）
     data/workspace/                     /（孤儿，实际不存在）
     ├── user.md                        ├── ???（看不到新文件）
     └── sessions/                      └── ???

  ↑ 两边互相看不到对方的文件！
```

#### 正确做法

```bash
# ❌ 错误做法：删除目录再创建
rm -rf data/workspace
mkdir data/workspace
# Docker bind mount 绑定的是 inode
# 删掉再 mkdir 创建了新 inode，但容器还指向旧（已删）inode

# ✅ 正确做法：只删内容，保留目录
rm -rf data/workspace/*
rm -rf data/workspace/.[!.]*    # 删除隐藏文件（如 .gitignore）

# 如果已经 rm -rf 了，修复方法：
docker compose -f sandbox-docker-compose.yaml restart
# 重启容器会重新绑定当前 inode
```

**记忆口诀**：**只删内容，不删目录；要是删了，重启容器**。

---

## 五、Sub-Crew 执行流程

### 5.1 完整执行链路时序图

```
1. MainCrew 决定调用 skill_loader
   │  skill_loader(skill_name="baidu_search", task_context="搜索 Python")
   ▼
2. SkillLoaderTool._run() 被调用（主线程）
   │
   ├─ 快照父线程 ContextVar（copy_context）
   │  - adapter, trace_id, span 栈等
   │
   └─ 提交到子线程（ThreadPoolExecutor）
       │
       ▼
3. 子线程内：
   │
   ├─ 重置 Langfuse ContextVar
   │  - 保留 trace_id（同一棵树）
   │  - 重置 span 栈（从空开始）
   │
   ├─ build_skill_crew() 构建 Sub-Crew
   │  ├─ 创建 MCPServerHTTP 连接（lazy，第一次调用时连）
   │  ├─ 创建 Agent（挂载 MCP）
   │  │  └─ backstory 塞入 SKILL.md 指令
   │  ├─ 创建 Task
   │  └─ 创建 Crew
   │
   ├─ crew.akickoff(inputs={task_context, skill_name})
   │  │
   │  ├─ LLM 思考：读技能说明，决定执行步骤
   │  │  └─ before_llm_hook 触发（如果有）
   │  │
   │  ├─ Agent 决定调用 MCP 工具
   │  │  ├─ before_tool_hook 触发（参数归一化）
   │  │  │
   │  │  ├─ 调用 MCP 工具（通过 HTTP）：
   │  │  │  ├─ tools/call: execute_command("python search.py")
   │  │  │  │  ├─ 沙箱接收请求
   │  │  │  │  ├─ 在容器内执行命令
   │  │  │  │  └─ 返回结果
   │  │  │  │
   │  │  │  ├─ tools/call: file_operations("read", "output.json")
   │  │  │  │  └─ 读取结果文件
   │  │  │  │
   │  │  │  └─ tools/call: str_replace_editor(...)
   │  │  │     └─ 编辑文件
   │  │  │
   │  │  └─ 每步触发 step_callback
   │  │     └─ 触发 AFTER_TURN 事件 + pending_deny 检查
   │  │
   │  ├─ LLM 看到执行结果，思考下一步
   │  │
   │  └─ Task 完成 → 返回执行结果
   │
   ├─ flush Langfuse 事件（推送到 Langfuse 服务）
   │
   └─ 关闭 event loop
       │
       ▼
4. 返回结果给 MainCrew（通过 future.result）
   │
   ▼
5. MainCrew 看到 skill_loader 的返回值
   │
   ▼
6. MainCrew 整理后回复用户
```

### 5.2 子线程中的 ContextVar 状态

```python
# 父线程的 ContextVar 快照
ctx = contextvars.copy_context()

# 快照内容：
# - _current_adapter = <CrewObservabilityAdapter>  ← 共享（故意）
#   Sub-Crew 要用同一个 adapter（共用 _pending_deny）
# - _trace_id_var = "s-session-xxx"                ← 共享（同一棵树）
#   Sub-Crew 的 trace 必须挂在同一棵树上
# - _span_stack_var = (父栈内容)                    ← 重置（子线程从空栈开始）
#   子线程的 push/pop 不应污染主线程栈
# - _gen_id_var = "gen-xxx"                        ← 重置（子线程无未关闭 gen）
#   子线程没有未关闭的 LLM generation

# 在子线程中执行：
ctx.run(_run_with_cleanup)
# _run_with_cleanup 内部看到的 ContextVar = 快照值

# 子线程重置：
_reset_langfuse_contextvars(parent_span_id)
# - _trace_id_var 不变（保持同一棵 trace 树）
# - _root_span_id_var = parent_span_id（挂在父 span 下）
#   让子 trace 自动挂在父 skill span 之下
# - _gen_id_var = ""（重置，没有未关闭的 gen）
# - _span_stack_var = ()（重置，空栈）
```

**为什么"既 copy_context 又部分 reset"？**

- **copy_context**：让 adapter / trace_id 这些"应该共享"的 ContextVar 自动到位
- **部分 reset**：但 _span_stack_var / _gen_id_var 是"父线程当前正在做什么"的瞬时状态，子线程要从空栈、空 gen 重新开始累积，否则会出现：
  - 父线程的 LLM gen 被子线程当成自己的 → 关闭时机错乱
  - 父线程的 span 栈被子线程 push/pop → 主线程后续看到脏栈

---

## 六、安全注意事项

### 6.1 沙箱权限控制

```python
# 沙箱以非 root 用户运行（gem 用户，UID 1000）
# 限制了以下能力：
# - 不能访问宿主文件系统（只能写 /workspace）
#   即使容器被攻破，也只能看到 /workspace 下的文件
# - 不能修改网络配置
#   防止恶意代码扫描内网
# - 不能安装系统级软件
#   防止安装后门程序
# - 进程资源受限（CPU/内存/时间）
#   防止死循环烧光资源
```

**通俗类比**：沙箱像一个"隔离病房"——病人在里面治病（执行代码），但不能出来乱跑（不能访问宿主机其他文件）。

### 6.2 workspace 权限问题 ★★★

这是另一个常见坑。沙箱里的 `gem` 用户（UID 1000）需要能写 `/workspace`，但宿主机的文件可能权限不对。

```bash
# 沙箱内 gem 用户需要能写 /workspace
# 宿主机需要设置正确权限：

# Linux / macOS:
chmod -R 777 data/workspace/         # 递归改权限
chmod 666 data/workspace/*.md        # 记忆文件改成可写
```

**权限不对的后果（完整排查步骤）**：

```
症状：用户说"记住我叫张三"，但下次会话 Agent 不知道

排查步骤：
1. 检查 memory-save 技能是否成功
   → 看日志有没有 "Permission denied" 错误

2. 检查 user.md 是否被写入
   → ls -la data/workspace/user.md
   → 看修改时间是不是刚刚

3. 如果没写入，检查权限
   → ls -la data/workspace/
   → 如果是 root:root，就是权限问题

4. 修复权限
   → chmod -R 777 data/workspace/
   → chmod 666 data/workspace/*.md

5. 更隐蔽的问题：LLM "创意"地绕道写入
   症状：memory-save 报权限错误
   LLM 想："写不了 user.md？那我写到子目录试试"
   → 写到 data/workspace/sessions/xxx/user.md
   → Bootstrap 读 user.md 看不到新内容
   → 用户感觉"记住了"但下次会话召回失败

6. 验证修复
   → 重新发"记住我叫张三"
   → 重启服务
   → 发"我叫什么"
   → Agent 应该回答"张三"
```

---

## 七、设计优势与局限性

### 优势

1. **安全隔离**：代码在 Docker 容器里执行，不影响宿主
   - 即使代码是恶意的（如 `rm -rf /`），也只能删容器里的文件
2. **零编排**：技能自带描述，不需要中央编排
   - 新增技能只需加 SKILL.md，不用改编排逻辑
3. **资源控制**：限制迭代次数和超时，防止失控
   - max_iter=20 + 5 分钟超时双重保险
4. **追踪完整**：ContextVar 跨线程传递保证 trace 完整
   - Langfuse 能看到完整的调用树

### 局限性

1. **启动延迟**：每次调用 Sub-Crew 需要建立 MCP 连接（1-2s）
   - 对实时性要求高的场景不友好
2. **资源消耗**：每个 Sub-Crew 独立线程 + 事件循环
   - 高并发时线程数可能爆炸
3. **调试困难**：子线程出问题不易定位
   - 需要 Langfuse 辅助查看 trace
4. **Docker 依赖**：必须装 Docker，部署门槛高
   - Windows/Mac 开发体验不如 Linux

---

## 八、❓ 常见问题

### Q1：零编排架构和传统编排有什么区别？

**A**：
- **传统编排**：有一个中央编排者（Orchestrator），显式声明工作流（A → B → C）。编排者要懂所有流程。
- **零编排**：每个技能自带描述（SKILL.md），不需要中央编排者。MainCrew 只负责选技能，技能自己知道怎么做。

类比：传统编排像工厂流水线（每个工位有固定工序），零编排像外包（找到合适的外包团队，他们自己知道怎么做）。

### Q2：为什么必须用 MCPServerHTTP 而不是 MCPServerSSE？

**A**：因为 AIO-Sandbox 的 `/mcp` 端点实现的是 Streamable HTTP（POST 请求），不是 SSE（GET + 持续事件流）。用 MCPServerSSE 会：
1. 发 GET /mcp 期望持续事件流
2. 沙箱几秒后关连接（不支持 SSE）
3. CrewAI 卡在等 tools/list 响应
4. 5 分钟超时

详见 2.4 节。

### Q3：Docker bind mount 的 inode 机制是什么？为什么要小心 rm -rf？

**A**：Docker bind mount 绑定的是目录的 inode（文件系统身份证号），不是路径。如果 `rm -rf data/workspace` 然后 `mkdir data/workspace`，新目录是新 inode，但容器还指向旧（已删）inode，导致双方互相看不到文件。

正确做法：`rm -rf data/workspace/*`（只删内容，保留目录）。如果已经删了，`docker compose restart` 重启容器重新绑定。

详见 4.4 节。

### Q4：Sub-Crew 在子线程运行时，ContextVar 是怎么传递的？

**A**：通过 `copy_context()` + `ctx.run()`：
1. 主线程调用 `ctx = contextvars.copy_context()` 快照所有 ContextVar
2. `pool.submit(ctx.run, fn)` 提交到子线程
3. `ctx.run(fn)` 让子线程看到快照值
4. 子线程里再 `_reset_langfuse_contextvars()` 部分重置

详见第 07 篇 4.4 节。

### Q5：为什么要"部分重置"ContextVar 而不是全部保留或全部清空？

**A**：
- **全部保留**：父线程的 span 栈会被子线程污染（push/pop 错乱）
- **全部清空**：adapter 和 trace_id 没了，Sub-Crew 的 Hook 全失效
- **部分重置**：adapter/trace_id 共享（Sub-Crew 要用），span 栈/gen_id 重置（瞬时状态不应共享）

### Q6：沙箱里的代码能访问宿主机的文件吗？

**A**：正常情况下不能。Docker 容器是隔离的，只能看到挂载进来的目录（`/workspace` 和 `/mnt/skills`）。但如果：
- Docker 配置错误（挂载了 `/`）
- 容器以 root 运行且有特权模式

就可能突破隔离。XiaoPaw 用非 root 用户（gem，UID 1000）+ 限制挂载，保证安全。

### Q7：Sub-Crew 执行超时了怎么办？

**A**：`future.result(timeout=300)` 会在 5 分钟后抛 `TimeoutError`。处理方式：
- 检查沙箱是否正常（`docker ps` 看容器是否在跑）
- 检查脚本是否死循环（看沙箱日志）
- 调整超时时间（如果确实需要更长）
- cost_guard 会在超时前先检查 token 消耗

### Q8：怎么调试 Sub-Crew 的问题？

**A**：
1. **看 Langfuse trace**：能看到完整的调用链，哪个 step 出问题
2. **看沙箱日志**：`docker logs xiaopaw-aio-sandbox`
3. **加日志**：在 `_execute_skill_async` 里加 print/logger
4. **单独测试沙箱**：用 curl 直接调 MCP 端点

详见第九节调试技巧。

---

## 九、🔧 调试技巧

### 9.1 检查沙箱是否正常

```bash
# 1. 检查容器是否在运行
docker ps | grep aio-sandbox

# 2. 检查端口是否通
curl http://localhost:8030/mcp
# 应该返回一些 MCP 响应

# 3. 看容器日志
docker logs xiaopaw-aio-sandbox --tail 50
```

### 9.2 完整的权限排查步骤

```bash
# 1. 检查 workspace 目录权限
ls -la data/workspace/
# 应该看到 drwxrwxrwx（777）

# 2. 检查记忆文件权限
ls -la data/workspace/*.md
# 应该看到 -rw-rw-rw-（666）

# 3. 检查 inode（确认没被 rm -rf 破坏）
ls -i data/workspace
# 记下这个 inode 号

# 4. 进容器检查
docker exec -it xiaopaw-aio-sandbox bash
# 在容器内：
ls -la /workspace/
# 应该和宿主机看到一样的文件

# 5. 如果权限不对，修复
chmod -R 777 data/workspace/
chmod 666 data/workspace/*.md

# 6. 如果 inode 不匹配，重启容器
docker compose -f sandbox-docker-compose.yaml restart
```

### 9.3 测试 MCP 连接

```python
# 单独测试 MCP 连接（不通过 CrewAI）
import requests

# 测试沙箱是否响应
resp = requests.post(
    "http://localhost:8030/mcp",
    json={
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {},
        "id": 1
    },
    headers={"Content-Type": "application/json"}
)
print(resp.json())
# 应该返回工具列表
```

### 9.4 调试 Sub-Crew 执行

在 `_execute_skill_async` 里加日志：

```python
async def _execute_skill_async(self, skill_name, task_context):
    logger.info(f"开始执行技能: {skill_name}")
    logger.info(f"任务上下文: {task_context}")

    # ... 原代码 ...

    try:
        result = await crew.akickoff(inputs=inputs)
        logger.info(f"技能执行完成: {result}")
        return str(result)
    except Exception as e:
        logger.error(f"技能执行失败: {e}", exc_info=True)
        raise
    finally:
        # ... 清理代码 ...
```

### 9.5 CrewAI 常见报错与解决

| 报错 | 原因 | 解决 |
|------|------|------|
| `httpx.UnsupportedProtocol` | sandbox_url 格式错误 | 确保是 `http://` 或 `https://` 开头 |
| `TimeoutError` after 5min | MCP 连接卡住 | 检查用 MCPServerHTTP（不是 SSE） |
| `Permission denied` | 沙箱内写文件失败 | `chmod -R 777 data/workspace/` |
| `FileNotFoundError` | 容器里找不到文件 | 检查挂载是否正确 |
| `Connection refused` | 沙箱没启动 | `docker compose up -d` |
| `docker: Cannot connect` | Docker 没装/没启动 | 启动 Docker Desktop |
| `Max iterations exceeded` | Sub-Crew 死循环 | 检查 SKILL.md 指令是否清晰 |
| `ContextVar 不可见` | copy_context 没用 | 检查 `pool.submit(ctx.run, fn)` |

### 9.6 用 Langfuse 查看完整 trace

配置 Langfuse 后，可以看到：
- **父 span**：`tool-skill_baidu_search`（主线程的 skill_loader 调用）
- **子 span**：`gen-llm-1`（Sub-Crew 的第一次 LLM 调用）
- **子 span**：`tool-sandbox_execute_command`（Sub-Crew 调沙箱工具）

如果子 span 没挂在父 span 下，说明 ContextVar 传递有问题。详见第 12 篇《观测层》。

### 9.7 单独测试某个技能

```python
# 不通过 MainCrew，直接测试 SkillLoaderTool
import asyncio
from xiaopaw.tools.skill_loader import SkillLoaderTool

async def test():
    tool = SkillLoaderTool(
        session_id="test-session",
        sandbox_url="http://localhost:8030/mcp",
        routing_key="test",
        history_all=[],
    )
    result = tool._run("baidu_search", "搜索 Python 3.12")
    print(result)

asyncio.run(test())
```

---

## 十、验证你的理解

- [ ] 零编排架构和传统编排有什么区别？各自的优缺点？
- [ ] MCP 协议的 3 步消息（initialize/tools/list/tools/call）分别是什么？
- [ ] 为什么必须用 MCPServerHTTP 而不是 MCPServerSSE？
- [ ] Docker bind mount 的 inode 机制是什么？为什么要小心 rm -rf？
- [ ] Sub-Crew 在子线程运行时，ContextVar 是怎么传递的？
- [ ] 为什么要"部分重置"ContextVar 而不是全部保留或全部清空？
- [ ] 沙箱权限不对会有什么后果？怎么排查？
- [ ] Sub-Crew 的完整执行链路能画出来吗？

---

## 十一、下一步

到这里，你已经理解了 XiaoPaw 的"两层 Crew"架构：
- **MainCrew**：理解用户、选技能（第 06 篇）
- **SkillLoader**：渐进式披露、加载技能（第 07 篇）
- **SubCrew + 沙箱**：执行代码、隔离环境（本篇）

下一篇我们会讲"三层记忆系统"——Agent 是怎么记住用户的偏好、历史对话、长期知识的。

> 下一篇：[09-三层记忆系统](./09-三层记忆系统.md)
