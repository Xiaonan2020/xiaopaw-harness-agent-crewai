# 06 - 第一层 Agent — MainCrew 实现

> 本篇是 XiaoPaw 项目"大脑"的核心实现。学完本篇，你会理解一个用户消息从飞书进来后，是怎样被 AI 理解、思考、调用技能、最终回复的完整链路。

---

## 本节学习目标

读完这一篇，你应该能够回答以下问题：

1. CrewAI 框架的 Agent、Task、Crew 三个核心概念分别是什么？它们是怎么协作的？
2. MainCrew（主 Agent）为什么只挂一个 `skill_loader` 工具，而不是把所有工具都塞进去？
3. `before_llm_call` 这个 Hook（钩子）在什么时候触发？它做了哪 4 件事？
4. `_restore_session` 是怎么把"上一次的对话历史"重新塞回 LLM 的消息列表里的？
5. `pending_deny` 机制解决的是什么问题？为什么 CrewAI 会"吞掉"异常？
6. 为什么用 `build_agent_fn` 工厂函数而不是直接 `new` 一个 Agent？

如果你对 Python 的 `async/await`、`ContextVar`、装饰器还不熟，建议先回看 03 篇《配置系统实现》和 05 篇《消息队列与 Runner 调度》。

---

## 一、CrewAI 框架基础

### 1.1 用"开公司"类比理解 CrewAI

CrewAI 这个名字来自 "Crew"（船员队伍）。它把构建 AI 应用的过程类比成组建一支船员队伍：

| CrewAI 概念 | 现实类比 | 在 XiaoPaw 中的对应 |
|------------|---------|-------------------|
| **Agent**（智能体） | 一个有职位的员工 | "工作助手"，负责理解用户、选技能 |
| **Task**（任务） | 一张任务单 | "处理用户消息：xxx" |
| **Crew**（船员队伍） | 把人和任务单组装起来 | `Crew(agents=[...], tasks=[...])` |
| **Tool**（工具） | 员工手里的工具箱 | `skill_loader`（技能加载器） |
| **Process**（流程） | 任务怎么分配 | `Process.sequential`（顺序执行） |

一句话总结：**Agent 是"谁"，Task 是"做什么"，Crew 是"怎么组织"，Tool 是"用什么做"**。

### 1.2 三个核心概念的代码骨架

下面这段代码是 CrewAI 最小可运行示例，先不用理解每个参数，跟着注释看整体结构：

```python
# 1. Agent：一个有角色、目标、工具的 AI 代理（"员工"）
orchestrator = Agent(
    role="工作助手",                    # 这个员工的职位名
    goal="理解用户需求并调用合适的技能",  # 他的工作目标
    backstory="你是小趴，一个友善的 AI 助手...",  # 他的背景介绍（影响说话风格）
    tools=[skill_loader],                # 他能用的工具
    llm=AliyunLLM(model="qwen3-max"),    # 他的"大脑"用哪个模型
)

# 2. Task：Agent 要完成的任务（"任务单"）
task = Task(
    description="处理用户消息：{user_message}",  # 任务内容（{user_message} 是占位符）
    expected_output="给用户的回复",               # 期望输出是什么样
    agent=orchestrator,                          # 指派给哪个 Agent
)

# 3. Crew：把 Agent 和 Task 组装成可执行的编排（"组建船员队伍"）
crew = Crew(
    agents=[orchestrator],          # 队伍里有谁
    tasks=[task],                   # 要完成哪些任务
    process=Process.sequential,     # 顺序执行：任务一个个来
)

# 4. 执行（"开工"）
# inputs 里的 user_message 会替换 Task description 里的 {user_message}
result = await crew.akickoff(inputs={"user_message": "你好"})
```

### 1.3 Agent 的"思考-行动"循环

Agent 不是一次性回答问题的，它会反复"思考→行动→观察→再思考"，直到认为可以给最终答案。这叫 **ReAct 循环**（Reasoning + Acting）。

```
用户说："帮我搜索 Python 新特性"

【第 1 轮】
  Thought（思考）: 用户需要搜索信息，我应该调用 baidu_search 技能
  Action（行动）: skill_loader(skill_name="baidu_search", task_context="搜索 Python 新特性")

  ↓ CrewAI 执行这个工具调用，把结果返回给 Agent

  Observation（观察）: 搜索结果包含 Python 3.12 引入了类型参数语法...

【第 2 轮】
  Thought: 我已经拿到搜索结果，整理后回复用户
  Final Answer: Python 3.12 的新特性包括：1. 类型参数语法...

  ↓ Agent 认为任务完成，输出最终答案
```

**关键概念**：
- 每一轮"思考+行动"叫一个 **step**（步骤）
- `max_iter` 参数限制最多走多少步（orchestrator 是 50 步，防止无限循环烧钱）
- 每个 step 结束后，CrewAI 会调用 `step_callback`（这就是我们后面要重点讲的钩子）

---

## 二、Agent 配置文件

XiaoPaw 用 YAML 文件管理 Agent 和 Task 的配置，这样修改提示词不用改代码。

### 2.1 agents.yaml —— Agent 角色定义

```yaml
# 文件路径：xiaopaw/agents/config/agents.yaml
# 这里定义了两个 Agent 的"人设"

orchestrator:                          # 主 Agent（编排者）
  role: "XiaoPaw 工作助手"             # 职位名
  goal: >                              # 工作目标（> 表示多行字符串）
    理解用户意图，精准选择并调用合适的 Skill 完成任务。
    对于简单问答直接回复，对于复杂任务通过 skill_loader 调用对应的 Skill。
  backstory: >                         # 背景故事（运行时会被覆盖，见 3.4 节）
    你是 XiaoPaw（小爪子），一个飞书本地 AI 工作助手。
    工作流程：
    1. 理解用户的真实意图
    2. 如果需要外部能力，先加载 reference 类型的 Skill 获取指导
    3. 根据指导规划子任务
    4. 通过 skill_loader 调用 task 类型的 Skill 执行
    5. 根据执行结果调整策略
    6. 综合所有结果，生成对用户友好的最终回复

    工具约束（严格遵守）：
    - 你只有两个工具：skill_loader 和 IntermediateTool
    - skill_loader description 中的 <name> 标签是 skill_name 参数值，不是工具名称
    - 直接以 baidu_search 等名称调用工具会报"Tool not found"
    - 必须通过 skill_loader(skill_name="baidu_search") 调用
  max_iter: 50                         # 单轮最多思考 50 步（防止烧钱）

skill_agent:                           # 子 Agent（执行技能时用，见第 08 篇）
  role: "{skill_name_upper} 执行专家"   # {skill_name_upper} 运行时替换，如 "BAIDU_SEARCH 执行专家"
  goal: >
    在 AIO-Sandbox 沙箱环境中精确执行 {skill_name} 任务。
    严格遵循 Skill 指令，通过 MCP 工具操作文件和代码。
  backstory: >
    你是 {skill_name} 的专业执行者，在 Docker 沙箱中工作。
    工作目录：{session_dir}

    Skill 指令：
    {skill_instructions}
  max_iter: 20                         # Sub-Crew 最多 20 步（更严格，防失控）
```

