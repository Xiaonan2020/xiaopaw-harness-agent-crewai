# 12 - 观测层 — 日志与 Langfuse

## 本节学习目标

读完本节后，你将能够：

1. 理解为什么 LLM Agent 比"普通后端"更需要可观测性
2. 区分结构化日志与传统日志的差异，并能在生产环境写 JSON 日志
3. 读懂 Langfuse 的 Trace/Span/Generation 三层模型
4. 说出 Langfuse 集成的 5 个核心机制各解决什么问题
5. 排查"假成功"问题——即用户看到回复但实际功能失败
6. 部署一套自己的 Langfuse + Prometheus 指标系统

> 类比提示：把 Agent 想象成一个"医院"。结构化日志像"标准化表格病历"（字段固定、可统计），Langfuse 像"医院全程监控系统"（每个房间、每个动作都被录像，事后可回放）。两者互补，缺一不可。

---

## 一、可观测性的价值

### 1.1 没有可观测性的问题

LLM Agent 的特点：单次用户请求背后可能涉及 10+ 步骤——理解意图、推理、调用工具、Sub-Crew 二次推理、整理结果……任何一步失败都会让用户感觉"它没反应"。

```
没有可观测性：

用户：为什么我的搜索没有结果？
开发者：让我看看...
  → 翻日志：日志是自由文本，grep 不准
  → 看到 500 错误，但不知道哪一步出错
  → 只能逐步重现代码，耗时 2 小时


有可观测性：

用户：为什么我的搜索没有结果？
开发者：打开 Langfuse，看到 trace 树：
  - BEFORE_TURN: 消息接收 ✓
  - BEFORE_LLM: LLM 调用 ✓
  - BEFORE_TOOL_CALL: skill_loader 调用 ✓
  - Sub-Crew: baidu_search 执行
    - MCP 工具调用：execute_command
    - 结果：API 返回空（搜索词太特殊）  ← 这里！
  - AFTER_TURN: 完成
定位问题：3 分钟
```

### 1.2 两类观测手段

```
┌─────────────────────────────────────┐
│  结构化日志（structured_log）        │
│  ────────────────────────────       │
│  特点：JSON 格式，本地文件           │
│  用途：事后排查、grep 分析           │
│  优势：简单、无外部依赖              │
│  类比：标准化表格病历                │
├─────────────────────────────────────┤
│  Langfuse 全链路追踪                 │
│  ────────────────────────────       │
│  特点：可视化 Trace 树               │
│  用途：实时监控、性能分析            │
│  优势：直观、支持多轮对话关联        │
│  类比：医院监控系统                  │
└─────────────────────────────────────┘
```

**为什么要两套？**
- 结构化日志：本地兜底，即使 Langfuse 网络断了也有据可查
- Langfuse：可视化、跨轮次聚合，能直观看出"用户和 Agent 整段对话"
- 两者互为补充——日志系统不能成为"单点故障"

---

## 二、结构化日志

### 2.1 设计理念：标准化表格 vs 自由日记

传统日志像"自由日记"，每行格式都不一样，机器无法可靠解析：

```text
// 传统日志（自由日记）
2024-01-15 10:30:00 INFO User asked to search Python
2024-01-15 10:30:01 INFO calling skill_loader
2024-01-15 10:30:02 WARN baidu_search returned empty result
```

问题：你想要"统计所有 baidu_search 的失败次数"，得写复杂的正则去匹配"baidu_search returned empty"——一旦有人改了日志文案，统计就崩了。

结构化日志像"标准化表格"，每个字段都是 JSON 的 key，机器可以直接 `entry["event"] == "after_tool_call"` 取值：

```json
// 结构化日志（标准化表格）
{"ts":"2024-01-15T10:30:00Z","level":"INFO","event":"before_turn","session_id":"abc123","user_message":"search Python"}
{"ts":"2024-01-15T10:30:02Z","level":"WARN","event":"after_tool_call","tool_name":"baidu_search","success":false}
```

**对比同一事件的两种格式**：

| 维度 | 传统日志 | 结构化日志 |
|------|---------|-----------|
| 格式 | 自由文本 | JSON |
| 字段查询 | 写正则匹配文案 | `entry["tool_name"]` |
| 统计 | 难 | `for e in logs: if e["success"]: ...` |
| 升级 schema | 改文案就崩 | 加新字段不破坏旧解析 |
| 跨工具分析 | 不可能 | 可以用 jq/pandas |

### 2.2 实现

