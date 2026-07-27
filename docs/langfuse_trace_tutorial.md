# `shared_hooks/langfuse_trace.py` 小白教程

> 目标：让你真正看懂这个文件在做什么，而不是只会背注释。
>
> 对应文件：[`shared_hooks/langfuse_trace.py`](file:///d:/ProjectsCodes/企业级智能体实战/xiaopaw-v2/shared_hooks/langfuse_trace.py)

---

## 一、这文件到底是干嘛的？

想象你开了一家餐厅：

- 顾客点单 → 厨师做菜 → 服务员上菜
- 但某天顾客投诉菜不好吃，你想知道**到底是哪个环节出了问题**

最笨的办法是：在厨房每个角落装摄像头，把每一步录下来。

在 AI Agent 项目里，**Langfuse** 就是那个“摄像头”，而这个文件 [`langfuse_trace.py`](file:///d:/ProjectsCodes/企业级智能体实战/xiaopaw-v2/shared_hooks/langfuse_trace.py) 就是负责：

> **把 Agent 运行过程中发生的各种事件，翻译成 Langfuse 能看懂的“追踪树（trace tree）”。**

这些事件包括：

- 用户说了一句话（`before_turn`）
- Agent 调用了一次大模型（`before_llm`）
- Agent 调用了一个工具（`before_tool` / `after_tool`）
- 一轮对话结束（`after_turn`）
- 整个会话结束（`session_end`）

---

## 二、必须懂的 5 个概念（用大白话讲）

### 1. Langfuse 是什么？

Langfuse 是一个** observability（可观测性）平台**，专门给 LLM/Agent 应用用的。

你可以把它理解成：

> 一个网页后台，能看到每一次用户请求里，Agent 调用了几次模型、用了哪些工具、每次输入输出是什么、花了多少钱。

我们要做的，就是往 Langfuse 服务器发送数据。

### 2. Trace（追踪）

一次完整的用户会话，在 Langfuse 里叫做一个 **Trace**。

比如用户从打开应用到退出，整个对话过程就是一个 Trace。

**关键设计**：这个文件把 `trace_id` 设成 `session_id`。也就是说：

> 同一个用户、同一个会话里的多轮对话，都会合并到同一个 Trace 里。

为什么这样做？因为 Langfuse 的 `trace-create` 是 **upsert**（更新或插入）：相同 ID 第二次发送不会新建，而是更新原来的 Trace。

### 3. Span（跨度）

一个 Trace 里面有很多 **Span**，表示一段“做了某件事”的过程。

比如：

- 根 Span：整个 session
- Generation Span：一次大模型调用
- Tool Span：一次工具调用
- Task-complete Span：任务完成标记

每个 Span 都有：开始时间、结束时间、输入、输出、父节点。

### 4. Generation（生成）

在 Langfuse 里，**Generation** 特指一次大模型调用。

它和普通 Span 的区别是：Generation 会记录模型名、输入 messages、输出内容、token 使用量等。

### 5. Hook（钩子）

这个文件被放在 `shared_hooks/` 目录下，说明它是被“钩子机制”调用的。

什么叫钩子？

> 在 Agent 运行的某些关键节点，系统会“钩一下”，通知你：“嘿，现在发生了一件事，你要不要处理？”

比如：

- `before_turn_handler`：在一轮对话开始前被调用
- `before_llm_handler`：在调用大模型前被调用
- `before_tool_handler`：在调用工具前被调用
- `after_tool_handler`：在工具调用结束后被调用
- `after_turn_handler`：在一轮对话结束后被调用
- `task_complete_handler`：在任务完成时被调用
- `flush_and_close`：在整个会话结束时被调用

这些函数名本身就是“事件发生时机”的说明。

---

## 三、Trace 树长什么样？

先给你画一棵树，后面讲代码的时候你会反复回来看这张图。

```text
Trace (id = session_id)                        ← 一次完整会话
│
└── root span: session-{sid}                    ← 根 span，包裹整个会话
    │
    └── tool-agent_execution                    ← 整个 Crew 执行
        │
        ├── GENERATION: llm-call-1              ← 第 1 次大模型调用
        │   │
        │   └── TOOL span: tool-search          ← 这次调用触发了搜索工具
        │       └── TOOL span: tool-calc        ← 搜索里又嵌套调用了计算器
        │
        ├── GENERATION: llm-call-2              ← 第 2 次大模型调用
        │
        └── task-complete span                  ← 任务完成标记
```

**核心思想**：

- 同一个 `session_id` 的所有内容，都在同一棵 Trace 树下。
- 大模型调用（Generation）下面可以挂工具调用（Tool Span）。
- 工具调用可以嵌套（比如一个工具里又调用了另一个工具）。

---

## 四、文件开头的“基础设施”

### 4.1 环境变量开关

```python
_ENABLED = os.environ.get("TRACE_TO_LANGFUSE", "").lower() in ("1", "true")
```

这行代码的意思是：

> 只有环境变量 `TRACE_TO_LANGFUSE` 设置为 `1` 或 `true` 时，这个文件里的追踪功能才会真正工作。

如果没有设置，所有 handler 函数一进来就会 `return`，什么都不做。

**为什么要有开关？**

因为发数据到 Langfuse 需要联网、需要配置密钥。开发调试时你可能不想发，所以加个开关控制。

### 4.2 一堆 ContextVar

```python
_trace_id_var: ContextVar[str] = ContextVar("lf_trace_id", default="")
_session_id_var: ContextVar[str] = ContextVar("lf_session_id", default="")
_root_span_id_var: ContextVar[str] = ContextVar("lf_root_span_id", default="")
_gen_id_var: ContextVar[str] = ContextVar("lf_gen_id", default="")
_gen_count_var: ContextVar[int] = ContextVar("lf_gen_count", default=0)
_tool_count_var: ContextVar[int] = ContextVar("lf_tool_count", default=0)
_span_stack_var: ContextVar[tuple] = ContextVar("lf_span_stack", default=())
_closed_spans_var: ContextVar[dict] = ContextVar("lf_closed_spans", default={})
```

这里用到了 `ContextVar`。你可以把它理解成：

> **“线程安全的全局变量”**。

#### 为什么不用普通全局变量？

假设有 100 个用户同时和 Agent 聊天，如果用普通全局变量：

```python
trace_id = "user-A-的-trace-id"  # 用户 A 刚设置好
# 这时候用户 B 的线程也执行了
trace_id = "user-B-的-trace-id"  # 用户 A 的数据被覆盖了！
```

`ContextVar` 会为每个线程/上下文保存独立的一份数据，互不干扰。

#### 这些变量各自存什么？

| 变量名 | 存什么 | 作用 |
|--------|--------|------|
| `_trace_id_var` | 当前会话的 Trace ID | 让所有事件知道属于哪个 Trace |
| `_session_id_var` | 当前会话 ID | 记录会话 |
| `_root_span_id_var` | 根 Span 的 ID | 其他 Span 没父节点时就挂它下面 |
| `_gen_id_var` | 当前未关闭的 Generation ID | 知道现在在哪次大模型调用里 |
| `_gen_count_var` | 当前轮次 Generation 计数 | 生成 `llm-call-1`、`llm-call-2` 名字 |
| `_tool_count_var` | 当前轮次 Tool 计数 | 统计工具调用次数 |
| `_span_stack_var` | Span 栈（元组） | 管理工具调用的嵌套关系 |
| `_closed_spans_var` | 已关闭 Span 的映射 | 给后续 Generation 补 output 时查找用 |

### 4.3 批处理缓冲区

```python
_batch_buffer: list = []
_batch_lock = Lock()
```

每次生成一个 Langfuse 事件，不是立即发送，而是先放进 `_batch_buffer` 这个列表里。

等积累到一定数量，或者到了关键节点（比如一轮结束），再一次性批量发送。

**为什么批量发送？**

- 减少网络请求次数，性能更好。
- Langfuse SDK v4 的 `ingestion.batch()` 就是设计来批量接收的。

`_batch_lock` 是一把锁，防止多个线程同时往列表里 `append` 或清空导致数据错乱。

---

## 五、核心函数逐个讲

### 5.1 `_ensure_client()` —— 初始化 Langfuse 客户端

```python
def _ensure_client():
    global _client, _init_failed
    if _init_failed:
        return None
    if _client is None:
        # 读取环境变量
        public_key = os.environ.get("XIAOPAW_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = os.environ.get("XIAOPAW_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
        base_url   = os.environ.get("XIAOPAW_LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_BASE_URL")

        if not all([public_key, secret_key, base_url]):
            _init_failed = True
            logger.warning("langfuse disabled: missing env vars ...")
            return None

        _client = Langfuse(
            tracing_enabled=False,
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
        )
        atexit.register(_flush_batch)
    return _client
```

这个函数是“懒加载”：

> 第一次真正需要发数据时，才去初始化 Langfuse 客户端。

它做了几件事：

1. 如果之前初始化失败过，直接返回 `None`，不再尝试。
2. 从环境变量读取公钥、密钥、服务地址。
3. 如果缺少配置，标记失败并警告。
4. 创建 `Langfuse` 客户端，注意 `tracing_enabled=False`，因为我们要自己控制发送时机。
5. 用 `atexit.register(_flush_batch)` 注册：程序退出前自动 flush 剩余数据。

### 5.2 `_ensure_trace(ctx)` —— 确保 Trace 和根 Span 存在

```python
def _ensure_trace(ctx):
    if _trace_id_var.get(""):
        return _trace_id_var.get()

    client = _ensure_client()
    if client is None:
        return None

    trace_id = _get_trace_id(ctx)  # 机制一：trace_id = session_id
    if not trace_id:
        return None

    _trace_id_var.set(trace_id)
    _session_id_var.set(ctx.session_id)

    # 发送 trace-create 事件
    _enqueue(IngestionEvent_TraceCreate(...))

    root_id = _uid()
    _root_span_id_var.set(root_id)

    # 发送 span-create 事件，创建根 span
    _enqueue(IngestionEvent_SpanCreate(...))

    return trace_id
```

这个函数会被几乎每个 handler 调用。

它的逻辑是：

> 如果当前上下文已经有 Trace 了，直接返回；如果没有，就创建 Trace 和根 Span。

注意 `TraceBody(id=trace_id, ...)` 里的 `trace_id` 是 `session_id`，这就是**机制一**的核心。

### 5.3 `before_turn_handler(ctx)` —— 一轮对话开始前

```python
def before_turn_handler(ctx) -> None:
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    _gen_count_var.set(0)
    _gen_id_var.set("")
    _tool_count_var.set(0)
    _closed_spans_var.set({})

    user_message = ctx.metadata.get("user_message", "")
    trace_id = _trace_id_var.get("")
    if trace_id and user_message:
        _enqueue(IngestionEvent_TraceCreate(...input={"message": user_message}...))
```

每开始一轮新对话，都要“归零”：

- Generation 计数器清零
- 当前 Generation ID 清空
- Tool 计数器清零
- 已关闭 Span 映射清空

然后把用户的输入消息，作为 Trace 的 `input` 更新到 Langfuse。

### 5.4 `before_llm_handler(ctx)` —— 最复杂的函数

这是整个文件里最难的函数，因为它要解决一个核心问题：

> **系统没有 AFTER_LLM 事件，那 Generation 什么时候关闭？**

假设 Agent 调用了大模型，大模型返回结果。但是我们拿不到“大模型调用结束”这个明确的事件，只能等下次调用前，或者回合结束前，去“反推”上一次的结果。

所以 `before_llm_handler` 做两件事：

#### 第一步：关闭上一个 Generation

```python
prev_gen_id = _gen_id_var.get("")
if prev_gen_id:
    # 1. 从 prompt_messages 里提取最近几次 tool 结果
    tool_results = _extract_recent_tool_results(...)

    # 2. 把这些结果补到对应的 tool span 上，并关闭它们
    for entry in stack:
        ...
        _enqueue(IngestionEvent_SpanUpdate(...))

    # 3. 从 prompt_messages 里反推上一个 LLM 的输出
    gen_output = _extract_prev_llm_output(prompt_messages)

    # 4. 关闭上一个 generation
    _enqueue(IngestionEvent_GenerationUpdate(...))
    _gen_id_var.set("")
```

这里的关键是“反推”：

- 上一个 LLM 的输出 = 上一个 `assistant` 消息。
- 上一个 LLM 调用的工具结果 = 后面的 `tool` 消息。

#### 第二步：创建新的 Generation

```python
count = _gen_count_var.get(0) + 1
_gen_count_var.set(count)

gen_id = _uid()
_gen_id_var.set(gen_id)

_enqueue(IngestionEvent_GenerationCreate(...))
```

为这次 LLM 调用创建一个新的 Generation Span。

### 5.5 `before_tool_handler(ctx)` —— 工具调用开始前

```python
def before_tool_handler(ctx) -> None:
    if not _ENABLED:
        return
    _ensure_trace(ctx)

    span_id = _uid()

    _enqueue(IngestionEvent_SpanCreate(
        body=CreateSpanBody(
            id=span_id,
            trace_id=_trace_id_var.get(""),
            parent_observation_id=_get_tool_parent_id(),
            name=f"tool-{ctx.tool_name}",
            start_time=_now(),
            input=tool_input or None,
            ...
        )
    ))

    # 把新 span 压入栈顶
    old_stack = _span_stack_var.get(())
    _span_stack_var.set((*old_stack, (span_id, ctx.tool_name, ctx.turn_number, tool_input)))
```

工具调用开始时：

1. 创建一个 Tool Span。
2. 父节点是 `_get_tool_parent_id()`，优先是当前 Generation。
3. 把这个 Span 的信息压入 `_span_stack_var` 栈顶。

**为什么用元组栈而不是列表？**

因为 `copy_context()` 复制 ContextVar 时复制的是引用。如果栈是列表，子线程修改栈会影响主线程。元组不可变，每次追加都产生新元组，安全。

### 5.6 `after_tool_handler(ctx)` —— 工具调用结束后

```python
def after_tool_handler(ctx) -> None:
    # 1. 构造 output_body（工具输出、是否失败、是否被拒等）
    output_body = {...}

    # 2. 从 span 栈里找到匹配的 span，弹出来
    for i in range(len(stack) - 1, -1, -1):
        if (stack[i][1], stack[i][2]) == key:
            matched_span_id = stack.pop(i)[0]
            break

    # 3. 发送 span-update 关闭它
    if matched_span_id:
        _enqueue(IngestionEvent_SpanUpdate(...))
```

工具调用结束时：

1. 从栈顶往下找，找到工具名和轮次匹配的 Span。
2. 用工具输出更新这个 Span，并设置结束时间。
3. 如果栈里找不到（可能之前被自动关闭了），就兜底新建一个完整 Span。

### 5.7 `after_turn_handler(ctx)` —— 一轮对话结束

这个函数是“收尾”：

```python
def after_turn_handler(ctx) -> None:
    if ctx.metadata.get("is_intermediate", False):
        return

    _ensure_trace(ctx)

    # 1. 关闭最后一个 Generation
    gen_id = _gen_id_var.get("")
    if gen_id:
        _enqueue(IngestionEvent_GenerationUpdate(...))
        _gen_id_var.set("")

    # 2. 关闭栈里剩下的 span
    for entry in stack:
        _enqueue(IngestionEvent_SpanUpdate(...))
    _span_stack_var.set(())

    # 3. 更新整个 Trace 的 output 和 metadata
    if trace_id:
        _enqueue(IngestionEvent_TraceCreate(...))

    # 4. 更新根 span 的 output
    if root_id and output:
        _enqueue(IngestionEvent_SpanUpdate(...))

    # 5. 强制发送！机制五
    _flush_batch()
```

关键点：

- 中间 turn（`is_intermediate=True`）不触发，避免 step_callback 频繁调用导致重复 flush。
- 这一轮最后一个 Generation 要在这里关闭。
- 所有未关闭的 Span 都要兜底关闭。
- 最后调用 `_flush_batch()`，把缓冲区数据发送到 Langfuse。

**为什么要在 `sender.send(reply)` 之前 flush？**

因为用户看到回复的时候，希望 Langfuse 后台已经能看到这次完整的 trace 了。如果等用户看完再发，后台会延迟几秒才显示。

### 5.8 `task_complete_handler(ctx)` —— 任务完成

```python
def task_complete_handler(ctx) -> None:
    ...
    _enqueue(IngestionEvent_SpanCreate(
        body=CreateSpanBody(
            name="task-complete",
            start_time=_now(),
            end_time=_now(),
            input=task_desc or None,
            output=raw_output or None,
            ...
        )
    ))
```

任务完成时，创建一个特殊的 Span 做标记，方便在 Langfuse 里一眼看到“任务完成了”。

### 5.9 `subcrew_cleanup()` —— 子线程清理

```python
def subcrew_cleanup() -> None:
    gen_id = _gen_id_var.get("")
    if gen_id:
        _enqueue(IngestionEvent_GenerationUpdate(...))
        _gen_id_var.set("")

    for entry in stack:
        _enqueue(IngestionEvent_SpanUpdate(...))
    _span_stack_var.set(())

    _flush_batch()
```

当 Agent 里嵌套了 sub-crew（子 Crew），它可能在另一个线程里运行。这个函数在线程结束时被调用，负责：

1. 关闭 sub-crew 里遗留的 Generation。
2. 关闭 sub-crew 里遗留的 Span。
3. flush 子线程里累积的事件。

**注意**：这里只 close span，不把 `_gen_id_var` 等重置为空。因为 ContextVar 在子线程里和父线程共享，如果重置了会影响父线程。

### 5.10 `flush_and_close(ctx)` —— 最终清理

```python
def flush_and_close(ctx) -> None:
    # 1. 关闭所有孤儿 span
    # 2. 关闭所有遗留 generation
    # 3. 创建 session_end span
    # 4. 关闭根 span
    # 5. flush
    # 6. 重置所有 ContextVar
```

这是整个会话结束时最后执行的函数。它做的都是兜底工作：

- 把还没关的 span 强制关掉。
- 把还没关的 generation 强制关掉。
- 发送一个 `session_end` span。
- 关闭根 span。
- flush 所有数据。
- 清空所有 ContextVar，为下一个会话做准备。

---

## 六、五大机制总结

| 机制 | 代码位置 | 解决什么问题 |
|------|---------|-------------|
| 机制一：多轮对话同 trace | `_get_trace_id()` | 让同一 session 的多轮对话合并到同一棵 trace 树 |
| 机制二：sub-crew 自动挂父 trace | ContextVar + `copy_context()` | 子线程自动继承父线程的 trace 上下文 |
| 机制三：Span 栈维护嵌套关系 | `_span_stack_var`（元组栈） | 工具调用嵌套时，父子关系正确 |
| 机制四：Generation 先写后更新 | `before_llm_handler()` | 没有 AFTER_LLM 事件时，靠下次调用前反推关闭 |
| 机制五：强制 flush 保证可见性 | `after_turn_handler()` 末尾 `_flush_batch()` | 用户看到回复前，数据已发送到 Langfuse |

---

## 七、一个完整例子串起来

假设用户说：“帮我查一下北京天气，然后算一下 23+45”。

整个事件流如下：

### 1. `before_turn_handler`

- 创建 Trace（如果还没有）。
- 计数器归零。
- 把用户消息 “帮我查一下北京天气，然后算一下 23+45” 作为 Trace input。

### 2. `before_llm_handler`（第 1 次）

- 没有上一个 Generation，跳过关闭逻辑。
- 创建 Generation：`llm-call-1`。
- Agent 决定调用 `get_weather` 工具。

### 3. `before_tool_handler`

- 创建 Tool Span：`tool-get_weather`。
- 父节点是当前 Generation `llm-call-1`。
- 把 Span 压入栈。

### 4. `after_tool_handler`

- 工具返回结果：“北京今天晴，25°C”。
- 从栈里找到 `tool-get_weather`，更新 output 并关闭。
- Agent 决定调用 `calculator` 工具。

### 5. `before_tool_handler`

- 创建 Tool Span：`tool-calculator`。
- 父节点仍然是当前 Generation `llm-call-1`。
- 压入栈。

### 6. `after_tool_handler`

- 工具返回结果：“68”。
- 关闭 `tool-calculator`。

### 7. `before_llm_handler`（第 2 次）

- 发现上一个 Generation `llm-call-1` 还没关。
- 从 prompt_messages 里找到 tool 结果，补到对应 Span（已经补过了，这里可能没新内容）。
- 反推上一个 LLM 的输出是调用这两个工具。
- 关闭 `llm-call-1`。
- 创建新的 Generation：`llm-call-2`。

### 8. `after_turn_handler`

- 关闭最后一个 Generation `llm-call-2`，output 是最终回复。
- 关闭栈里剩余的 Span（如果有）。
- 更新 Trace output。
- `_flush_batch()` 发送所有事件到 Langfuse。

### 9. 用户看到回复：

> “北京今天晴，25°C；23+45=68。”

同时 Langfuse 后台已经能看到完整的 trace 树。

---

## 八、常见问题

### Q1：为什么 `_span_stack_var` 用元组而不是列表？

因为 ContextVar 在 `copy_context()` 时复制的是引用。列表是可变的，子线程改了会影响主线程。元组不可变，每次修改都产生新元组，安全。

### Q2：`_flush_batch()` 什么时候会执行？

三种情况：

1. `after_turn_handler` 末尾：每轮对话结束时。
2. `subcrew_cleanup()` 末尾：子线程结束时。
3. `flush_and_close()` 末尾：整个会话结束时。
4. 程序正常退出时：`atexit.register(_flush_batch)` 兜底。

### Q3：如果 Langfuse 配置错了会怎样？

`_ensure_client()` 会捕获异常，把 `_init_failed` 设为 `True`，后续所有发送都不会再尝试。程序主流程不受影响。

### Q4：`_batch_buffer` 里的数据会不会丢失？

如果在 `_flush_batch()` 发送前程序被 `kill -9` 强制终止，缓冲区里的数据会丢失。这是 atexit 的限制。正常退出、Ctrl+C、未捕获异常一般都能 flush。

---

## 九、总结

[`shared_hooks/langfuse_trace.py`](file:///d:/ProjectsCodes/企业级智能体实战/xiaopaw-v2/shared_hooks/langfuse_trace.py) 这个文件的核心任务可以概括为一句话：

> **把 Agent 的运行过程，按照时间顺序和父子嵌套关系，打包成 Langfuse 能看懂的事件流。**

它用到了几个关键技巧：

- `session_id` 当 `trace_id`，让多轮对话合并。
- `ContextVar` 保存每个线程自己的上下文。
- 不可变元组栈管理 Span 嵌套。
- “先写后更新”策略处理没有 AFTER_LLM 的问题。
- 批量缓冲 + 关键节点 flush，平衡性能和实时性。

建议你配合 Langfuse 后台界面一起看：每当你在代码里看到一个 `_enqueue(...)`，就对应后台树状图里的一个节点。

---

> 如果还有哪一行看不懂，或者想让我画一张更详细的流程图，随时告诉我。