**为什么要用 YAML 而不是写死在代码里？**

- 改提示词不用重新部署代码（运营同学也能改）
- 不同环境（测试/生产）可以用不同配置文件
- 集中管理，一目了然

### 2.2 tasks.yaml —— Task 描述

```yaml
# 文件路径：xiaopaw/agents/config/tasks.yaml

main_task:                             # 主任务
  description: >
    当前用户消息：
    {user_message}                     # 运行时替换为实际用户消息

    请理解用户意图并完成任务。
    - 简单问答直接回复
    - 需要外部能力时使用 skill_loader 调用 Skill
    - 需要查看历史对话时使用 skill_loader 调用 history_reader
  expected_output: >                   # 期望输出格式
    JSON 格式，包含以下字段：
    - reply: 发送给用户的回复内容（中文，友好自然）
    - used_skills: 本次调用的 Skill 名称列表（可为空列表）

skill_task:                            # 子任务（Sub-Crew 用，见第 08 篇）
  description: >
    执行以下任务：
    {{task_context}}                   # 双花括号转义，CrewAI 不会替换

    工作目录：{session_dir}
    请严格按照 Skill 指令执行，将结果文件保存到 {session_dir}/outputs/ 目录。
  expected_output: >
    JSON 格式，包含：
    - errcode: 0 表示成功，非 0 表示失败
    - message: 结果描述
    - data: 结构化数据（可选）
    - files: 生成的文件路径列表（可选）
```

**注意 `{{task_context}}` 的双花括号**：CrewAI 会把 `{xxx}` 当作模板变量替换。如果想保留字面的花括号，要写 `{{}}` 转义。

### 2.3 输出模型 MainTaskOutput

Task 的 `expected_output` 只是文字描述，真正约束输出格式的是 `output_pydantic`：

```python
# 文件路径：xiaopaw/agents/models.py
from pydantic import BaseModel, Field

class MainTaskOutput(BaseModel):
    """主任务的输出结构。

    CrewAI 会要求 LLM 输出符合这个结构的 JSON，
    然后自动解析成 Python 对象。
    """
    reply: str = Field(..., description="发送给飞书用户的回复内容")
    # ... 表示必填；reply 是给用户的回复文本
    used_skills: list[str] = Field(
        default_factory=list,
        description="本次调用的 Skill 名称列表"
    )
    # default_factory=list 表示默认空列表（可选字段）
```

**为什么用 Pydantic 而不是手动解析 JSON？**
- 自动校验类型（LLM 偶尔会输出不合规 JSON）
- 类型提示友好（IDE 自动补全）
- 出错时报错信息清晰

---

## 三、MainCrew 完整实现

### 3.1 文件结构与辅助函数

```python
# 文件路径：xiaopaw/agents/main_crew.py
# 本文件是整个系统的"大脑"，约 350 行代码

from __future__ import annotations    # 允许在类型注解里写未定义的类型

import asyncio                        # 异步 IO（并发执行）
import logging                        # 日志
import time                           # 时间戳
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path              # 跨平台路径处理
from typing import Any                # 任意类型

import yaml                           # 解析 YAML 配置
from crewai import Agent, Crew, Process, Task
from crewai.agents.parser import AgentAction, AgentFinish
from crewai.hooks import (
    LLMCallHookContext,               # LLM 调用前的上下文对象
    ToolCallHookContext,              # 工具调用前的上下文对象
    before_llm_call,                  # 装饰器：注册 LLM 调用前 Hook
    before_tool_call,                 # 装饰器：注册工具调用前 Hook
    unregister_before_tool_call_hook,  # 注销工具 Hook（防泄漏）
)
from crewai.project import CrewBase, agent, crew, task
# CrewBase：让类变成 CrewAI 项目类，自动加载配置
# @agent / @task / @crew：装饰器，标记对应方法

from xiaopaw.hook_framework.crew_adapter import get_current_adapter
# get_current_adapter()：从 ContextVar 取当前会话的 Hook 适配器

from xiaopaw.agents.models import MainTaskOutput
from xiaopaw.config.flags import FeatureFlags
from xiaopaw.llm.aliyun_llm import AliyunLLM
from xiaopaw.memory.bootstrap import build_bootstrap_prompt
from xiaopaw.memory.context_mgmt import (
    append_session_raw,    # 追加原始消息到会话文件
    load_session_ctx,       # 加载会话历史
    maybe_compress,         # 超长时压缩上下文
    prune_tool_results,     # 剪枝过长的工具结果
    save_session_ctx,       # 保存会话上下文
)
from xiaopaw.memory.indexer import async_index_turn      # 向量索引
from xiaopaw.models import SenderProtocol                # 发消息接口
from xiaopaw.session.models import MessageEntry           # 消息数据结构
from xiaopaw.tools.intermediate_tool import IntermediateTool

logger = logging.getLogger(__name__)    # 本模块的 logger

# 配置文件目录：main_crew.py 同级的 config/
_CONFIG_DIR = Path(__file__).parent / "config"
# 默认保留最近 20 轮历史
_DEFAULT_MAX_HISTORY_TURNS = 20

# Agent 函数类型签名（供 build_agent_fn 用）
AgentFn = Callable[
    [str, list[MessageEntry], str, str, bool],  # 参数：消息、历史、session_id、routing_key、verbose
    Awaitable[str],                              # 返回：异步字符串（回复）
]

# MCP 沙箱工具名前缀（这些工具的参数归一化逻辑不一样）
_MCP_TOOL_PREFIXES = ("sandbox_", "mcp_")
```

#### 辅助函数 `_is_mcp_sandbox_tool`

```python
def _is_mcp_sandbox_tool(tool_name: str) -> bool:
    """判断一个工具名是不是 MCP 沙箱工具。

    为什么要区分？
    沙箱工具（如 sandbox_file_operations）的参数里出现 shell 特殊字符是合法的
    （比如 git 命令组合 "git pull && git push"），所以需要豁免某些安全检查。

    参数：
        tool_name: 工具名，如 "sandbox_execute_command"

    返回：
        bool: True 表示是沙箱工具

    使用示例：
        >>> _is_mcp_sandbox_tool("sandbox_execute_command")
        True
        >>> _is_mcp_sandbox_tool("skill_loader")
        False
    """
    # any()：只要有一个前缀匹配就返回 True
    return any(tool_name.startswith(p) for p in _MCP_TOOL_PREFIXES)
```