```python
# shared_hooks/structured_log.py
"""结构化日志 Hook —— JSON 事件日志。

设计目标：
1. 每条日志是一行 JSON（JSONL 格式），方便流式读取
2. 字段固定：ts / event / session_id / ...
3. 长字段做截断（如 tool_input_preview 只保留 200 字符）
   避免一条日志几十 KB 把文件撑爆
"""

import json      # 用于把 dict 序列化为 JSON 字符串
import sys       # 标准库（这里其实未用到，可移除）
import time      # 用于生成 ISO 8601 时间戳
from pathlib import Path  # Path 对象处理路径跨平台

from xiaopaw.hook_framework.registry import EventType, HookContext


# 日志文件路径：相对运行目录的 data/logs/events.jsonl
# .jsonl = JSON Lines，每行一个 JSON 对象
_LOG_DIR = Path("data/logs")
_LOG_FILE = _LOG_DIR / "events.jsonl"


def _log_event(event_type: str, data: dict) -> None:
    """写入一条 JSON 日志。

    参数表：
    ----------
    event_type : str
        事件类型，如 "before_turn"、"after_tool_call"
    data : dict
        事件相关数据，如 {"session_id": "abc", "tool_name": "baidu_search"}

    返回值：
    ----------
    None（写文件，无返回）

    注意事项：
    ----------
    - 每次调用都会打开/关闭文件（简单但性能一般，高并发场景应改用 logging handler）
    - ensure_ascii=False 保证中文不转义成 \uXXXX
    - 末尾加 \n：JSONL 规范要求每行一个对象
    """
    # 组装日志条目：固定字段 ts/event + 业务字段
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),  # UTC 时间
        "event": event_type,
        **data,  # 把 data 里的字段展开进来
    }
    # 确保目录存在（parents=True 表示连父目录一起创建）
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # 以追加模式打开文件（"a" = append）
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        # json.dumps 把 dict 转成 JSON 字符串
        # ensure_ascii=False 让中文字符原样输出
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── 7 个事件的 Handler ──────────────────────────
# 每个 Handler 对应一个生命周期事件
# 共同点：从 ctx（HookContext）读数据 → 调 _log_event 写日志


def before_turn_handler(ctx: HookContext):
    """轮次开始 —— 记录用户消息。

    参数表：
    ----------
    ctx : HookContext
        框架传入的上下文，包含 session_id/sender_id/turn_number 等

    使用示例：
    ----------
    >>> # 框架在 BEFORE_TURN 事件自动调用此函数
    >>> # 你不需要手动调用
    """
    _log_event("before_turn", {
        "session_id": ctx.session_id,    # 会话 ID，关联多轮对话
        "sender_id": ctx.sender_id,      # 发消息的飞书用户 ID
        "turn_number": ctx.turn_number,  # 第几轮对话（1, 2, 3...）
    })


def before_llm_handler(ctx: HookContext):
    """LLM 调用前 —— 记录模型和 token 数。

    为什么记 input_tokens？
    用于事后统计成本（cost_guard 也读这个字段）
    """
    _log_event("before_llm", {
        "session_id": ctx.session_id,
        "agent_id": ctx.agent_id,            # 哪个 Agent（main/sub_crew）
        "input_tokens": ctx.input_tokens,    # 输入 token 数
    })


def before_tool_handler(ctx: HookContext):
    """工具调用前 —— 记录工具名和参数。

    为什么截断到 200 字符？
    工具参数可能很大（如搜索一篇文章），全写进日志会让文件膨胀。
    200 字符足够看出"调用了什么"，又不会爆磁盘。
    """
    _log_event("before_tool_call", {
        "session_id": ctx.session_id,
        "tool_name": ctx.tool_name,
        # str(ctx.tool_input)[:200] 把参数转字符串后只取前 200 字符
        "tool_input_preview": str(ctx.tool_input)[:200],
    })


def after_tool_handler(ctx: HookContext):
    """工具调用后 —— 记录结果和耗时。

    duration_ms（毫秒）很重要：能定位"哪个工具慢"
    success（True/False）用于计算失败率
    """
    _log_event("after_tool_call", {
        "session_id": ctx.session_id,
        "tool_name": ctx.tool_name,
        "duration_ms": ctx.duration_ms,  # 耗时（毫秒）
        "success": ctx.success,          # 是否成功
    })


def after_turn_handler(ctx: HookContext):
    """轮次结束 —— 记录耗时和回复摘要。

    guardrail_deny=True 表示这一轮被安全策略拦截了
    事后可以 grep 这个字段统计拦截率
    """
    reply = ctx.metadata.get("reply", "")  # 从 metadata 取回复内容
    _log_event("after_turn", {
        "session_id": ctx.session_id,
        "duration_ms": ctx.duration_ms,
        "reply_preview": reply[:200],  # 回复内容前 200 字符
        # guardrail_deny 标记本轮是否被拦截
        "guardrail_deny": ctx.metadata.get("guardrail_deny", False),
    })


def task_complete_handler(ctx: HookContext):
    """任务完成。"""
    _log_event("task_complete", {
        "session_id": ctx.session_id,
        "task_name": ctx.task_name,
    })


def session_end_handler(ctx: HookContext):
    """会话结束。"""
    _log_event("session_end", {
        "session_id": ctx.session_id,
    })
```

