# XiaoPaw v2 → LangChain/LangGraph 完整迁移方案

## Context（背景与目标）

原项目 `xiaopaw-v2` 是一个基于 **CrewAI** 的飞书 AI 工作助手（极客时间《企业级多智能体设计实战》课程代码），采用"两层 Crew + Hook 加固层"架构。本任务要求：**在不修改现有项目代码的前提下**，新建一个完全独立、自包含的 LangChain（含 LangGraph）版本，1:1 迁移全部功能，并利用 LangChain v1 的核心能力（`create_agent` + Middleware + Subagents 模式 + MCP Adapters）。

**关键技术结论**（已通过 LangChain 官方文档 MCP 核实）：
- CrewAI 的 `Crew/Agent/Task` → LangChain v1 的 `create_agent(model, tools, system_prompt, middleware, response_format)`
- 两层 Crew（Main + Sub-Crew）→ **Subagents 模式**：`skill_loader` 是一个 `@tool`，内部调用由 `create_agent` 构建的子 Agent
- CrewAI 的 `MCPServerHTTP`（沙箱）→ `langchain-mcp-adapters` 的 `MultiServerMCPClient`（`transport="http"`）
- `AliyunLLM`（自定义 DashScope 适配）→ `ChatOpenAI`（DashScope 是 OpenAI 兼容端点，无需自定义类）
- **Hook 框架（5+2 事件）→ LangChain Middleware**（`AgentMiddleware`）；`shared_hooks/` 的 9 个策略 handler 保持不变（它们只依赖 `HookRegistry`，与 CrewAI 无关）
- **`pending_deny` 机制可完全消除**：原项目需要它是因为 CrewAI 会吞掉 `@before_tool_call` 抛出的异常；而 LangChain 的 `wrap_tool_call(request, handler)` 是真正的包装器，`GuardrailDeny` 可直接传播——这是迁移中最大的简化点