#### 辅助函数 `_normalize_tool_input`

```python
# 这三个集合定义了需要归一化的 Python 字面量字符串
_PY_NONE_STRINGS = {"None"}      # LLM 有时把 None 当字符串 "None" 传
_PY_TRUE_STRINGS = {"True"}      # 同理 True → "True"
_PY_FALSE_STRINGS = {"False"}    # 同理 False → "False"


def _normalize_tool_input(tool_input: dict) -> None:
    """MCP 沙箱工具参数归一化（原地修改）。

    为什么要归一化？
    LLM（大模型）有时会把 Python 的 None/True/False 当成字符串传给工具，
    比如 {"timeout": "None"} 而不是 {"timeout": null}。
    这会导致 Pydantic 校验失败，工具反复重试，烧时间烧钱。

    本函数遍历所有参数，把：
    - 字符串 "None" → 删除该参数（等同 Python 的 None）
    - 字符串 "True" → 布尔值 True
    - 字符串 "False" → 布尔值 False

    参数：
        tool_input: 工具参数字典（原地修改，无返回值）

    注意事项：
        - 只处理 str 类型，不处理其他类型
        - 用 list(tool_input.keys()) 避免遍历时修改字典报错
    """
    # 遍历所有 key（先转 list，因为后面可能 del）
    for key in list(tool_input.keys()):
        val = tool_input[key]
        # 只处理字符串类型
        if not isinstance(val, str):
            continue
        # 字符串 "None" → 删除该参数
        if val in _PY_NONE_STRINGS:
            del tool_input[key]
        # 字符串 "True" → 布尔 True
        elif val in _PY_TRUE_STRINGS:
            tool_input[key] = True
        # 字符串 "False" → 布尔 False
        elif val in _PY_FALSE_STRINGS:
            tool_input[key] = False
```

### 3.2 step_callback —— 每个"思考步"后的回调

`step_callback` 是 CrewAI 提供的钩子：Agent 每完成一个"思考-行动"步骤，CrewAI 就会调用它一次。

```python
def _make_step_callback(
    sender: SenderProtocol, routing_key: str
) -> Callable[[Any], Awaitable[None]]:
    """生成 CrewAI step_callback 工厂函数。

    什么时候触发？
    Agent 每完成一个 step（一次"思考+行动"）后触发。
    例如：Agent 思考了 5 轮才给出最终答案，这个回调会被调用 5 次。

    两个核心职责：
    1. 触发 AFTER_TURN 事件（loop_detector 用它判循环，cost_guard 用它算账）
    2. 重抛 pending_deny（让安全拦截真正生效，详见后面的"pending_deny 机制"）

    参数：
        sender: 发消息的接口（用于给用户推送"思考中"卡片）
        routing_key: 路由键（标识是哪个会话）

    返回：
        一个 async 回调函数，CrewAI 会 await 它

    使用示例：
        callback = _make_step_callback(sender, "session-xxx")
        crew = Crew(..., step_callback=callback)
    """

    async def _callback(step_output: Any) -> None:
        """真正的回调函数。

        参数：
            step_output: 本步的输出，可能是 AgentAction 或 AgentFinish
        """
        # 注意：不在这里发"思考中"卡片
        # 因为 Runner 在调用 agent_fn 之前已经发过卡片了
        # 这里再发会重复

        # 从 ContextVar 取当前会话的 adapter（Hook 适配器）
        adapter = get_current_adapter()
        # 如果没有 adapter（比如没启用 Hook 框架），直接返回
        if not adapter:
            return

        # ── 提取本 step 的输出文本 ──
        step_text = ""
        if isinstance(step_output, AgentAction):
            # AgentAction：Agent 还在行动中（调了工具但没给最终答案）
            # text 是行动描述，thought 是思考内容
            step_text = str(step_output.text or step_output.thought or "")
        elif isinstance(step_output, AgentFinish):
            # AgentFinish：Agent 完成了（给出了最终答案）
            # output 是最终答案文本
            step_text = str(getattr(step_output, "output", "") or "")

        # 触发 AFTER_TURN 事件
        # 截断到 2000 字（防止超长文本撑爆 Hook 系统）
        adapter.dispatch_after_turn(output=step_text[:2000])

        # ── ★ pending_deny 重抛口 ──
        # 这是整个安全机制的关键出口，详见 3.6 节解释
        if adapter._pending_deny:
            pending = adapter._pending_deny
            adapter._pending_deny = None    # 清空，防止重复抛
            raise pending                    # 抛出，让 CrewAI 终止执行

    return _callback
```

### 3.3 MemoryAwareCrew 类结构

```python
@CrewBase   # CrewAI 装饰器：把这个类标记为 CrewAI 项目类
class MemoryAwareCrew:
    """主 Agent —— 集成三层记忆和 Hook 框架。

    这个类做了什么？
    - 加载 YAML 配置定义 Agent/Task
    - 从工作区文件构建 backstory（记忆注入）
    - 挂载 skill_loader 工具
    - 注册 before_llm_call / before_tool_call Hook
    - 执行 Crew 并保存结果

    使用 @CrewBase 装饰器的好处：
    - 自动加载 agents_config / tasks_config 指定的 YAML
    - @agent / @task / @crew 装饰器自动注册
    - 支持 crewai cli 命令行工具
    """

    # 配置文件路径（CrewBase 会自动加载）
    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(
        self,
        session_id: str,                # 会话 ID（唯一标识一次对话）
        routing_key: str,               # 路由键（飞书群/私聊标识）
        user_message: str,              # 用户发来的消息
        sender: SenderProtocol,         # 发消息接口（回复用户用）
        workspace_dir: Path,            # 工作区目录（记忆文件在这里）
        ctx_dir: Path,                  # 上下文目录（会话历史存这里）
        history_all: list[MessageEntry],# 完整历史消息列表
        db_dsn: str = "",               # 向量数据库连接串（可选）
        max_history_turns: int = _DEFAULT_MAX_HISTORY_TURNS,  # 最多保留几轮历史
        sandbox_url: str = "",          # 沙箱 MCP 地址
        flags: FeatureFlags | None = None,  # 功能开关
        verbose: bool = False,          # 是否打印详细日志
    ) -> None:
        # 把参数存到 self 上，供后续方法用
        self.session_id = session_id
        self.routing_key = routing_key
        self.user_message = user_message
        self._sender = sender
        self._workspace_dir = workspace_dir
        self._ctx_dir = ctx_dir
        self._db_dsn = db_dsn              # 向量数据库连接串
        self._history_all = history_all    # 完整历史（history_reader 用）
        self._max_history_turns = max_history_turns
        self._sandbox_url = sandbox_url
        self._flags = flags or FeatureFlags()
        self._verbose = verbose

        # 每次创建新实例时生成 step_callback（闭包，捕获 sender/routing_key）
        self._step_callback = _make_step_callback(sender, routing_key)
        self._prune_keep_turns = 10        # 保留最近 10 轮工具结果不剪枝
        self._session_loaded = False       # 标记：是否已经恢复过会话历史
        self._last_msgs: list[dict] = []   # 记录最后发送给 LLM 的消息列表
        self._history_len = 0              # 历史消息长度（保存时用，跳过已存部分）
        self._turn_start_ts = int(time.time() * 1000)  # 本轮开始时间戳（毫秒）

        self._index_coroutine: Coroutine | None = None  # 异步索引协程引用
```