**模块逻辑解释**：

1. **`_log_event` 是核心函数**：所有 7 个 Handler 都调用它，统一格式
2. **字段截断策略**：长字段（如 `tool_input_preview`、`reply_preview`）都 `[:200]` 截断，防止日志爆炸
3. **append 模式**：用 `"a"` 模式打开文件，新日志追加到末尾，不覆盖历史
4. **JSONL 格式**：每行一个 JSON 对象，方便 `for line in file: json.loads(line)` 流式处理

### 2.3 日志分析

```bash
# 查看某个会话的所有事件
# 思路：把每一行 JSON 解析出来，过滤 session_id
cat data/logs/events.jsonl | python -c "
import json, sys
for line in sys.stdin:               # 逐行读取
    entry = json.loads(line)          # 解析 JSON
    if entry.get('session_id') == 'abc123':   # 过滤指定 session
        print(f\"{entry['ts']} {entry['event']}\")
"

# 统计工具调用失败率
# 思路：遍历所有 after_tool_call 事件，统计 success=False 的比例
cat data/logs/events.jsonl | python -c "
import json, sys
total = 0
failed = 0
for line in sys.stdin:
    entry = json.loads(line)
    if entry.get('event') == 'after_tool_call':  # 只看工具调用后
        total += 1
        if not entry.get('success', True):       # success=False 算失败
            failed += 1
print(f'失败率：{failed}/{total} = {failed/total*100:.1f}%')
"
```

**逐字符解释**：
- `entry.get('session_id')`：用 `.get()` 而不是 `entry['session_id']`，因为有些事件可能没有这个字段，`.get()` 找不到返回 `None` 而不是报错
- `f'{failed/total*100:.1f}%'`：`:.1f` 表示保留 1 位小数

---

## 三、Langfuse 全链路追踪

### 3.1 Langfuse 是什么？

Langfuse 是一个开源的 LLM 可观测性平台（self-hostable），提供：

| 功能 | 说明 | 生活类比 |
|------|------|---------|
| Trace | 一次完整的用户交互（多轮对话） | 一次住院的完整记录 |
| Span | Trace 内的一个操作（如工具调用） | 住院期间的一次检查 |
| Generation | LLM 调用记录（含 prompt/response） | 一次问诊 |
| Session | 多轮对话的关联视图 | 一个病人的全部住院史 |

**通俗解释 Trace/Span/Generation**：

```
Trace（追踪）= 整棵树
  ├── "用户问：帮我搜索 Python"
  │     这是一个 Trace，从用户发消息开始，到 Agent 回复结束
  │
  ├── Span（跨度）= 树上的一个节点
  │     ├── "调用 skill_loader"（一次工具调用 = 一个 Span）
  │     └── "调用 baidu_search"（嵌套的子 Span）
  │
  └── Generation（生成）= 特殊的 Span，专指 LLM 调用
        ├── 记录了 model="qwen3-max"
        ├── 记录了 input（prompt）
        └── 记录了 output（LLM 回复）
```

为什么 Generation 单独是一类？因为它额外携带 model/input_tokens/output_tokens/cost 这些 LLM 特有信息，普通 Span 不需要这些字段。

### 3.2 Trace 树结构

```
Trace: session-abc123（整个会话）
├── Span: before_turn（轮次开始）
├── Generation: LLM 调用 #1（主 Agent 推理）
│   ├── input: "帮我搜索 Python 新特性"
│   └── output: "调用 skill_loader"
├── Span: tool-skill_loader（技能加载）
│   ├── Generation: LLM 调用 #2（Sub-Crew 推理）
│   │   ├── input: "执行百度搜索"
│   │   └── output: "调用 execute_command"
│   ├── Span: tool-execute_command（沙箱命令执行）
│   │   └── output: "搜索结果..."
│   └── Generation: LLM 调用 #3（整理结果）
│       └── output: "Python 3.12 的新特性..."
└── Span: after_turn（轮次结束）
```

**如何读 trace 定位问题**：

1. **从根 Span 开始看**：确认 `name` 是不是 `xiaopaw_session`，`source` 是不是 `xiaopaw-v2`
2. **找红色/黄色节点**：Langfuse UI 里 `level=WARNING` 是黄色，`level=ERROR` 是红色
3. **点开最深的 Span**：通常问题出在最底层（如 `execute_command` 返回空）
4. **看 Generation 的 output**：如果 LLM 输出"我应该重试"，但实际工具失败，问题在工具不在 LLM