**新目录位置**：`d:\ProjectsCodes\企业级智能体实战\xiaopaw-v2\xiaopaw-langchain\`
（因工作区可写路径限制在 `xiaopaw-v2` 内，故作为其下自包含子目录；不触碰任何现有文件。如需移到仓库外，可直接整体拷贝该子目录。）

---

## 核心架构映射表

| 原项目（CrewAI） | 新项目（LangChain/LangGraph） | 说明 |
|---|---|---|
| `Crew(agents, tasks, Process.sequential)` | `create_agent(model, tools, system_prompt, middleware, response_format)` | LangChain v1 标准方式 |
| `Agent` + `Task`(main_task) | `create_agent` 的 system_prompt + tools | Task 内容并入 prompt |
| `MemoryAwareCrew.run_and_index` | `main_agent.ainvoke()` + `after_agent` 落库 | runner 调用 |
| `SkillLoaderTool(BaseTool)` + Sub-Crew | `@tool skill_loader` 包裹子 Agent（Subagents 模式） | 官方推荐的多 Agent 模式 |
| `skill_crew.build_skill_crew` + `MCPServerHTTP` | `create_agent` 子 Agent + `MultiServerMCPClient.get_tools()` | 沙箱 MCP 工具加载 |
| `AliyunLLM(BaseLLM)` | `ChatOpenAI(base_url=DashScope端点, api_key, model)` | OpenAI 兼容，无需自定义 |
| `CrewObservabilityAdapter`（crew_adapter） | `HookMiddleware(AgentMiddleware)` 单个中间件 | 调用**未改动**的 `HookRegistry` |
| `@before_tool_call` + pending_deny | `wrap_tool_call`（直接 raise GuardrailDeny） | pending_deny 消除 |
| `@before_llm_call`（restore session/compress） | `before_model` 中间件 + `SummarizationMiddleware` | |
| `step_callback`（AFTER_TURN + 重抛） | `after_model` 中间件（直接 raise） | |
| `task_callback`（TASK_COMPLETE） | `after_agent` 中间件 | |
| `on_turn_start`/`cleanup`（BEFORE_TURN/SESSION_END） | 保留在 runner（非 Agent 循环事件） | |
| `before_llm_call` 注入 bootstrap prompt | `before_model` 修改 system_message | |

### 5+2 事件 → Middleware Hook 映射

| EventType | 触发位置 | Middleware Hook | gate/observe |
|---|---|---|---|
| BEFORE_TURN | runner `_handle` 开头 | runner dispatch（保留） | observe |
| BEFORE_LLM | 每次 LLM 调用前 | `before_model` | observe |
| BEFORE_TOOL_CALL | 工具执行前 | `wrap_tool_call`（调 handler 前） | **gate**（dispatch_gate） |
| AFTER_TOOL_CALL | 工具执行后 | `wrap_tool_call`（调 handler 后） | observe |
| AFTER_TURN | 每轮结束 | `after_model` | **gate**（cost/loop） |
| TASK_COMPLETE | Agent 完成 | `after_agent` | observe |
| SESSION_END | runner finally | runner dispatch（保留） | observe |

---

## 新项目目录结构

```
xiaopaw-langchain/
├── pyproject.toml                  # 新：langchain/langgraph/langchain-mcp-adapters 依赖
├── README.md                       # 新：迁移说明 + 运行方式
├── config.yaml.example             # 复制
├── schema.sql                      # 复制
├── sandbox-docker-compose.yaml     # 复制
├── pgvector-docker-compose.yaml    # 复制
├── workspace-init/                 # 复制（记忆四件套模板）
├── skills/                         # 复制（13 技能 SKILL.md + 脚本，框架无关）
├── shared_hooks/                   # 复制（9 策略 handler + hooks.yaml，框架无关）
├── xiaopaw_lc/                     # 主包（lc = langchain）
│   ├── __init__.py
│   ├── main.py                     # 改写：agent_fn / 中间件装配
│   ├── runner.py                   # 轻改：adapter→middleware，BEFORE_TURN/SESSION_END 保留
│   ├── models.py                   # 复制
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── main_agent.py           # 新：create_agent 主 Agent + build_agent_fn
│   │   ├── skill_agent.py          # 新：create_agent 子 Agent + MCP 沙箱
│   │   ├── prompts.py              # 新：从 agents.yaml/tasks.yaml 提取的 prompt 构建
│   │   └── config/agents.yaml, tasks.yaml  # 复制
│   ├── llm/
│   │   └── factory.py              # 新：ChatOpenAI(DashScope) 工厂
│   ├── tools/
│   │   ├── skill_loader.py         # 新：@tool subagents 模式
│   │   ├── intermediate_tool.py    # 新：@tool
│   │   ├── add_image_tool_local.py # 新：@tool
│   │   └── baidu_search_tool.py     # 新：@tool
│   ├── middleware/                 # ★ 替换 hook_framework
│   │   ├── __init__.py
│   │   ├── hook_middleware.py      # 新：AgentMiddleware → HookRegistry（消除 pending_deny）
│   │   ├── memory_middleware.py    # 新：before_model 注入 bootstrap + 恢复/压缩会话
│   │   ├── registry.py             # 复制（EventType/HookContext/HookRegistry/GuardrailDeny）
│   │   └── loader.py               # 复制（HookLoader 加载 hooks.yaml）
│   ├── memory/                     # 复制 bootstrap/config/indexer/token_counter；context_mgmt 轻改
│   ├── feishu/ session/ cron/ cleanup/ api/ observability/ config/ utils/  # 全部复制
├── tests/                          # 复制全部；改写依赖 crewai 的 4 个
└── docs/                           # 复制（设计文档参考）
```

---

## 文件处理清单（精确，基于 grep 核实）

### A. 新建/重写（CrewAI 专属 → LangChain）—— 14 个源文件 + 配置
1. `pyproject.toml` — 依赖换为 `langchain>=1.0`、`langgraph`、`langchain-openai`、`langchain-mcp-adapters`、`langfuse`、`lark-oapi`、`prometheus_client` 等
2. `agents/main_agent.py` — `create_agent` 主 Agent，工具 `[skill_loader, intermediate]`，`response_format=MainTaskOutput`
3. `agents/skill_agent.py` — `create_agent` 子 Agent，`MultiServerMCPClient` 加载沙箱工具，system_prompt 由 SKILL.md 构建
4. `agents/prompts.py` — `build_orchestrator_prompt(workspace_dir)`、`build_skill_prompt(skill_md, session_id, ...)`
5. `llm/factory.py` — `build_llm(model, temperature)` → `ChatOpenAI(base_url=os.getenv("BASE_URL"), api_key=os.getenv("OPENAI_API_KEY"), model=model)`
6. `tools/skill_loader.py` — `@tool` 包裹子 Agent；保留 ContextVar trace 父子传播（`copy_context` + `_reset_langfuse_contextvars`）
7. `tools/intermediate_tool.py`、`add_image_tool_local.py`、`baidu_search_tool.py` — `@tool` 装饰器重写
8. `middleware/hook_middleware.py` — `HookMiddleware(AgentMiddleware)`：`before_model`→BEFORE_LLM dispatch；`wrap_tool_call`→BEFORE_TOOL_CALL dispatch_gate（raise GuardrailDeny）+ AFTER_TOOL_CALL dispatch；`after_model`→AFTER_TURN dispatch_gate（raise）；`after_agent`→TASK_COMPLETE dispatch。**无 pending_deny**
9. `middleware/memory_middleware.py` — `before_model`：注入 bootstrap system message、`load_session_ctx` 恢复、`prune_tool_results`+`maybe_compress` 压缩
10. `main.py` — `build_agent_fn` 改为返回基于 `create_agent` 的闭包；装配 `middleware=[MemoryMiddleware, HookMiddleware, ...]`
11. `runner.py` — `_handle` 中 `CrewObservabilityAdapter` 替换为 `HookMiddleware` 实例注入；BEFORE_TURN/SESSION_END dispatch 保留；pre-flight `agent_execution` 虚拟工具检查保留
12. `memory/context_mgmt.py` — `_summarize_chunk` 内 `crewai.LLM` → `ChatOpenAI`，`.call()` → `.invoke()`（其余复制）
13. `README.md` — 迁移说明、依赖安装、运行、与原项目对照
14. `config.yaml.example` — 复制并标注 LLM 改为 OPENAI_API_KEY/BASE_URL/MODEL

### B. 原样复制（grep 确认无真实 crewai 导入）—— 框架无关
- `hook_framework/registry.py`（仅注释提及 CrewAI）、`hook_framework/loader.py`
- `shared_hooks/*.py` 全部（9 策略 + hooks.yaml）— 它们是 `HookRegistry` 的 handler，与 CrewAI 解耦
- `feishu/`、`session/`、`cron/`、`cleanup/`、`api/`、`observability/`、`config/`、`utils/` 全部 `.py`
- `memory/bootstrap.py`、`memory/config.py`、`memory/indexer.py`、`memory/token_counter.py`
- `models.py`、`agents/config/*.yaml`
- `skills/` 整个目录（SKILL.md + 脚本，框架无关指令）
- `workspace-init/`、`schema.sql`、两个 `*-docker-compose.yaml`

> **关键洞察**：原项目的"加固层"设计（`HookRegistry` + 9 策略 + `hooks.yaml` 声明式接线）本身就是框架无关的——CrewAI 只在 `crew_adapter` 这一层被引用。因此 **shared_hooks/ 可 100% 复制**，只需把 `crew_adapter`（CrewAI 回调翻译层）替换为 LangChain `HookMiddleware`。这最大程度复用了原项目 30-33 课的加固成果。

### C. 测试
- **复制**：`tests/unit/` 中除 `hook_framework/test_crew_adapter.py` 外全部；`tests/unit/shared_hooks/` 全部；`tests/e2e/`、`tests/integration/` 中不依赖 crewai 的
- **改写**：`tests/unit/hook_framework/test_crew_adapter.py` → `test_hook_middleware.py`（验证中间件事件分发 + GuardrailDeny 传播）；`tests/integration/test_deny_observability.py`；`tests/e2e/test_e2e_08_search_memory.py`、`test_e2e_10_langfuse_trace.py`（替换 crewai 导入）

---

## 实现阶段（建议执行顺序）

**Phase 1 — 骨架与依赖**
- 建 `xiaopaw-langchain/` 目录、`pyproject.toml`、`README.md`、复制 `config.yaml.example`/docker-compose/`schema.sql`/`workspace-init/`

**Phase 2 — 复制框架无关模块**
- 复制 `feishu/session/cron/cleanup/api/observability/config/utils`、`memory`（除 context_mgmt）、`models.py`、`shared_hooks/`、`skills/`、`hook_framework/registry.py`+`loader.py`、`agents/config/*.yaml`
- 改 `context_mgmt.py`（`crewai.LLM`→`ChatOpenAI`）

**Phase 3 — LangChain 核心（重点）**
- `llm/factory.py`、`agents/prompts.py`
- `tools/*.py`（4 个 `@tool`，重点是 `skill_loader` 的 subagents 模式 + ContextVar 传播）
- `agents/skill_agent.py`（子 Agent + `MultiServerMCPClient`）
- `agents/main_agent.py`（主 Agent + `build_agent_fn`）

**Phase 4 — Middleware（加固层迁移，重点）**
- `middleware/registry.py`+`loader.py`（复制）
- `middleware/hook_middleware.py`（`AgentMiddleware` → `HookRegistry`，消除 pending_deny）
- `middleware/memory_middleware.py`（`before_model` 注入 bootstrap + 恢复/压缩）
- 装配：`create_agent(..., middleware=[MemoryMiddleware, HookMiddleware])`

**Phase 5 — 接线与入口**
- `main.py`、`runner.py`（adapter→middleware；保留 BEFORE_TURN/SESSION_END/pre-flight）

**Phase 6 — 测试迁移**
- 复制可复用测试；改写 4 个 crewai 相关测试

---

## 关键代码骨架（核心三处，供实现参照）

### 1. LLM 工厂（替换 AliyunLLM）
```python
# xiaopaw_lc/llm/factory.py
from langchain_openai import ChatOpenAI
import os

def build_llm(model: str | None = None, temperature: float = 0.3) -> ChatOpenAI:
    return ChatOpenAI(
        model=model or os.getenv("MODEL", "gpt-5.4"),
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("BASE_URL"),          # DashScope 兼容端点
        temperature=temperature,
        timeout=int(os.getenv("LLM_TIMEOUT", "600")),
        max_retries=int(os.getenv("LLM_RETRY_COUNT", "2")),
    )
```

### 2. skill_loader（Subagents 模式，替换 SkillLoaderTool）
```python
# xiaopaw_lc/tools/skill_loader.py
from langchain.tools import tool
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

@tool
async def skill_loader(skill_name: str, task_context: str = "") -> str:
    """加载并调用 Skill（渐进式披露）。<name> 标签是 skill_name 参数值。"""
    if skill_name == "history_reader":
        return _handle_history_reader(task_context, ...)
    instructions = _get_skill_instructions(skill_name, ...)
    # subagents 模式：子 Agent = create_agent + MCP 沙箱工具
    async with MultiServerMCPClient({"sandbox": {"transport": "http", "url": sandbox_url}}) as client:
        mcp_tools = await client.get_tools()
        sub_agent = create_agent(
            model=build_llm(),
            tools=mcp_tools,
            system_prompt=instructions,
            middleware=[HookMiddleware(registry, session_id), ...],
        )
        result = await sub_agent.ainvoke({"messages": [{"role": "user", "content": task_context}]})
    return result["messages"][-1].content
```

### 3. HookMiddleware（替换 crew_adapter，消除 pending_deny）
```python
# xiaopaw_lc/middleware/hook_middleware.py
from langchain.agents.middleware import AgentMiddleware, ToolRequest, ToolResponse, ModelRequest, ModelResponse

class HookMiddleware(AgentMiddleware):
    """LangChain 中间件 → 未改动的 HookRegistry（5+2 事件）。
    ★ 无 pending_deny：wrap_tool_call 是真包装器，GuardrailDeny 直接传播。"""
    def __init__(self, registry, session_id): self._reg, self._sid = registry, session_id

    def before_model(self, request): self._reg.dispatch(BEFORE_LLM, ctx(...))

    def wrap_tool_call(self, request, handler):
        # BEFORE_TOOL_CALL（gate）—— 直接 raise，无需暂存
        try:
            self._reg.dispatch_gate(BEFORE_TOOL_CALL, ctx(...))
        except GuardrailDeny:
            self._reg.dispatch(AFTER_TOOL_CALL, ctx(success=False, deny=True))  # 关 span
            raise  # ← 直接传播，runner 捕获（无需 pending_deny）
        result = handler(request)
        self._reg.dispatch(AFTER_TOOL_CALL, ctx(...))   # observe
        return result

    def after_model(self, response):
        self._reg.dispatch_gate(AFTER_TURN, ctx(...))   # cost/loop gate，直接 raise

    def after_agent(self, response):
        self._reg.dispatch(TASK_COMPLETE, ctx(...))
```

### 主 Agent 装配
```python
# xiaopaw_lc/agents/main_agent.py
agent = create_agent(
    model=build_llm(),
    tools=[skill_loader, intermediate],
    system_prompt=build_orchestrator_prompt(workspace_dir),  # bootstrap 注入
    response_format=MainTaskOutput,
    middleware=[
        MemoryMiddleware(ctx_dir, session_id, ...),   # before_model 恢复/压缩
        HookMiddleware(registry, session_id),          # 加固层
    ],
)
```

---

## 验证方式

1. **依赖安装**：`cd xiaopaw-langchain && pip install -e ".[full,dev]"`
2. **单元测试**（加固层与中间件）：
   - `pytest tests/unit/shared_hooks/ tests/unit/middleware/ -v`
   - 重点：`test_hook_middleware.py` 验证 BEFORE_TOOL_CALL deny 能直接传播（不再需要 pending_deny 重抛）
3. **本地对话**（开发模式，不需飞书）：
   - `set XIAOPAW_ENV=dev && python -m xiaopaw_lc.main`
   - `curl -X POST http://127.0.0.1:9090/api/test/message -H "Authorization: Bearer <token>" -d '{"routing_key":"p2p:ou_test","text":"你好"}'`
4. **技能 + 沙箱**（需启动 `sandbox-docker-compose.yaml`）：
   - 测试 `skill_loader` subagents 链路：发"帮我搜索 Python 3.13 新特性"→ 子 Agent 经 MCP 沙箱执行 baidu_search
5. **加固链路**：构造恶意 prompt（路径穿越/Shell 注入）→ 验证 `sandbox_guard` 经 `wrap_tool_call` 抛 `GuardrailDeny` → runner 回复"安全策略拦截"
6. **可观测**：`curl http://127.0.0.1:8090/metrics`；启用 Langfuse 验证 trace 树（主 Agent → skill_loader span → 子 Agent 自动挂父节点）
7. **对照验证**：相同输入下，LangChain 版与原 CrewAI 版行为一致（回复格式、技能调用、拦截行为）

## 约束与注意事项
- **不修改原项目任何文件**；新代码全部在 `xiaopaw-langchain/` 内
- 包名用 `xiaopaw_lc` 避免与原 `xiaopaw` 冲突；复制文件时统一改 import 前缀
- `shared_hooks/` 与 `skills/` 复制后路径保持一致（沙箱挂载 `/mnt/skills`、`hooks.yaml` 相对路径不变）
- LangChain v1 API（`create_agent`/`AgentMiddleware`/`ModelRequest.override`/`MultiServerMCPClient`）以官方文档为准，实现中可继续用 langchain MCP 查询
- `GuardrailDeny` 传播语义变化是最大差异点：原项目靠 `pending_deny` 在 step_callback 重抛；新项目靠 `wrap_tool_call`/`after_model` 直接 raise——测试需验证此行为