### 3.4 Agent 定义 —— `@agent` 装饰器

```python
    @agent   # CrewAI 装饰器：标记这是 Agent 定义方法
    def orchestrator(self) -> Agent:
        """定义主 Agent（编排者）。

        关键设计点：
        1. backstory 从 workspace 的记忆文件动态构建（不是写死在 YAML 里）
        2. 只挂载 skill_loader + IntermediateTool 两个工具（渐进式披露，见第 07 篇）
        3. 使用 AliyunLLM 接入 Qwen3-max 模型

        返回：
            CrewAI Agent 实例
        """
        # 1. 加载 Agent 配置（agents.yaml 里的 orchestrator 段）
        agents_cfg = yaml.safe_load(
            (_CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8")
        )
        cfg = agents_cfg["orchestrator"]

        # 2. ★ 从记忆文件构建 backstory
        # build_bootstrap_prompt 会读 workspace_dir 下的：
        #   soul.md（灵魂设定）+ user.md（用户画像）+ agent.md（助手设定）+ memory.md（长期记忆）
        # 拼成一段 XML 风格的文本，作为 Agent 的 backstory
        # 这样每次对话开始时，Agent 都能看到最新的用户记忆
        cfg["backstory"] = build_bootstrap_prompt(self._workspace_dir)

        # 3. 创建 SkillLoaderTool（技能加载器，见第 07 篇）
        # 延迟导入避免循环依赖
        from xiaopaw.tools.skill_loader import SkillLoaderTool
        skill_tool = SkillLoaderTool(
            session_id=self.session_id,
            sandbox_url=self._sandbox_url,
            routing_key=self.routing_key,
            history_all=self._history_all,
        )

        # 4. 创建并返回 Agent
        return Agent(
            **cfg,                                    # 展开 role/goal/backstory/max_iter
            tools=[skill_tool, IntermediateTool()],   # 只暴露这两个工具
            llm=AliyunLLM(
                model="qwen3-max",
                region="cn",          # 国内节点
                temperature=0.3,      # 低温度 = 输出更稳定
            ),
            verbose=self._verbose,
        )
```

**为什么只挂 `skill_loader` 一个工具，而不直接挂 `baidu_search`、`web_browse` 等所有工具？**

这是"渐进式能力披露"的核心设计，详见第 07 篇。简单说：
- 如果挂 13 个工具，LLM 的 prompt 会被工具 schema 撑爆（每个工具的参数定义都要塞进 prompt）
- 用户问"你好"时，LLM 还是要看完 13 个工具的描述才能决定"不需要调工具"
- 改成只挂 `skill_loader`，LLM 看到的就是一份"技能菜单"，需要时再调 `skill_loader` 去加载具体技能

### 3.5 Task 定义

```python
    @task    # CrewAI 装饰器：标记这是 Task 定义方法
    def main_task(self) -> Task:
        """定义主任务。

        返回：
            CrewAI Task 实例
        """
        # 加载 tasks.yaml
        tasks_cfg = yaml.safe_load(
            (_CONFIG_DIR / "tasks.yaml").read_text(encoding="utf-8")
        )
        return Task(
            **tasks_cfg["main_task"],       # 展开 description/expected_output
            agent=self.orchestrator(),      # 指定执行者（注意：每次调用都创建新 Agent）
            output_pydantic=MainTaskOutput, # 要求输出符合这个 Pydantic 模型
        )
```

### 3.6 Crew 定义与 Hook 集成

```python
    @crew    # CrewAI 装饰器：标记这是 Crew 定义方法（入口）
    def crew(self) -> Crew:
        """组装 Crew —— 这里是 L33 接线点。

        L33 是课程编号，"接线点"指把 Hook 框架接入 CrewAI 的具体位置。
        本方法把 adapter 的两个回调装进 CrewAI：
        - step_callback：每个推理 step 触发 → AFTER_TURN + pending_deny 重抛
        - task_callback：Task 完成时触发 → TASK_COMPLETE + pending_deny 重抛（最后一道防线）

        返回：
            CrewAI Crew 实例
        """
        # 从 ContextVar 取当前 adapter
        adapter = get_current_adapter()

        return Crew(
            agents=self.agents,           # CrewBase 自动收集所有 @agent 方法
            tasks=self.tasks,             # CrewBase 自动收集所有 @task 方法
            process=Process.sequential,   # 顺序执行（任务一个个来）
            verbose=self._verbose,
            step_callback=self._step_callback,   # 每步回调（见 3.2 节）
            task_callback=adapter.make_task_callback() if adapter else None,
            # task_callback：Task 完成时触发，是 pending_deny 的"最后一道防线"
        )
```

### 3.7 before_llm_call Hook —— LLM 调用前的核心钩子

这是整个系统最重要的 Hook 之一。**每次 LLM 要被调用前**，CrewAI 都会先调用这个方法，让我们有机会修改要发给 LLM 的消息列表。