### 3.3 核心机制

Langfuse 集成有 5 个关键机制，缺一不可：

#### 机制一：多轮对话留在同一棵树（trace_id = session_id）

```python
# shared_hooks/langfuse_trace.py

# 核心思想：trace_id = session_id
# 利用 Langfuse 的 upsert 语义：相同 trace_id 的多次写入会合并到同一棵树

# upsert 是什么？
# upsert = update + insert
# - 如果 trace_id 不存在 → 创建新 trace
# - 如果 trace_id 已存在 → 更新已有 trace
#
# 为什么这样设计？
# 一个 session（会话）可能有多轮对话（多个 turn）
# 每轮都会调 langfuse.trace(id=trace_id, ...)
# 如果每次都创建新 trace，多轮对话就分散在不同 trace 里，没法看整体
# 用 session_id 作为 trace_id，多轮对话自动合并到同一棵树

# contextvars.ContextVar 是什么？
# Python 的"线程局部变量"升级版
# 在异步代码里，每个协程有自己的副本，互不干扰
# 子协程通过 copy_context() 继承父协程的值
_trace_id_var = contextvars.ContextVar("trace_id", default="")

def before_turn_handler(ctx: HookContext):
    """轮次开始时创建/复用 trace。

    参数表：
    ----------
    ctx : HookContext
        上下文，包含 session_id（用作 trace_id）

    返回值：
    ----------
    None

    使用示例：
    ----------
    >>> # 框架在 BEFORE_TURN 自动调用
    >>> # 第一轮：trace_id="abc"，创建新 trace
    >>> # 第二轮：trace_id="abc"，更新已有 trace（合并到同一棵树）
    """
    trace_id = ctx.session_id  # ★ 用 session_id 作为 trace_id
    _trace_id_var.set(trace_id)  # 把 trace_id 存到 ContextVar

    # upsert：如果 trace 已存在则更新，不存在则创建
    # langfuse.trace() 是 Langfuse SDK 的方法
    # id 参数是 trace 的唯一标识
    # name 是 trace 的显示名（在 UI 上显示）
    # user_id 关联到 Langfuse 的用户系统
    langfuse.trace(
        id=trace_id,
        name="xiaopaw_session",
        user_id=ctx.sender_id,
    )
```

**生活类比**：upsert 就像酒店入住。第一次报房号"301"系统会新建一个房间记录；第二次再报"301"系统不会新建，而是更新已有记录（加新的服务消费）。多轮对话 = 同一个客人多次点客房服务，都记到"301"账上。

#### 机制二：Sub-crew 自动挂到父 trace

```python
# Sub-crew 是什么？
# Sub-crew = 子工作流（sub-crew = sub-crewai = 子任务执行器）
# 主 Agent 觉得"这个任务我需要百度搜索"→ 启动 Sub-crew → Sub-crew 调用工具

# 问题：Sub-crew 在子线程/子协程里跑
# 如果子线程看不到父线程的 trace_id，它的日志就脱离了 trace 树

# 解决：通过 copy_context() 继承父线程的 ContextVar
# 子线程的 trace_id 自动 = 父线程的 trace_id
# 不需要显式传 parent_id

# 在 skill_loader.py 中：
ctx = contextvars.copy_context()  # 快照所有 ContextVar
# 子线程里 _trace_id_var 自动可见（继承父值）
# 所以子线程调 langfuse.span(trace_id=_trace_id_var.get())
# 拿到的 trace_id 和父线程一样，自动挂到同一棵树
```

**通俗解释**：`copy_context()` 就像给子线程拷贝一份"父亲的身份证"。子线程拿着这份身份证去 Langfuse 登记，Langfuse 一看"哦，你的 trace_id 和你爸一样"，自动把你挂到你爸名下。

#### 机制三：Span 栈维护嵌套关系

```python
# 为什么需要栈？
# 一个工具调用可能嵌套另一个工具
# 如：skill_loader 调用 baidu_search，baidu_search 又调用 execute_command
# Langfuse 需要知道"baidu_search 是 skill_loader 的子 span"
# 用栈记录"当前在哪一层"，每次进入新工具压栈，退出时弹栈

# 为什么用元组（tuple）而不用 list？
# 元组不可变 → 在 ContextVar 跨协程传播时更安全
# 修改元组必须创建新元组（new = old + (elem,)）
# 不会出现"多个协程改同一个 list 导致数据错乱"
_span_stack_var = contextvars.ContextVar("span_stack", default=())

def before_tool_handler(ctx: HookContext):
    """工具调用前压栈。

    参数表：
    ----------
    ctx : HookContext
        包含 tool_name、session_id

    返回值：
    ----------
    None（但会调 langfuse.span() 创建 span）

    注意事项：
    ----------
    - _tool_count 是模块级变量，用于生成唯一 span_id
    - parent_id 取栈顶元素，栈空时取根 span
    """
    # 生成唯一的 span_id（如 "span-baidu_search-3"）
    span_id = f"span-{ctx.tool_name}-{_tool_count}"
    _tool_count += 1  # 全局计数器递增

    # push：新元组 = 旧元组 + 新元素
    # 元组的 + 操作返回新元组，不修改原元组
    old_stack = _span_stack_var.get()  # 取当前栈（元组）
    new_stack = old_stack + ((span_id, ctx.tool_name),)  # 追加新元素
    _span_stack_var.set(new_stack)  # 设置新栈

    # 计算 parent_id：
    # 如果栈非空 → parent = 栈顶（上一个未关闭的 span）
    # 如果栈空 → parent = 根 span
    # old_stack[-1] 取元组最后一个元素
    # [0] 取它的 span_id 部分
    parent_id = old_stack[-1][0] if old_stack else _root_span_id_var.get()

    # 创建 Langfuse span
    # parent_observation_id 指定父节点，建立嵌套关系
    langfuse.span(
        id=span_id,
        trace_id=_trace_id_var.get(),           # 关联到当前 trace
        parent_observation_id=parent_id,        # ★ 指定父节点
        name=ctx.tool_name,
    )


def after_tool_handler(ctx: HookContext):
    """工具调用后弹栈。

    注意事项：
    ----------
    - 弹栈 = 取除最后一个元素的元组
    - stack[:-1] 返回新元组（不含最后一个元素）
    """
    stack = _span_stack_var.get()
    if stack:
        # pop：取除最后一个元素
        # 元组切片 stack[:-1] 返回新元组
        _span_stack_var.set(stack[:-1])
```

**栈的变化过程示例**：

```
初始：stack = ()

调用 skill_loader：
  before: old=()         new=(("span-skill_loader-0",),)   parent=root
  after_tool: stack = ()  (弹栈后)

如果 skill_loader 内部又调用 baidu_search：
  初始：stack = (("span-skill_loader-0",),)

  调用 baidu_search：
    before: old=(("span-skill_loader-0",),)
            new=(("span-skill_loader-0",), ("span-baidu_search-1",))
            parent="span-skill_loader-0"  ← 挂到 skill_loader 下
    after_tool: stack = (("span-skill_loader-0",),)  (弹栈后)

  退出 skill_loader：
    after_tool: stack = ()
```

#### 机制四：Generation 先写后更新

```python
# 为什么 Generation 需要"先写后更新"？
# LLM 调用是异步的：开始时只有 input（prompt），结束时才有 output
# 如果等 output 出来再写，万一 LLM 中途崩了，连开始的记录都没有
# 所以分两步：
#   1. before_llm：先创建 generation，写入 input
#   2. after_turn：拿到 output 后，更新同一个 generation

_gen_id_var = contextvars.ContextVar("gen_id", default="")

def before_llm_handler(ctx: HookContext):
    """LLM 调用前创建 generation（只写 input）。

    参数表：
    ----------
    ctx : HookContext
        ctx.metadata 包含 model、messages

    注意事项：
    ----------
    - 先写 input 是为了即使后续失败，至少能看到"调用了什么"
    - gen_id 存到 ContextVar，after_turn 时取出来更新
    """
    gen_id = f"gen-{_gen_count}"  # 生成唯一 ID
    _gen_count += 1
    _gen_id_var.set(gen_id)  # 存到 ContextVar

    # 先写入 input（output 还没有）
    # 同一个 id 多次调用 langfuse.generation() 会触发 upsert
    # 第一次创建，第二次更新
    langfuse.generation(
        id=gen_id,
        trace_id=_trace_id_var.get(),
        model=ctx.metadata.get("model", ""),         # 模型名
        input=ctx.metadata.get("messages", []),       # prompt
    )

def after_turn_handler(ctx: HookContext):
    """轮次结束时补完 generation 的 output。

    为什么在 after_turn 而不是 after_llm？
    因为 CrewAI 的回调可能不暴露 after_llm，
    但 after_turn 一定有，且此时 reply 已生成
    """
    gen_id = _gen_id_var.get()
    if gen_id:
        # 更新 output
        # 同一个 id 再调 generation() → upsert 更新
        langfuse.generation(
            id=gen_id,
            output=ctx.metadata.get("reply", ""),  # LLM 回复
        )
        _gen_id_var.set("")  # 清空，等待下一轮
```