```python
    @before_llm_call   # CrewAI 装饰器：注册 LLM 调用前 Hook
    def before_llm_hook(self, context: LLMCallHookContext) -> bool | None:
        """LLM 调用前的 Hook —— 注入记忆 + 压缩上下文。

        什么时候触发？
        每次 CrewAI 要调用 LLM 之前（每个 step 都会调一次 LLM）。
        一个对话里可能触发很多次（思考 5 轮就触发 5 次）。

        4 个核心职责（按顺序执行）：
        1. 首次调用时恢复会话历史（把上次的对话塞回 messages）
        2. 剪枝过长的工具结果（保留最近 10 轮，更早的截断）
        3. 压缩上下文（超 token 限制时摘要）
        4. 触发 BEFORE_LLM Hook（让 cost_guard / langfuse_trace 知道）

        参数：
            context: LLMCallHookContext，包含 messages（要发给 LLM 的消息列表）和 llm

        返回：
            None：表示"继续执行"（不拦截）
            True：表示"跳过本次 LLM 调用"（罕见用法）
        """
        # ── 步骤 1：首次调用时恢复会话历史 ──
        # _session_loaded 标记位：保证只恢复一次，后续调用不重复恢复
        if not self._session_loaded:
            self._restore_session(context)
            self._session_loaded = True

        # 记录当前消息（后续保存用）
        self._last_msgs = context.messages
        len_before = len(context.messages)

        # ── 步骤 2：剪枝工具结果 ──
        # 工具结果（role=tool 的消息）可能很长（比如搜索结果几千字）
        # 保留最近 10 轮不剪枝，更早的会被截断或删除
        prune_tool_results(context.messages, keep_turns=self._prune_keep_turns)

        # ── 步骤 3：压缩上下文 ──
        # 如果消息总长度超过模型的 token 限制（如 32K），
        # 用 LLM 把旧消息摘要成一段，节省 token
        maybe_compress(
            context.messages,
            model_limit=self._flags.context_window_tokens
            if hasattr(self._flags, "context_window_tokens")
            else 32000,    # 兜底：32K
        )

        # 检查压缩后是否变短了（如果剪枝/压缩删了消息）
        len_after = len(context.messages)
        if len_after < len_before:
            # 更新历史长度计数（保存时跳过已存部分）
            self._history_len = max(0, self._history_len - (len_before - len_after))

        # ── 步骤 4：触发 BEFORE_LLM Hook ──
        adapter = get_current_adapter()
        if adapter:
            # 提取模型名（用于 langfuse 记录用的是什么模型）
            llm_model = ""
            if context.llm:
                # getattr 安全取属性：没有 model 属性就返回 ""
                llm_model = getattr(context.llm, "model", "") or ""
                # 有些 LLM 的 model 字段是 "provider/qwen3-max" 格式，只取最后一段
                if isinstance(llm_model, str) and "/" in llm_model:
                    llm_model = llm_model.rsplit("/", 1)[-1]
            # 触发 BEFORE_LLM 事件
            adapter.on_before_llm(
                agent_role="orchestrator",
                messages=context.messages,
                model=llm_model,
            )

        # 返回 None：表示"继续调用 LLM"（不拦截）
        return None
```

#### before_llm_call 执行流程图

```
用户发消息 "帮我搜索 Python 新特性"
         │
         ▼
   CrewAI 决定调用 LLM
         │
         ▼
   ┌─────────────────────────────────────┐
   │  before_llm_hook 被调用              │
   │                                     │
   │  ① 首次？是 → _restore_session      │
   │     把上次对话历史塞回 messages       │
   │                                     │
   │  ② prune_tool_results              │
   │     把 10 轮前的工具结果截断          │
   │                                     │
   │  ③ maybe_compress                  │
   │     超 token 限制？是 → 摘要旧消息    │
   │                                     │
   │  ④ adapter.on_before_llm           │
   │     触发 cost_guard / langfuse       │
   └─────────────────────────────────────┘
         │
         ▼
   实际调用 LLM（Qwen3-max）
         │
         ▼
   LLM 返回："我应该调用 skill_loader"
```

### 3.8 会话恢复 `_restore_session`

**问题背景**：每条用户消息都会创建一个新的 `MemoryAwareCrew` 实例（无状态设计，见 3.10 节）。但对话是有上下文的——用户说"再来一个"时，Agent 必须知道"再来一个"指的是什么。所以需要从磁盘恢复上次的对话历史。

```python
    def _restore_session(self, context: LLMCallHookContext) -> None:
        """从持久化存储恢复会话历史。

        流程：
        1. 从 ctx_dir 读取历史消息（之前 save_session_ctx 存的）
        2. 分离当前消息（system + 最新 user）
        3. 把历史消息插入中间，组成新的 messages

        参数：
            context: LLMCallHookContext，context.messages 是当前要发给 LLM 的消息

        恢复前 vs 恢复后对比：

        【恢复前】context.messages（CrewAI 初始构造的）：
        [
          {"role": "system", "content": "你是 XiaoPaw..."},   # 系统提示
          {"role": "user", "content": "帮我搜索 Python 新特性"} # 当前用户消息
        ]

        【恢复后】context.messages（插入了历史）：
        [
          {"role": "system", "content": "你是 XiaoPaw..."},   # 系统提示（保留）
          {"role": "user", "content": "你好"},                 # ↓ 历史对话
          {"role": "assistant", "content": "你好！有什么..."},
          {"role": "user", "content": "Python 怎么样"},       
          {"role": "assistant", "content": "Python 是..."},
          {"role": "user", "content": "帮我搜索 Python 新特性"} # 当前消息（放最后）
        ]
        """
        # 1. 从磁盘读取历史消息
        history = load_session_ctx(self.session_id, ctx_dir=self._ctx_dir)
        if not history:
            return    # 新会话，没有历史，直接返回

        # 2. 分离当前消息
        # 当前 system 消息（CrewAI 构造的，包含 role/goal/backstory）
        current_system_msgs = [m for m in context.messages if m.get("role") == "system"]
        # 当前最新的 user 消息（用户这次发的）
        current_user_msg = None
        for m in reversed(context.messages):    # 从后往前找
            if m.get("role") == "user":
                current_user_msg = m
                break

        # 3. 准备历史对话（去掉历史里的 system 消息，但保留 context_summary）
        # context_summary 是压缩时生成的摘要，需要保留
        hist_conv = [
            m for m in history
            if m.get("role") != "system"
            or "<context_summary>" in str(m.get("content", ""))
        ]

        # 4. 记录历史长度（保存时用，跳过这部分不重复存）
        self._history_len = len(current_system_msgs) + len(hist_conv)

        # 5. 重组消息列表：system + history + current_user
        context.messages.clear()                       # 清空当前列表
        context.messages.extend(current_system_msgs)   # 1. 系统消息放最前
        context.messages.extend(hist_conv)            # 2. 历史对话放中间
        if current_user_msg:
            context.messages.append(current_user_msg)  # 3. 当前消息放最后
```

### 3.9 执行与索引 `run_and_index`