**两步写入的时序图**：

```
时间轴 →

before_llm:    [创建 gen-1, 写入 input="帮我搜索"]
  │
  │ LLM 推理中...（可能几秒）
  │
  ▼
after_turn:     [更新 gen-1, 写入 output="调用 skill_loader"]
```

#### 机制五：强制 flush 保证可见性

```python
def after_turn_handler(ctx: HookContext):
    """轮次结束 —— 必须在 sender.send() 之前 flush。

    为什么 flush 必须在 send 之前？
    ───────────────────────────────
    时序图：

    错误顺序：
    after_turn:
      └─ sender.send("回复")       ← 用户看到回复了
      └─ _flush_batch()            ← Langfuse 才开始上传
      └─ 用户打开 Langfuse：看不到 trace！
         （因为还在上传中）

    正确顺序：
    after_turn:
      └─ _flush_batch()            ← 先上传到 Langfuse
      └─ sender.send("回复")       ← 再发回复给用户
      └─ 用户看到回复 → 打开 Langfuse → trace 已就位 ✓
    """
    # ... 更新 generation ...

    # ★ 强制 flush
    # 必须在 runner 发送回复给用户之前完成
    # 否则用户看到回复了但 Langfuse 还没数据
    _flush_batch()


def _flush_batch():
    """把缓冲区的事件推送到 Langfuse。

    返回值：
    ----------
    None（但会阻塞直到上传完成或超时）

    注意事项：
    ----------
    - Langfuse SDK 默认是异步批量上传（性能考虑）
    - flush() 强制立即上传当前缓冲区
    - 如果网络慢，会阻塞几秒
    """
    langfuse.flush()
```

### 3.4 SESSION_END 清理

```python
def flush_and_close(ctx: HookContext):
    """会话结束 —— 关闭所有未关闭的 span/gen + flush。

    为什么要清理？
    ─────────────
    如果一个 span 没有设置 end_time，Langfuse UI 会显示它"还在运行"
    会话结束时统一收尾，避免 UI 显示"幽灵 span"

    参数表：
    ----------
    ctx : HookContext
        包含 session_id

    注意事项：
    ----------
    - 即使中途出错，也要保证清理逻辑执行
    - 建议放在 try/finally 的 finally 块里
    """
    # 关闭所有未关闭的 generation
    gen_id = _gen_id_var.get()
    if gen_id:
        # end_time = datetime.now() 标记结束时间
        langfuse.generation(id=gen_id, end_time=datetime.now())

    # 关闭所有未关闭的 span
    # reversed(stack) 从栈顶往栈底关，模拟"后进先出"
    stack = _span_stack_var.get()
    for span_id, _ in reversed(stack):
        langfuse.span(id=span_id, end_time=datetime.now())

    # 清空栈
    _span_stack_var.set(())
    _gen_id_var.set("")

    # 最后一次 flush
    _flush_batch()
```

---

## 四、Langfuse 部署

### 4.1 自托管 Langfuse

```bash
# 用 Docker 快速部署
# 前提：已安装 Docker 和 docker-compose

git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d
# -d 表示后台运行（detach）

# 访问 http://localhost:3000
# 首次访问需要注册管理员账号
# 然后创建 project，获取 API Key
```

### 4.2 配置环境变量

```bash
# 这两个 Key 在 Langfuse Web UI 里创建 project 后获得
export XIAOPAW_LANGFUSE_PUBLIC_KEY="pk-lf-xxx"   # pk- 开头 = public key
export XIAOPAW_LANGFUSE_SECRET_KEY="sk-lf-xxx"   # sk- 开头 = secret key
export TRACE_TO_LANGFUSE=true                     # 总开关

# 可选：自托管时设置 URL
# 如果不设置，默认连官方 Langfuse Cloud
# export XIAOPAW_LANGFUSE_BASE_URL="http://localhost:3000"
```

### 4.3 在 config.yaml 中启用

```yaml
observability:
  enable_langfuse: true                  # 总开关
  langfuse_host: "http://localhost:3000"  # Langfuse 服务地址
  langfuse_public_key: ""                 # 从环境变量读
  langfuse_secret_key: ""                 # 从环境变量读
```

---

## 五、Metrics 指标

### 5.1 Prometheus 指标

> 背景知识：Prometheus 是什么？
> Prometheus 是一个开源的监控系统。它的核心思想是"拉模式"：
> 1. 你的程序把指标暴露在 HTTP 端点（如 `/metrics`）
> 2. Prometheus 服务器定时来"拉"这些指标
> 3. 用 Grafana 等工具可视化
>
> 常见指标类型：
> - Counter（计数器）：只增不减，如"总请求数"
> - Histogram（直方图）：统计分布，如"延迟分布"