```python
    async def run_and_index(self) -> str:
        """执行 Crew 并保存结果。

        这是整个 MainCrew 的入口方法，Runner 调用它来处理一条用户消息。

        流程：
        1. 执行 Crew（kickoff）
        2. 保存对话到磁盘（下次能恢复）
        3. 提取回复文本
        4. 异步索引到向量数据库（供 search_memory 用）

        返回：
            str: 给用户的回复文本

        异常处理：
            finally 块保证工具 Hook 一定被注销（防止内存泄漏）
        """
        try:
            # ── 1. 执行 Crew ──
            # akickoff 是异步版本（kickoff 是同步）
            # inputs 里的 user_message 会替换 Task description 里的 {user_message}
            result = await self.crew().akickoff(
                inputs={"user_message": self.user_message}
            )

            # ── 2. 保存对话到磁盘 ──
            # _last_msgs 是 before_llm_hook 最后一次记录的消息列表
            # _history_len 是历史长度，跳过已存部分，只存新增
            new_msgs = self._last_msgs[self._history_len:] if self._last_msgs else []
            append_session_raw(self.session_id, new_msgs, self._ctx_dir)
            save_session_ctx(self.session_id, list(self._last_msgs), self._ctx_dir)

            # ── 3. 提取回复文本 ──
            # result.pydantic 是 MainTaskOutput 实例（结构化输出）
            # result.raw 是原始文本（兜底）
            try:
                reply = result.pydantic.reply if result.pydantic else result.raw
            except Exception:
                # Pydantic 解析失败时兜底
                reply = str(result.raw) if result.raw else str(result)

            # ── 4. 异步索引到向量数据库 ──
            # 如果配置了 db_dsn（PostgreSQL + pgvector 连接串）
            # 把这轮对话存进向量库，供 search_memory 技能检索
            if self._db_dsn:
                self._index_coroutine = async_index_turn(
                    session_id=self.session_id,
                    routing_key=self.routing_key,
                    user_message=self.user_message,
                    assistant_reply=reply,
                    turn_ts=self._turn_start_ts,    # 本轮开始时间戳
                    db_dsn=self._db_dsn,
                )

            return reply
        finally:
            # ── 注销工具 Hook（防止泄漏）──
            # 如果不注销，下次会话还会触发这次注册的 Hook，导致混乱
            try:
                unregister_before_tool_call_hook(self.before_tool_hook)
            except (ValueError, AttributeError):
                # Hook 已经被注销过 / 不存在，忽略
                pass
```

### 3.10 build_agent_fn 工厂函数

```python
def build_agent_fn(
    sender: SenderProtocol,
    workspace_dir: Path,
    ctx_dir: Path,
    db_dsn: str = "",
    max_history_turns: int = _DEFAULT_MAX_HISTORY_TURNS,
    sandbox_url: str = "",
    flags: FeatureFlags | None = None,
) -> AgentFn:
    """创建 Agent 执行函数（工厂模式）。

    为什么要用工厂函数？
    返回一个闭包（closure），Runner 调用它来处理每条消息。
    每次调用都会创建新的 MemoryAwareCrew 实例（无状态设计）。

    无状态设计的好处：
    - 天然支持并发（不同会话互不干扰）
    - 不会有"上次的状态污染这次"问题
    - 出问题好排查（每次都是全新实例）

    参数：
        sender: 发消息接口
        workspace_dir: 工作区目录（记忆文件）
        ctx_dir: 上下文目录（会话历史）
        db_dsn: 向量数据库连接串（可选）
        max_history_turns: 最多保留几轮历史
        sandbox_url: 沙箱 MCP 地址
        flags: 功能开关

    返回：
        agent_fn: 一个异步函数，签名见 AgentFn 类型

    使用示例：
        agent_fn = build_agent_fn(sender, workspace, ctx)
        reply = await agent_fn("你好", [], "session-1", "user-1")
    """
    # 确保上下文目录存在
    ctx_dir.mkdir(parents=True, exist_ok=True)

    # 这是真正被 Runner 调用的函数
    async def agent_fn(
        user_message: str,                  # 用户消息
        history: list[MessageEntry],         # 历史消息
        session_id: str,                     # 会话 ID
        routing_key: str = "",               # 路由键
        verbose: bool = False,               # 是否详细日志
    ) -> str:
        # 每条消息创建新实例（无状态）
        crew_instance = MemoryAwareCrew(
            session_id=session_id,
            routing_key=routing_key,
            user_message=user_message,
            sender=sender,
            workspace_dir=workspace_dir,
            ctx_dir=ctx_dir,
            history_all=history,
            db_dsn=db_dsn,
            max_history_turns=max_history_turns,
            sandbox_url=sandbox_url,
            flags=flags,
            verbose=verbose,
        )
        # 执行并返回回复
        return await crew_instance.run_and_index()

    return agent_fn
```

---

## 四、LLM 接入 —— AliyunLLM

### 4.1 为什么需要自定义 LLM 类？

CrewAI 默认支持 OpenAI、Anthropic 等，但阿里云 DashScope（通义千问）的 API 格式略有不同。所以需要继承 `BaseLLM` 自己实现。

```python
# 文件路径：xiaopaw/llm/aliyun_llm.py（简化版，完整版见源码）
import os
from crewai import BaseLLM

class AliyunLLM(BaseLLM):
    """阿里云 Qwen LLM 封装。

    为什么继承 BaseLLM 而不是 LLM？
    BaseLLM 是 CrewAI 的抽象基类，要求实现 call() 和 acall()。
    我们自己实现可以完全控制请求格式。

    DashScope 兼容 OpenAI API 协议（/v1/chat/completions），
    所以底层用 requests 直接发 HTTP 请求。
    """

    def __init__(
        self,
        model: str = "qwen3-max",       # 默认用 qwen3-max
        region: str = "cn",             # 区域：cn/intl/finance
        temperature: float = 0.3,       # 温度：越低越稳定
        timeout: int = 600,             # 超时 10 分钟
        retry_count: int = 2,           # 重试次数
    ):
        super().__init__(model=model, temperature=temperature)
        # 从环境变量取 API Key（不要硬编码！）
        self.api_key = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
        self.region = region
        # 根据区域选端点
        self.endpoint = ENDPOINTS.get(region, ENDPOINTS["cn"])
        self.timeout = timeout
        self.retry_count = retry_count

    def call(self, messages, tools=None, callbacks=None, **kwargs) -> str:
        """同步调用 LLM。

        参数：
            messages: 消息列表 [{"role": "user", "content": "..."}]
            tools: 工具定义（function calling 用）
            callbacks: 回调（Langfuse 用）

        返回：
            str: LLM 的回复内容
        """
        # 构造请求 payload
        payload = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # 重试循环（5xx/429/超时都重试）
        for attempt in range(self.retry_count + 1):
            try:
                resp = requests.post(
                    self.endpoint, json=payload, headers=headers, timeout=self.timeout
                )
                # 5xx 重试
                if resp.status_code >= 500 and attempt < self.retry_count:
                    continue
                # 429（限流）重试
                if resp.status_code == 429 and attempt < self.retry_count:
                    continue
                resp.raise_for_status()    # 4xx 直接抛

                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except requests.Timeout:
                if attempt < self.retry_count:
                    continue
                raise

    async def acall(self, *args, **kwargs):
        """异步调用（用 asyncio.to_thread 包装同步方法）。"""
        return await asyncio.to_thread(self.call, *args, **kwargs)
```

---

## 五、pending_deny 机制详解 —— 为什么 CrewAI 会"吞掉"异常？

这是整个系统最精妙也最反直觉的设计，初学者一定要理解。

### 5.1 问题：CrewAI 吞掉了安全拦截

假设我们想阻止 Agent 执行危险命令（比如 `rm -rf /`）。最直接的做法是在工具调用前抛异常：

```python
# ❌ 看似合理但实际无效的做法
@before_tool_call
def safety_check(context):
    if is_dangerous(context.tool_input):
        raise GuardrailDeny("禁止执行危险命令")  # 抛异常
```

**但 CrewAI 的实际行为是**：

```
1. Agent 想调用 execute_command("rm -rf /")
2. CrewAI 触发 before_tool_call → safety_check 抛 GuardrailDeny
3. CrewAI 捕获异常，认为"这个工具调用失败了"
4. CrewAI 告诉 Agent："execute_command 失败了，错误：GuardrailDeny"
5. Agent 想："哦失败了，那我换个命令试试"
6. Agent 又调用 execute_command("rm -rf ~")  ← 危险命令换了个写法！
7. 死循环...
```

**核心问题**：CrewAI 把异常当成"工具失败"，会让 Agent 重试，安全拦截根本没生效。

### 5.2 解决方案：pending_deny 延迟重抛

```python
# ✅ 正确做法：先存起来，等安全出口再抛
class CrewObservabilityAdapter:
    def on_before_tool_call(self, tool_name, tool_input):
        try:
            self._registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        except GuardrailDeny as e:
            # 不直接 raise！存到 _pending_deny
            self._pending_deny = e
            # 但工具实际不会执行（因为 gate 拦了）
```

然后在 `step_callback`（安全出口）重抛：

```python
async def _callback(step_output):
    adapter = get_current_adapter()
    # ... 触发 AFTER_TURN ...

    # ★ 在这里重抛，CrewAI 才会真正终止
    if adapter._pending_deny:
        pending = adapter._pending_deny
        adapter._pending_deny = None
        raise pending   # 抛出 → CrewAI 终止 → Runner 收到 → 回复用户"安全拦截"
```

### 5.3 完整的"安全拦截"流程图

```
Agent: "我要调用 execute_command('rm -rf /')"
       │
       ▼
CrewAI 触发 before_tool_call
       │
       ▼
sandbox_guard 检测到危险命令
       │
       ├─ 抛 GuardrailDeny("禁止 rm -rf")
       │
       ▼
adapter.on_before_tool_call 捕获异常
       │
       ├─ 存入 _pending_deny
       ├─ 工具实际不执行
       ├─ 补发 AFTER_TOOL_CALL（标记 guardrail_deny=True，给 Langfuse 记录）
       │
       ▼
CrewAI 告诉 Agent："execute_command 没返回结果"
       │
       ▼
Agent 继续思考（可能想重试）
       │
       ▼
本 step 结束 → 触发 step_callback
       │
       ▼
step_callback 检查 _pending_deny
       │
       ├─ 有 deny！重抛！
       │
       ▼
CrewAI 收到异常，终止执行
       │
       ▼
Runner 收到异常，回复用户："安全策略拦截了此操作"
```

**为什么叫"安全出口"？** 因为 `step_callback` 抛的异常会被 CrewAI 正确传播到 `kickoff()` 的调用方（Runner），而不是被吞掉。

---

## 六、设计优势与局限性

### 优势

1. **记忆动态注入**：每次 LLM 调用都从文件读取最新记忆（用户改了 user.md 立即生效）
2. **上下文自动压缩**：长对话不会超过 token 限制（maybe_compress 兜底）
3. **Hook 无缝集成**：通过 step_callback / before_llm_call 接入安全检查
4. **无状态设计**：每条消息创建新实例，天然支持并发
5. **结构化输出**：Pydantic 模型保证输出格式可控

### 局限性

1. **LLM 延迟**：Qwen3-max 响应通常需要 2-10 秒
2. **成本**：每次调用都消耗 token（有 cost_guard 围栏限制）
3. **不确定性**：LLM 输出不完全可控（有 Hook 兜底）
4. **配置复杂**：YAML + Pydantic + Python 三处配置，初学者容易晕

---

## 七、完整流程示例 —— 用户发消息到收到回复

```
1. 飞书用户发消息："帮我搜索 Python 新特性"
   │
   ▼
2. 飞书 Listener 收到 → 推到 asyncio Queue
   │
   ▼
3. Runner 从 Queue 取出 → 调用 agent_fn
   │
   ▼
4. agent_fn 创建新的 MemoryAwareCrew 实例
   │
   ├─ 加载 agents.yaml（orchestrator 配置）
   ├─ build_bootstrap_prompt（读 soul.md/user.md/agent.md/memory.md）
   ├─ 创建 SkillLoaderTool（构建技能清单 description）
   └─ 注册 before_llm_call / before_tool_call Hook
   │
   ▼
5. crew.akickoff(inputs={"user_message": "帮我搜索..."})
   │
   ▼
6. CrewAI 开始执行 Task
   │
   ├─ before_llm_hook 触发
   │   ├─ _restore_session（恢复历史对话）
   │   ├─ prune_tool_results（剪枝）
   │   ├─ maybe_compress（压缩）
   │   └─ on_before_llm（触发 BEFORE_LLM 事件）
   │
   ├─ 调用 LLM（Qwen3-max）
   │   └─ LLM 返回："调用 skill_loader(skill_name='baidu_search')"
   │
   ├─ CrewAI 执行工具调用
   │   ├─ before_tool_hook 触发（安全检查）
   │   ├─ SkillLoaderTool._run() 执行
   │   └─ step_callback 触发（AFTER_TURN + pending_deny 检查）
   │
   ├─ LLM 看到搜索结果，思考后给出最终答案
   │
   └─ Task 完成 → task_callback 触发（TASK_COMPLETE）
   │
   ▼
7. run_and_index 保存对话到磁盘 + 索引到向量库
   │
   ▼
8. 返回 reply 文本给 Runner
   │
   ▼
9. Runner 通过 sender 发送回复到飞书
```