```python
# xiaopaw/observability/metrics.py
from prometheus_client import Counter, Histogram

# 入站消息计数（Counter）
# 参数1：指标名（必须以字母开头，下划线分隔）
# 参数2：帮助文本（在 /metrics 端点显示）
# 参数3：标签（labels），用于按维度过滤
inbound_total = Counter(
    "xiaopaw_inbound_total",        # 指标名
    "Total inbound messages",        # 帮助文本
    ["routing_type"]                 # 标签：p2p（私聊）或 group（群聊）
)

# Agent 处理延迟（Histogram）
# buckets 定义直方图的桶边界
# 如 buckets=[0.5, 1, 2, 5, ...] 表示统计
# "<0.5s 的有多少个"，"0.5-1s 的有多少个"，...
agent_latency = Histogram(
    "xiaopaw_agent_latency_seconds",
    "Agent processing latency",
    ["routing_type"],
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120, 300]  # 单位：秒
)
```

### 5.2 Metrics 服务端

```python
# xiaopaw/observability/metrics_server.py
from aiohttp import web
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

async def start_metrics_server(host: str, port: int):
    """启动 Prometheus metrics 端点。

    参数表：
    ----------
    host : str
        监听地址，如 "0.0.0.0"
    port : int
        监听端口，如 8090

    返回值：
    ----------
    web.AppRunner
        aiohttp 的 runner 对象，用于后续清理

    使用示例：
    ----------
    >>> runner = await start_metrics_server("0.0.0.0", 8090)
    >>> # 现在访问 http://localhost:8090/metrics 就能看到指标
    """
    async def metrics_handler(request):
        """处理 /metrics 请求。

        generate_latest() 返回当前所有指标的文本格式
        CONTENT_TYPE_LATEST 是正确的 Content-Type
        """
        return web.Response(
            body=generate_latest(),          # 生成 Prometheus 文本格式
            content_type=CONTENT_TYPE_LATEST,  # 如 "text/plain; version=0.0.4"
        )

    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)  # 注册路由

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    return runner
```

### 5.3 查询指标

```bash
# 获取指标
curl http://localhost:8090/metrics

# 输出示例（Prometheus 文本格式）：
# xiaopaw_inbound_total{routing_type="p2p"} 42
# xiaopaw_inbound_total{routing_type="group"} 15
# xiaopaw_agent_latency_seconds_bucket{routing_type="p2p",le="2"} 38
```

**逐字段解释输出**：
- `xiaopaw_inbound_total`：指标名
- `{routing_type="p2p"}`：标签，表示这是私聊的统计
- `42`：当前值（42 条私聊消息）
- `le="2"`：less than or equal，"≤2秒"的桶

---

## 六、正确读 Langfuse Trace

### 6.1 不要被假成功骗

"假成功"是最隐蔽的 bug：用户看到 Agent 回复了"好的，已记住你的信息"，但其实底层操作失败了。

```
Trace 显示：
  根 span output: "好的，已记住你的信息"
  → 看起来成功了

但是仔细看子 span：
  tool span (memory-save):
    level: WARNING                  ← 警告级别
    statusMessage: "Permission denied, tried alternate path"
    → 实际写入失败！

为什么会产生假成功？
  Agent 的 LLM 看到 tool 调用"返回了"（即使返回的是错误）
  就乐观地告诉用户"已完成"
  → 这就是为什么需要结构化日志 + 审计日志双重核对
```

### 6.2 假成功排查步骤

完整的排查流程：

```
Step 1：打开 Langfuse，找到这个 session 的 trace

Step 2：检查根 span
  - name 是不是 "xiaopaw_session"？
  - source 是不是 "xiaopaw-v2"？
  - 如果不是 → 你的 trace 可能混入了其他系统的数据

Step 3：展开每个 tool span，看 level 字段
  - DEFAULT（绿色）→ 正常
  - WARNING（黄色）→ 有问题，看 statusMessage
  - ERROR（红色）→ 失败，看 output

Step 4：点开最里层的 file_operations span
  - 看 output 的 JSON 里 success 字段
  - success=true 才是真的成功
  - success=false 即使上层 span 是绿色，也是假成功

Step 5：跨 session 验证
  - 新开一个会话，问"还记得我吗"
  - 如果 Agent 答不出 → 确认上次保存失败
  - 这是端到端断言，最可靠
```

### 6.3 正确的检查方法