---

## 八、❓ 常见问题

### Q1：为什么 `@agent` 方法里每次都 `new` 一个 Agent，会不会很慢？

**A**：会有一些开销，但不大。Agent 对象本身是轻量的（就是存配置），真正慢的是 LLM 调用（几秒）。而且每次新建保证了无状态，避免上次对话污染这次。

### Q2：`before_llm_call` 和 `before_tool_call` 有什么区别？

**A**：
- `before_llm_call`：**LLM 要被调用前**触发，每个 step 都会调一次（思考 N 轮就触发 N 次）。主要做记忆注入和上下文压缩。
- `before_tool_call`：**工具要被调用前**触发（Agent 决定调 skill_loader 时）。主要做安全检查和参数归一化。

### Q3：为什么 `temperature=0.3`？调高/调低有什么影响？

**A**：
- `temperature` 控制 LLM 输出的随机性：0 = 完全确定（每次回答一样），1 = 很随机
- 0.3 是经验值：既保证回答稳定（不会每次都不一样），又有一点创造性
- 如果做代码生成可以调到 0.1（更严谨）；做创意写作可以调到 0.7

### Q4：CrewAI 报 `Tool not found: baidu_search` 怎么办？

**A**：这是初学者最常见的错误。原因：LLM 把技能名当成工具名直接调用了。
- **错误调用**：`baidu_search(query="Python")` ← LLM 以为有这个工具
- **正确调用**：`skill_loader(skill_name="baidu_search", task_context="...")`
- **解决**：检查 agents.yaml 里的 backstory 是否强调了"必须通过 skill_loader 调用"

### Q5：`max_iter=50` 会不会烧很多钱？

**A**：理论上有风险，但有保护：
- cost_guard Hook 会监控每轮 token 消耗，超限会 deny
- loop_detector 会检测循环（Agent 反复调同一个工具），检测到会终止
- 实际正常对话 3-5 轮就结束了，50 是兜底

### Q6：`_restore_session` 为什么不直接把所有历史塞进去？

**A**：因为 token 有限制（qwen3-max 是 32K 上下文）。如果对话了 100 轮，全塞进去会超限。所以：
- 保留最近 10 轮工具结果（`prune_keep_turns=10`）
- 超长时用 LLM 摘要旧消息（`maybe_compress`）

### Q7：CrewAI 报 `ValidationError: field required` 怎么办？

**A**：通常是 `output_pydantic` 校验失败。LLM 输出的 JSON 缺了必填字段。
- 检查 `MainTaskOutput` 的字段是否都标了 `default` 或 `default_factory`
- 在 `expected_output` 里明确告诉 LLM 输出格式
- 兜底：`run_and_index` 里有 `try/except` 会用 `result.raw` 作为回复

### Q8：为什么用 `async def` 而不是 `def`？

**A**：因为整个系统是异步的（asyncio）。
- `async def` 让函数变成协程，可以 `await`
- 好处：一个进程能同时处理多个飞书消息（并发）
- 如果用同步 `def`，一条消息处理时整个进程会卡住

---

## 九、🔧 调试技巧

### 9.1 开启详细日志

```python
# 创建 MemoryAwareCrew 时传 verbose=True
crew_instance = MemoryAwareCrew(..., verbose=True)
```

或设置环境变量：

```bash
export CREWAI_VERBOSE=1
export LOG_LEVEL=DEBUG
```

### 9.2 查看 LLM 实际收到的消息

在 `before_llm_hook` 里加日志：

```python
@before_llm_call
def before_llm_hook(self, context):
    # 打印要发给 LLM 的消息（调试用）
    for i, msg in enumerate(context.messages):
        logger.debug(f"msg[{i}] role={msg.get('role')} content={str(msg.get('content'))[:200]}")
```

### 9.3 CrewAI 常见报错与解决

| 报错 | 原因 | 解决 |
|------|------|------|
| `Tool not found: xxx` | LLM 直接用技能名当工具名调用 | 检查 backstory 是否强调用 skill_loader |
| `ValidationError: field required` | LLM 输出不符合 Pydantic 模型 | 检查 expected_output 描述 |
| `asyncio.TimeoutError` | LLM 调用超时 | 检查网络 / QWEN_API_KEY / 模型名 |
| `KeyError: 'choices'` | LLM 返回了错误（不是 choices 结构） | 检查 API Key 是否正确 |
| `RuntimeError: max_iterations exhausted` | LLM 空响应重试耗尽 | 检查模型是否支持 function calling |
| `httpx.UnsupportedProtocol` | URL 格式错误 | 检查 sandbox_url 是否 http(s):// 开头 |

### 9.4 检查记忆文件是否正确加载

```python
# 临时打印 backstory
from xiaopaw.memory.bootstrap import build_bootstrap_prompt
from pathlib import Path

prompt = build_bootstrap_prompt(Path("data/workspace"))
print(prompt)
# 应该看到 <soul>...</soul> <user>...</user> 等标签
```

### 9.5 用 Langfuse 查看完整调用链

配置 Langfuse 后，访问 Langfuse 面板可以看到：
- 每次 LLM 调用的输入输出
- 每个工具调用的参数和结果
- token 消耗和耗时
- 完整的 trace 树（哪个 step 调了什么）

详见第 12 篇《观测层 — 日志与 Langfuse》。

---

## 十、验证你的理解

- [ ] 能画出 Agent/Task/Crew 三者的关系图吗？
- [ ] `step_callback` 和 `before_llm_call` 分别在什么时候触发？
- [ ] 为什么要用 `build_agent_fn` 工厂函数而不是直接实例化？
- [ ] `_restore_session` 做了什么？为什么需要恢复会话？
- [ ] `pending_deny` 机制解决什么问题？为什么不能直接抛异常？
- [ ] `before_llm_hook` 的 4 个步骤分别是什么？
- [ ] 为什么 `temperature=0.3` 而不是 0 或 1？

---

## 十一、下一步

理解了 MainCrew 后，下一篇我们会讲 `SkillLoaderTool` —— 那个让 Agent "知道有什么技能、需要时才加载"的关键工具。它是"渐进式能力披露"的核心实现。

> 下一篇：[07-SkillLoader与渐进式能力披露](./07-SkillLoader与渐进式能力披露.md)