1. **检查根 span**：`name`、`source: xiaopaw-v2`、tree 结构
2. **检查每个 tool span**：`level`（DEFAULT vs WARNING）、`statusMessage`
3. **检查最里层**：file_operations 的 output JSON 里 `success` 字段
4. **跨 session 验证**：新会话问"还记得我吗"才是真正的端到端断言

---

## 七、设计优势与局限性

### 优势

1. **双保险**：结构化日志 + Langfuse 互为补充，任一失效另一个还能用
2. **全链路追踪**：从消息到回复的每一步都可见
3. **多轮关联**：同一 session 的多轮对话在同一棵树（upsert 语义）
4. **实时监控**：Prometheus 指标实时暴露，Grafana 可配告警

### 局限性

1. **Langfuse 依赖**：网络故障时 trace 可能丢失（但结构化日志还在）
2. **性能开销**：每次事件都有序列化和网络开销（约 5-10ms）
3. **存储成本**：trace 数据量大，需要定期清理（Langfuse 默认保留 30 天）

---

## 八、常见问题

### ❓ 常见问题

**Q1：Langfuse 连接失败怎么办？**

A：按以下步骤排查：
1. 检查 `TRACE_TO_LANGFUSE` 是否为 `true`
2. 检查环境变量 `XIAOPAW_LANGFUSE_PUBLIC_KEY` 和 `XIAOPAW_LANGFUSE_SECRET_KEY` 是否设置
3. 用 `curl http://localhost:3000/api/public/health` 测试 Langfuse 是否启动
4. 看启动日志有没有 `langfuse connection failed` 之类的错误
5. 如果是自托管，确认 Docker 容器在跑：`docker compose ps`

**Q2：trace 在 Langfuse UI 里看不到？**

A：常见原因：
- flush 没执行：检查 `after_turn_handler` 里是否调了 `_flush_batch()`
- flush 在 send 之后：用户看到回复但 trace 还没上传，调整顺序
- trace_id 为空：检查 `before_turn_handler` 里 `_trace_id_var.set()` 有没有执行
- 网络问题：Langfuse SDK 上传失败会静默重试，看 Langfuse 容器日志

**Q3：多轮对话没有合并到同一棵 trace 树？**

A：检查 `trace_id` 是否一致：
- 每轮的 `before_turn_handler` 里打印 `_trace_id_var.get()`
- 如果每轮不同 → session_id 变了（可能是新会话）
- 如果相同但没合并 → Langfuse 版本太旧，升级到 2.x+

**Q4：结构化日志文件越来越大怎么办？**

A：
- 用 logrotate 工具按天切割
- 或者改 `_log_event` 写入时按日期分文件（如 `events-2024-01-15.jsonl`）
- 定期归档到对象存储（OSS/S3）

**Q5：Prometheus 指标 `/metrics` 返回 404？**

A：
- 检查 `start_metrics_server` 有没有调用
- 检查端口是否被占用（`netstat -an | findstr 8090`）
- 检查防火墙是否放行端口

**Q6：Generation 只显示了 input 没有 output？**

A：
- `after_turn_handler` 里有没有更新 output？
- 检查 `ctx.metadata.get("reply", "")` 是否为空
- 如果 reply 为空，可能是 Runner 没把回复塞进 metadata

### 🔧 调试技巧

1. **临时加打印**：在 `_log_event` 开头加 `print(entry)`，实时看日志是否产生
2. **Langfuse SDK 调试模式**：设环境变量 `LANGFUSE_DEBUG=true`，SDK 会打印详细日志
3. **trace_id 一致性检查**：
   ```python
   # 在每个 handler 开头加：
   print(f"[{event}] trace_id={_trace_id_var.get()}, session={ctx.session_id}")
   ```
4. **模拟离线测试**：把 `TRACE_TO_LANGFUSE=false`，只看结构化日志，排除 Langfuse 干扰
5. **用 jq 分析日志**：
   ```bash
   # 找出所有失败的工具调用
   cat data/logs/events.jsonl | jq 'select(.event=="after_tool_call" and .success==false)'
   ```

---

## 九、验证你的理解

- [ ] 结构化日志相比传统文本日志有什么优势？
- [ ] Langfuse 的 Trace/Span/Generation 分别是什么？三者什么关系？
- [ ] Langfuse 集成的五个机制分别解决什么问题？
- [ ] 为什么 `_flush_batch()` 必须在 `sender.send()` 之前？
- [ ] 为什么 trace_id = session_id？upsert 语义是什么意思？
- [ ] 怎样正确读 Langfuse trace，避免被"假成功"骗？
- [ ] Prometheus 的 Counter 和 Histogram 有什么区别？

---

> 下一篇：[13-可靠性策略-cost-loop-retry](./13-可靠性策略-cost-loop-retry.md)
