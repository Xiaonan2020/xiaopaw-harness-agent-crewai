"""Langfuse 全链路追踪 —— 把 5+2 Hook 事件翻译为 Langfuse trace 树。

【课程对应】
- L30《项目实战 3》：建立"看得见"的可观测层
- L33《项目实战 5》第五节"Trace 树：从事件到完整树形结构"——本文件 5 大机制的完整阐释

【Trace 树的目标层次结构】
    Trace (id = session_id)                ← 机制一：多轮对话同 trace
      └─ root span: session-{sid}
          └─ tool-agent_execution         ← 包裹整个 Crew 执行
              ├─ GENERATION（每次 LLM 调用）  ← 机制四：先写后更新
              │    └─ TOOL span（每次工具调用）← 机制三：span 栈管理父子
              └─ task-complete span

【五大机制对应代码位置】
机制一：多轮对话同棵树 → _get_trace_id()（trace_id = session_id）
机制二：Sub-crew 自动挂父 trace → ContextVar + copy_context() 自动传播（在 crew_adapter）
机制三：Span 栈维护嵌套关系 → _span_stack_var（不可变元组栈，LIFO）
机制四：Generation 先写后更新 → before_llm_handler() 处理上一个 gen 的 close
机制五：强制 flush 保证可见性 → after_turn_handler() 末尾调用 _flush_batch()

【为什么用 SDK v4 + ContextVar】
- SDK v4 的 ingestion.batch() 是显式批处理，便于精确控制 flush 时机
- ContextVar 而非全局变量：thread-safe + 子线程通过 copy_context() 自动继承
- 不可变元组栈：copy_context() 复制 ContextVar 时只复制引用，列表会被多线程共享出 bug
"""

import atexit  # 导入 atexit 模块，用于注册程序退出时自动执行的清理函数
import logging  # 导入 logging 模块，用于记录日志信息
import os  # 导入 os 模块，用于读取环境变量
import uuid  # 导入 uuid 模块，用于生成全局唯一标识符
from contextvars import ContextVar  # 从 contextvars 导入 ContextVar，实现线程安全的上下文变量
from datetime import datetime, timezone  # 从 datetime 导入时间相关类，用于生成 UTC 时间戳
from threading import Lock  # 从 threading 导入 Lock，用于多线程下保护批处理缓冲区

logger = logging.getLogger(__name__)  # 获取当前模块的日志记录器，日志名与模块路径一致

_ENABLED = os.environ.get("TRACE_TO_LANGFUSE", "").lower() in ("1", "true")  # 读取环境变量，判断是否开启 Langfuse 追踪（1/true 表示开启）

try:  # 尝试执行以下导入代码，兼容外部未提供 trace 模块的情况
    from xiaopaw.observability.trace import trace_id_var as _ext_trace_id_var  # 从外部 observability 模块导入已有的 trace_id ContextVar
except ImportError:  # 如果外部模块不存在或导入失败，则进入 except 分支
    _ext_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")  # 兜底创建外部 trace_id 上下文变量，默认值为 "-"

_client = None  # Langfuse SDK 客户端实例，初始为空，懒加载
_init_failed = False  # 标记 Langfuse 初始化是否失败，避免重复尝试
_batch_buffer: list = []  # 批处理事件缓冲区，暂存待发送到 Langfuse 的所有事件
_batch_lock = Lock()  # 保护 _batch_buffer 的线程锁，防止并发追加/清空导致数据错乱

_trace_id_var: ContextVar[str] = ContextVar("lf_trace_id", default="")  # 当前会话的 Langfuse trace_id 上下文变量
_session_id_var: ContextVar[str] = ContextVar("lf_session_id", default="")  # 当前会话 ID 的上下文变量
_root_span_id_var: ContextVar[str] = ContextVar("lf_root_span_id", default="")  # trace 根 span 的 ID 上下文变量
_gen_id_var: ContextVar[str] = ContextVar("lf_gen_id", default="")  # 当前正在进行的 LLM generation 的 ID
_gen_count_var: ContextVar[int] = ContextVar("lf_gen_count", default=0)  # 当前轮次中 LLM 调用次数计数器
_tool_count_var: ContextVar[int] = ContextVar("lf_tool_count", default=0)  # 当前轮次中工具调用次数计数器
_span_stack_var: ContextVar[tuple] = ContextVar("lf_span_stack", default=())  # span 嵌套栈，使用不可变元组保证线程安全
_closed_spans_var: ContextVar[dict] = ContextVar("lf_closed_spans", default={})  # 已自动关闭的 tool span 映射表，键为 (tool_name, turn_number)


def _ensure_client():
    global _client, _init_failed  # 声明使用模块级全局变量 _client 和 _init_failed
    if _init_failed:  # 如果之前初始化已经失败，直接返回 None，避免反复尝试
        return None  # 返回 None 表示没有可用客户端
    if _client is None:  # 如果客户端尚未创建，则执行初始化逻辑
        try:  # 尝试初始化 Langfuse 客户端
            from langfuse import Langfuse  # 从 langfuse 包导入 Langfuse 类

            public_key = (  # 读取公钥，优先使用项目专有环境变量
                os.environ.get("XIAOPAW_LANGFUSE_PUBLIC_KEY")  # 先尝试 XIAOPAW_ 前缀的公钥
                or os.environ.get("LANGFUSE_PUBLIC_KEY")  # 兜底使用通用 LANGFUSE_PUBLIC_KEY
            )
            secret_key = (  # 读取密钥，优先使用项目专有环境变量
                os.environ.get("XIAOPAW_LANGFUSE_SECRET_KEY")  # 先尝试 XIAOPAW_ 前缀的密钥
                or os.environ.get("LANGFUSE_SECRET_KEY")  # 兜底使用通用 LANGFUSE_SECRET_KEY
            )
            base_url = (  # 读取 Langfuse 服务地址，优先使用项目专有环境变量
                os.environ.get("XIAOPAW_LANGFUSE_BASE_URL")  # 先尝试 XIAOPAW_ 前缀的地址
                or os.environ.get("LANGFUSE_BASE_URL")  # 兜底使用通用 LANGFUSE_BASE_URL
            )

            if not all([public_key, secret_key, base_url]):  # 如果三个必要配置任一为缺失
                _init_failed = True  # 标记初始化失败
                logger.warning(  # 记录警告日志，提示用户缺少环境变量
                    "langfuse disabled: missing env vars "  # 日志第一部分：说明 Langfuse 被禁用
                    "(need XIAOPAW_LANGFUSE_PUBLIC_KEY + SECRET_KEY + BASE_URL)"  # 日志第二部分：列出所需变量名
                )
                return None  # 返回 None，表示无法创建客户端

            _client = Langfuse(  # 创建 Langfuse 客户端实例
                tracing_enabled=False,  # 关闭 SDK 自带的自动 tracing，我们使用显式 batch 接口
                public_key=public_key,  # 传入公钥
                secret_key=secret_key,  # 传入密钥
                base_url=base_url,  # 传入服务端地址
            )
            atexit.register(_flush_batch)  # 注册程序退出时自动 flush 未发送的事件
        except Exception:  # 捕获初始化过程中的任何异常
            _init_failed = True  # 标记初始化失败
            logger.warning("langfuse init failed (non-blocking)", exc_info=True)  # 记录非阻塞警告，附带异常堆栈
            return None  # 返回 None
    return _client  # 返回已创建或已存在的客户端实例


def _now():
    return datetime.now(timezone.utc)  # 返回当前 UTC 时间，用于所有事件时间戳


def _uid():
    return str(uuid.uuid4())  # 生成并返回一个字符串形式的 UUID，作为事件或观测对象的唯一 ID


def _enqueue(event):
    with _batch_lock:  # 获取缓冲区锁，保证线程安全
        _batch_buffer.append(event)  # 将事件追加到批处理缓冲区末尾


def _flush_batch():
    global _batch_buffer  # 声明使用模块级全局变量 _batch_buffer
    with _batch_lock:  # 获取缓冲区锁
        if not _batch_buffer:  # 如果缓冲区为空，没有事件需要发送
            return  # 直接返回，避免空请求
        batch = _batch_buffer[:]  # 复制当前缓冲区内容到本地变量
        _batch_buffer = []  # 清空全局缓冲区，释放已提交事件

    client = _ensure_client()  # 获取或初始化 Langfuse 客户端
    if client is None:  # 如果客户端不可用
        return  # 直接返回，事件已被清空但不会发送到服务端

    chunk_size = 50  # 每批最多发送 50 个事件，避免请求过大
    for i in range(0, len(batch), chunk_size):  # 按 chunk_size 切分批次
        chunk = batch[i : i + chunk_size]  # 取出当前切片的事件子集
        try:  # 尝试发送当前批次
            client.api.ingestion.batch(batch=chunk)  # 调用 Langfuse SDK v4 的 batch 接口发送事件
        except Exception:  # 发送失败时不影响主流程
            logger.debug("langfuse batch ingestion failed", exc_info=True)  # 记录调试日志，附带异常信息


def _get_trace_id(ctx) -> str:
    """★ 机制一：让多轮对话留在同一棵 trace 树。

    核心思想：trace_id = session_id（不用随机 UUID）。
    Langfuse 的 trace-create 是 upsert 操作 —— 相同 ID 第二次调用是更新，不是新建。
    所以 Turn 1 / Turn 2 / Turn N 都用同一个 session_id 作为 trace_id，
    在 Langfuse 里自动合并到一棵树。

    优先用外部注入的 trace_id（_ext_trace_id_var），兼容上游已有 tracing 的场景；
    没有就 fallback 到 session_id。
    """
    trace_id = _ext_trace_id_var.get("-")  # 从外部上下文变量获取 trace_id，拿不到时返回 "-"
    if trace_id == "-":  # 如果外部没有注入 trace_id
        trace_id = ctx.session_id  # 使用会话 ID 作为 trace_id
    return trace_id or ""  # 返回 trace_id，如果是空值则返回空字符串


def _extract_recent_tool_results(prompt_messages: list) -> list[tuple[str, str]]:
    """Extract tool results after the last assistant message in prompt_messages.

    Returns [(tool_name, content), ...] in chronological order.
    Preserves duplicates for positional matching against span stack.
    """
    results: list[tuple[str, str]] = []  # 初始化结果列表，存储 (工具名, 工具输出) 元组
    for msg in reversed(prompt_messages or []):  # 从消息列表末尾向前遍历
        if not isinstance(msg, dict):  # 如果消息不是字典格式，跳过
            continue  # 继续下一条消息
        role = msg.get("role", "")  # 获取消息角色，如 tool/assistant/user
        if role == "tool":  # 如果是工具返回结果
            name = msg.get("name", "")  # 获取工具名称
            if name:  # 如果工具名存在
                results.append((name, msg.get("content", "")))  # 追加 (工具名, 工具输出内容)
        elif role == "assistant":  # 如果遇到 assistant 消息，说明已经到达上一次 LLM 输出
            break  # 停止遍历，只取最近一次 assistant 之后的 tool 结果
    results.reverse()  # 由于是从后往前收集的，需要反转回时间顺序
    return results  # 返回工具结果列表


def _extract_prev_llm_output(prompt_messages: list) -> dict | None:
    """Extract the previous LLM call's output from the current prompt_messages.

    Walks backward through messages to find the last assistant message before
    the current tool-result block. Works with both OpenAI and Qwen message formats
    (Qwen tool results use tool_call_id instead of name).
    """
    if not prompt_messages:  # 如果消息列表为空
        return None  # 没有可提取的输出
    for msg in reversed(prompt_messages):  # 从消息列表末尾向前遍历
        if not isinstance(msg, dict):  # 跳过非字典消息
            continue  # 继续下一条
        role = msg.get("role", "")  # 获取消息角色
        if role == "tool":  # 跳过工具结果消息
            continue  # 继续向前找 assistant 消息
        if role == "assistant":  # 找到 assistant 消息
            tool_calls = msg.get("tool_calls")  # 获取工具调用列表
            content = msg.get("content")  # 获取文本内容
            if tool_calls and isinstance(tool_calls, list):  # 如果存在工具调用
                return {  # 返回工具调用形式的输出
                    "action": "tool_calls",  # 标记为工具调用
                    "tools": [  # 构建工具列表
                        {  # 每个工具对象
                            "name": tc.get("function", {}).get("name", ""),  # 提取工具名
                            "arguments": tc.get("function", {}).get("arguments", ""),  # 提取工具参数
                        }
                        for tc in tool_calls  # 遍历所有工具调用
                    ],
                }
            if content:  # 如果存在文本回复
                return {"reply": str(content)[:500]}  # 返回截断后的文本回复
            return None  # assistant 消息既没有工具调用也没有内容
        break  # 遇到非 tool 非 assistant 消息时停止
    return None  # 未找到任何 assistant 消息


def _get_tool_parent_id() -> str:
    """★ 机制三：tool span 的父节点。

    优先级：当前 generation > span 栈顶 > root span
    LLM 调用工具时，工具自然挂在那次 LLM 调用（generation）之下。
    嵌套调用时，外层 tool 在栈底，内层 tool 在栈顶 —— LIFO 天然匹配嵌套。
    """
    gen_id = _gen_id_var.get("")  # 获取当前正在进行的 generation ID
    if gen_id:  # 如果存在未关闭的 generation
        return gen_id  # 工具 span 挂在当前 generation 下
    stack = _span_stack_var.get(())  # 获取当前 span 栈
    if stack:  # 如果栈不为空
        return stack[-1][0]  # 返回栈顶 span 的 ID 作为父节点
    return _root_span_id_var.get("")  # 兜底返回根 span ID


def _get_gen_parent_id() -> str:
    """★ 机制三：generation span 的父节点 —— 不能挂在当前 gen 上。

    优先级：span 栈顶 > root span
    Generation 不能挂另一个 generation（违反 Langfuse 的 trace 模型）。
    如果在 sub-crew 子线程里，栈顶是父线程的 tool-skill_name span ——
    sub-crew 的 LLM 调用就会自动成为父 skill span 的子节点（机制二的关键）。
    """
    stack = _span_stack_var.get(())  # 获取当前 span 栈
    if stack:  # 如果栈不为空
        return stack[-1][0]  # 返回栈顶 span 的 ID
    return _root_span_id_var.get("")  # 否则返回根 span ID


def _ensure_trace(ctx):
    if _trace_id_var.get(""):  # 如果当前上下文已经有 trace_id
        return _trace_id_var.get()  # 直接返回已有的 trace_id

    client = _ensure_client()  # 确保 Langfuse 客户端已初始化
    if client is None:  # 如果客户端不可用
        return None  # 无法创建 trace

    trace_id = _get_trace_id(ctx)  # 根据机制一获取 trace_id
    if not trace_id:  # 如果 trace_id 为空
        return None  # 无法创建 trace

    _trace_id_var.set(trace_id)  # 将 trace_id 写入当前上下文
    _session_id_var.set(ctx.session_id)  # 将 session_id 写入当前上下文

    from langfuse.api import CreateSpanBody, TraceBody  # 延迟导入 trace 和 span 创建体类型
    from langfuse.api.ingestion.types import (  # 延迟导入 ingestion 事件类型
        IngestionEvent_SpanCreate,  # span 创建事件
        IngestionEvent_TraceCreate,  # trace 创建事件
    )

    _enqueue(  # 将 trace 创建事件加入发送队列
        IngestionEvent_TraceCreate(  # 构造 trace 创建事件
            id=_uid(),  # 为 ingestion 事件本身生成唯一 ID
            timestamp=_now().isoformat(),  # 记录事件发生时间（ISO 格式字符串）
            type="trace-create",  # 事件类型：创建 trace
            body=TraceBody(  # trace 主体
                id=trace_id,  # trace 的 ID，使用 session_id
                name=f"xiaopaw-session-{ctx.session_id}",  # trace 的显示名称
                session_id=ctx.session_id,  # 关联会话 ID
                metadata={"source": "xiaopaw-v2"},  # 附加来源元数据
            ),
        )
    )

    root_id = _uid()  # 为根 span 生成唯一 ID
    _root_span_id_var.set(root_id)  # 将根 span ID 写入上下文

    _enqueue(  # 将根 span 创建事件加入发送队列
        IngestionEvent_SpanCreate(  # 构造 span 创建事件
            id=_uid(),  # ingestion 事件 ID
            timestamp=_now().isoformat(),  # 时间戳
            type="span-create",  # 事件类型：创建 span
            body=CreateSpanBody(  # span 主体
                id=root_id,  # span 的 ID
                trace_id=trace_id,  # 所属 trace
                name=f"session-{ctx.session_id}",  # span 显示名称
                start_time=_now(),  # 开始时间
                metadata={"session_id": ctx.session_id, "source": "xiaopaw-v2"},  # 元数据
            ),
        )
    )

    return trace_id  # 返回创建或复用的 trace_id


def before_turn_handler(ctx) -> None:
    if not _ENABLED:  # 如果 Langfuse 追踪未启用
        return  # 直接返回，不执行任何操作
    _ensure_trace(ctx)  # 确保当前回合有 trace 和根 span

    _gen_count_var.set(0)  # 重置当前轮次的 generation 计数器为 0
    _gen_id_var.set("")  # 清空当前 generation ID，表示新一轮开始
    _tool_count_var.set(0)  # 重置当前轮次的 tool 计数器为 0
    _closed_spans_var.set({})  # 清空已关闭 span 的映射表

    user_message = ctx.metadata.get("user_message", "")  # 从上下文中获取用户本轮输入消息
    trace_id = _trace_id_var.get("")  # 获取当前 trace_id
    if trace_id and user_message:  # 如果 trace 存在且有用户消息
        from langfuse.api import TraceBody  # 延迟导入 TraceBody
        from langfuse.api.ingestion.types import IngestionEvent_TraceCreate  # 延迟导入事件类型

        _enqueue(  # 将 trace 更新事件加入队列
            IngestionEvent_TraceCreate(  # 构造 trace 创建/更新事件
                id=_uid(),  # 事件唯一 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="trace-create",  # 类型为 trace-create（相同 ID 会 upsert）
                body=TraceBody(  # trace 主体
                    id=trace_id,  # 使用已有 trace_id
                    input={"message": user_message},  # 将用户消息作为 trace 输入
                    user_id=ctx.sender_id or None,  # 设置用户 ID，如果没有则为 None
                ),
            )
        )


def before_llm_handler(ctx) -> None:
    """★ 机制四：Generation 先写后更新。

    系统里没有 AFTER_LLM 事件，所以 generation 的关闭分两个时机：
    1. 下一次 BEFORE_LLM（本函数顶部）：关闭上一个 gen，并补全期间发生的 tool span 的 output
    2. AFTER_TURN（after_turn_handler 末尾）：关闭本轮最后一个 gen

    【为什么"先写后更新"是必要的】
    LLM 调用开始时只知道 input；它什么时候结束、output 是什么，
    要等到 Agent 拿到工具结果再次调用 LLM 时，才能从 prompt_messages 里反推：
        - 上一个 LLM 的 output = 上一个 assistant message
        - 期间触发的 tool 调用结果 = 后续 tool messages
    本函数前半部分就是这个反推过程：扫描 span 栈 + 匹配 tool messages，
    补全 tool span 的 output 并 close 它，再 close 上一个 generation。
    """
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回
    _ensure_trace(ctx)  # 确保当前已有 trace

    prev_gen_id = _gen_id_var.get("")  # 获取上一个未关闭的 generation ID
    if prev_gen_id:  # 如果存在上一个 generation
        from langfuse.api import UpdateGenerationBody, UpdateSpanBody  # 延迟导入更新体类型
        from langfuse.api.ingestion.types import (  # 延迟导入更新事件类型
            IngestionEvent_GenerationUpdate,  # generation 更新事件
            IngestionEvent_SpanUpdate,  # span 更新事件
        )

        stack = list(_span_stack_var.get(()))  # 复制当前 span 栈到可变列表
        tool_results = _extract_recent_tool_results(  # 从 prompt 中提取最近一批工具结果
            ctx.metadata.get("prompt_messages", [])  # 传入消息列表
        )

        remaining_stack = []  # 存放本轮未匹配到结果的 span 条目
        closed_entries = []  # 存放本轮被关闭的 span 条目
        used_indices: set[int] = set()  # 记录已被匹配过的 tool_result 下标
        closed = dict(_closed_spans_var.get({}))  # 复制已关闭 span 映射表

        for entry in stack:  # 遍历 span 栈中的每个条目
            span_id, tool_name, turn_num = entry[0], entry[1], entry[2]  # 解包条目中的 span_id、工具名、轮次
            matched_content = None  # 初始化匹配到的工具输出内容
            for i, (rname, rcontent) in enumerate(tool_results):  # 遍历工具结果
                if i not in used_indices and rname == tool_name:  # 如果下标未使用且工具名匹配
                    matched_content = rcontent  # 记录匹配到的内容
                    used_indices.add(i)  # 标记该结果已被使用
                    break  # 找到一个即可，跳出内层循环

            if matched_content is not None:  # 如果成功匹配到工具输出
                span_output = {"result": matched_content}  # 构造 span 的输出体
                closed[(tool_name, turn_num)] = span_id  # 记录该 span 已关闭
                closed_entries.append(entry)  # 记录被关闭的条目
                _enqueue(  # 发送 span 更新事件
                    IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                        id=_uid(),  # 事件 ID
                        timestamp=_now().isoformat(),  # 时间戳
                        type="span-update",  # 事件类型：更新 span
                        body=UpdateSpanBody(  # span 更新体
                            id=span_id,  # 被更新的 span ID
                            output=span_output,  # 设置输出
                            end_time=_now(),  # 设置结束时间
                            metadata={"phase": "auto-closed-by-next-llm"},  # 标记关闭阶段
                        ),
                    )
                )
            else:  # 如果没有匹配到结果
                remaining_stack.append(entry)  # 保留在栈中等待后续处理

        _closed_spans_var.set(closed)  # 更新已关闭 span 映射表
        _span_stack_var.set(tuple(remaining_stack))  # 更新 span 栈为未匹配的条目

        # Extract the previous LLM's actual output from the message history.
        # This works for both tool-call patterns and direct text responses,
        # and handles Qwen format (no "name" on tool result messages).
        prompt_messages = ctx.metadata.get("prompt_messages", [])  # 获取当前 prompt 消息列表
        gen_output = _extract_prev_llm_output(prompt_messages)  # 从消息中反推上一个 LLM 的输出
        # Fallback: use closed span names if message-based extraction failed
        if not gen_output and closed_entries:  # 如果消息提取失败但有关闭的 span
            gen_output = {  # 使用被关闭的 span 信息构造输出
                "action": "tool_calls",  # 标记为工具调用
                "tools": [  # 构建工具列表
                    {"name": e[1], "input": e[3] if len(e) > 3 else {}}  # 提取工具名和输入
                    for e in closed_entries  # 遍历被关闭的条目
                ],
            }

        close_kwargs: dict = {"id": prev_gen_id, "end_time": _now()}  # 构造关闭 generation 的参数
        if gen_output:  # 如果有反推得到的输出
            close_kwargs["output"] = gen_output  # 加入输出字段
        _enqueue(  # 发送 generation 更新事件关闭上一个 generation
            IngestionEvent_GenerationUpdate(  # 构造 generation 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="generation-update",  # 事件类型：更新 generation
                body=UpdateGenerationBody(**close_kwargs),  # 使用关键字参数展开生成更新体
            )
        )
        _gen_id_var.set("")  # 清空当前 generation ID，表示已关闭

    count = _gen_count_var.get(0) + 1  # generation 计数加 1
    _gen_count_var.set(count)  # 更新计数器

    gen_id = _uid()  # 为新的 generation 生成唯一 ID
    _gen_id_var.set(gen_id)  # 将新 generation ID 写入上下文

    prompt_messages = ctx.metadata.get("prompt_messages", [])  # 获取 prompt 消息
    prompt_preview = ctx.metadata.get("prompt_preview", "")  # 获取 prompt 预览文本
    gen_input = None  # 初始化 generation 输入为 None
    if prompt_messages:  # 如果存在完整消息列表
        gen_input = {"messages": prompt_messages}  # 使用完整消息作为输入
    elif prompt_preview:  # 否则如果存在预览文本
        gen_input = {"prompt": prompt_preview}  # 使用预览文本作为输入

    model = ctx.metadata.get("model", "") or "qwen3-max"  # 获取模型名，默认为 qwen3-max

    from langfuse.api import CreateGenerationBody  # 延迟导入 generation 创建体
    from langfuse.api.ingestion.types import IngestionEvent_GenerationCreate  # 延迟导入事件类型

    _enqueue(  # 发送 generation 创建事件
        IngestionEvent_GenerationCreate(  # 构造 generation 创建事件
            id=_uid(),  # 事件 ID
            timestamp=_now().isoformat(),  # 时间戳
            type="generation-create",  # 事件类型：创建 generation
            body=CreateGenerationBody(  # generation 创建体
                id=gen_id,  # generation 的 ID
                trace_id=_trace_id_var.get(""),  # 所属 trace ID
                parent_observation_id=_get_gen_parent_id(),  # 父观测对象 ID
                name=f"llm-call-{count}",  # generation 名称，带计数
                model=model,  # 使用的模型名
                start_time=_now(),  # 开始时间
                input=gen_input,  # 输入内容
                metadata={  # 附加元数据
                    "agent_id": ctx.agent_id,  # 当前 agent ID
                    "turn": ctx.turn_number,  # 当前轮次
                    "call_number": count,  # 本次是第几次 LLM 调用
                },
            ),
        )
    )


def before_tool_handler(ctx) -> None:
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回
    _ensure_trace(ctx)  # 确保当前已有 trace

    tool_input = dict(ctx.tool_input) if ctx.tool_input else {}  # 将工具输入转为字典，空则使用空字典
    _tool_count_var.set(_tool_count_var.get(0) + 1)  # tool 计数加 1

    span_id = _uid()  # 为工具 span 生成唯一 ID

    from langfuse.api import CreateSpanBody  # 延迟导入 span 创建体
    from langfuse.api.ingestion.types import IngestionEvent_SpanCreate  # 延迟导入事件类型

    _enqueue(  # 发送工具 span 创建事件
        IngestionEvent_SpanCreate(  # 构造 span 创建事件
            id=_uid(),  # 事件 ID
            timestamp=_now().isoformat(),  # 时间戳
            type="span-create",  # 事件类型：创建 span
            body=CreateSpanBody(  # span 创建体
                id=span_id,  # span ID
                trace_id=_trace_id_var.get(""),  # 所属 trace ID
                parent_observation_id=_get_tool_parent_id(),  # 父观测对象 ID
                name=f"tool-{ctx.tool_name}",  # span 名称，包含工具名
                start_time=_now(),  # 开始时间
                input=tool_input or None,  # 工具输入，空则存 None
                metadata={  # 元数据
                    "tool_name": ctx.tool_name,  # 工具名
                    "turn": ctx.turn_number,  # 当前轮次
                    "phase": "attempt",  # 阶段：尝试执行
                },
            ),
        )
    )

    # ★ 机制三：把新 span 压入栈顶（不可变元组追加，确保 ContextVar 安全传播）
    # 栈元素：(span_id, tool_name, turn_number, tool_input) —— after_tool_handler 用前两项匹配
    # 用元组而不是 list：copy_context() 复制 ContextVar 时复制的是引用，
    # 列表会被多线程共享导致 sub-crew 改栈影响主线程；元组不可变，append 总是产生新元组
    old_stack = _span_stack_var.get(())  # 获取当前 span 栈
    _span_stack_var.set(  # 更新 span 栈
        (*old_stack, (span_id, ctx.tool_name, ctx.turn_number, tool_input))  # 展开旧栈并追加新条目
    )


def after_tool_handler(ctx) -> None:
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回

    tool_output = ctx.metadata.get("tool_output", "")  # 获取工具输出
    is_deny = ctx.metadata.get("guardrail_deny", False)  # 判断是否被 guardrail 拒绝
    level = "ERROR" if (not ctx.success or is_deny) else "DEFAULT"  # 失败或被拒则级别为 ERROR

    output_body: dict = {"success": ctx.success}  # 构造输出体，包含成功状态
    if tool_output:  # 如果存在工具输出
        output_body["result"] = tool_output  # 加入结果字段
    if is_deny:  # 如果被拒绝
        output_body["deny_reason"] = ctx.metadata.get("deny_reason", "")  # 加入拒绝原因
        output_body["deny_detail"] = ctx.metadata.get("deny_detail", "")  # 加入拒绝详情

    stack = list(_span_stack_var.get(()))  # 复制当前 span 栈到可变列表
    key = (ctx.tool_name, ctx.turn_number)  # 构造匹配键（工具名 + 轮次）
    matched_span_id = None  # 初始化匹配到的 span ID
    for i in range(len(stack) - 1, -1, -1):  # 从栈顶向下遍历
        if (stack[i][1], stack[i][2]) == key:  # 如果工具名和轮次匹配
            matched_span_id = stack.pop(i)[0]  # 弹出并记录该 span ID
            break  # 找到即停止
    _span_stack_var.set(tuple(stack))  # 更新 span 栈

    if not matched_span_id:  # 如果在栈中没有找到
        closed = dict(_closed_spans_var.get({}))  # 从已关闭映射表中查找
        matched_span_id = closed.pop(key, None)  # 尝试取出对应的 span ID
        if matched_span_id:  # 如果找到
            _closed_spans_var.set(closed)  # 更新已关闭映射表

    if matched_span_id:  # 如果匹配到 span ID
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        _enqueue(  # 发送 span 更新事件
            IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="span-update",  # 事件类型：更新 span
                body=UpdateSpanBody(  # span 更新体
                    id=matched_span_id,  # 被更新的 span ID
                    output=output_body,  # 设置输出
                    level=level,  # 设置级别
                    end_time=_now(),  # 设置结束时间
                    metadata={  # 元数据
                        "tool_name": ctx.tool_name,  # 工具名
                        "duration_ms": ctx.duration_ms,  # 执行耗时
                        "phase": "denied" if is_deny else "completed",  # 阶段：被拒或完成
                    },
                ),
            )
        )
    else:  # 如果没有匹配到 span（兜底逻辑）
        _ensure_trace(ctx)  # 确保 trace 存在
        tool_input = dict(ctx.tool_input) if ctx.tool_input else {}  # 工具输入
        span_id = _uid()  # 生成新的 span ID

        from langfuse.api import CreateSpanBody  # 延迟导入 span 创建体
        from langfuse.api.ingestion.types import IngestionEvent_SpanCreate  # 延迟导入事件类型

        _enqueue(  # 直接创建一个完整的 span（带起止时间）
            IngestionEvent_SpanCreate(  # 构造 span 创建事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="span-create",  # 事件类型：创建 span
                body=CreateSpanBody(  # span 创建体
                    id=span_id,  # span ID
                    trace_id=_trace_id_var.get(""),  # 所属 trace
                    parent_observation_id=_get_tool_parent_id(),  # 父观测对象
                    name=f"tool-{ctx.tool_name}",  # span 名称
                    start_time=_now(),  # 开始时间
                    end_time=_now(),  # 结束时间（立即结束）
                    input=tool_input or None,  # 工具输入
                    output=output_body,  # 工具输出
                    level=level,  # 级别
                    metadata={  # 元数据
                        "tool_name": ctx.tool_name,  # 工具名
                        "duration_ms": ctx.duration_ms,  # 耗时
                        "phase": "denied" if is_deny else "completed",  # 阶段
                    },
                ),
            )
        )


def after_turn_handler(ctx) -> None:
    """★ 机制四 + 机制五：关闭最后的 generation/span，强制 flush 到 Langfuse。

    本函数末尾的 _flush_batch() 是 L33 课文"机制五"的关键：
    必须在 sender.send(reply) 之前完成，保证用户拿到回复时 Langfuse 数据已就绪。

    is_intermediate=True 的 turn 是 step_callback 触发的中间 turn —— 不 flush，
    只有真正的轮次结束（通常是 task_callback 之后）才执行清理与 flush。
    """
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回

    if ctx.metadata.get("is_intermediate", False):  # 如果是中间 turn，不是真正轮次结束
        return  # 直接返回，不执行 flush

    _ensure_trace(ctx)  # 确保 trace 存在

    output = ctx.metadata.get("reply", "") or ctx.metadata.get("output", "")  # 获取本轮最终输出

    gen_id = _gen_id_var.get("")  # 获取当前未关闭的 generation ID
    if gen_id:  # 如果存在未关闭的 generation
        from langfuse.api import UpdateGenerationBody  # 延迟导入更新体
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate  # 延迟导入事件类型

        update_kwargs: dict = {"id": gen_id, "end_time": _now()}  # 构造更新参数
        if output:  # 如果有输出
            update_kwargs["output"] = output  # 加入输出
        if ctx.input_tokens or ctx.output_tokens:  # 如果存在 token 使用量
            from langfuse.api.commons.types.usage import Usage  # 延迟导入 Usage 类型

            update_kwargs["usage"] = Usage(  # 构造 Usage 对象
                input=ctx.input_tokens,  # 输入 token 数
                output=ctx.output_tokens,  # 输出 token 数
                total=ctx.input_tokens + ctx.output_tokens,  # 总 token 数
            )

        _enqueue(  # 发送 generation 更新事件
            IngestionEvent_GenerationUpdate(  # 构造 generation 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="generation-update",  # 事件类型：更新 generation
                body=UpdateGenerationBody(**update_kwargs),  # 展开参数构造更新体
            )
        )
        _gen_id_var.set("")  # 清空 generation ID

    stack = list(_span_stack_var.get(()))  # 复制当前 span 栈
    if stack:  # 如果栈中还有未关闭的 span
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        for entry in stack:  # 遍历栈中每个 span
            _enqueue(  # 发送 span 更新事件
                IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                    id=_uid(),  # 事件 ID
                    timestamp=_now().isoformat(),  # 时间戳
                    type="span-update",  # 事件类型：更新 span
                    body=UpdateSpanBody(  # span 更新体
                        id=entry[0],  # span ID
                        end_time=_now(),  # 结束时间
                        metadata={"phase": "auto-closed-by-after-turn"},  # 标记为 after_turn 自动关闭
                    ),
                )
            )
        _span_stack_var.set(())  # 清空 span 栈

    trace_id = _trace_id_var.get("")  # 获取当前 trace ID
    if trace_id:  # 如果 trace 存在
        from langfuse.api import TraceBody  # 延迟导入 TraceBody
        from langfuse.api.ingestion.types import IngestionEvent_TraceCreate  # 延迟导入事件类型

        meta: dict = {"source": "xiaopaw-v2"}  # 初始化 trace 元数据
        if ctx.duration_ms:  # 如果存在耗时
            meta["duration_ms"] = ctx.duration_ms  # 加入耗时
        if ctx.input_tokens or ctx.output_tokens:  # 如果存在 token 使用量
            meta["usage"] = {  # 构造 usage 元数据
                "input_tokens": ctx.input_tokens,  # 输入 token
                "output_tokens": ctx.output_tokens,  # 输出 token
                "total_tokens": ctx.input_tokens + ctx.output_tokens,  # 总 token
            }
        model = ctx.metadata.get("model", "")  # 获取模型名
        if model:  # 如果存在
            meta["model"] = model  # 加入模型名
        if ctx.metadata.get("guardrail_deny"):  # 如果被 guardrail 拒绝
            meta["guardrail_deny"] = True  # 标记拒绝
            meta["deny_reason"] = ctx.metadata.get("deny_reason", "")  # 加入拒绝原因

        body_kwargs: dict = {"id": trace_id, "metadata": meta}  # 构造 trace 更新参数
        if output:  # 如果有输出
            body_kwargs["output"] = {"reply": output}  # 加入输出

        _enqueue(  # 发送 trace 更新事件
            IngestionEvent_TraceCreate(  # 构造 trace 创建/更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="trace-create",  # 事件类型
                body=TraceBody(**body_kwargs),  # 展开参数构造 TraceBody
            )
        )

    root_id = _root_span_id_var.get("")  # 获取根 span ID
    if root_id and output:  # 如果根 span 存在且有输出
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        _enqueue(  # 发送根 span 更新事件
            IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="span-update",  # 事件类型
                body=UpdateSpanBody(id=root_id, output={"reply": output}),  # 更新根 span 输出
            )
        )

    _flush_batch()  # 强制将缓冲区事件发送到 Langfuse（机制五）


def task_complete_handler(ctx) -> None:
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回
    _ensure_trace(ctx)  # 确保 trace 存在

    task_desc = ctx.metadata.get("task_description", ctx.task_name)  # 获取任务描述，默认使用任务名
    raw_output = ctx.metadata.get("raw_output", "")  # 获取任务原始输出

    from langfuse.api import CreateSpanBody  # 延迟导入 span 创建体
    from langfuse.api.ingestion.types import IngestionEvent_SpanCreate  # 延迟导入事件类型

    stack = _span_stack_var.get(())  # 获取当前 span 栈
    root_id = _root_span_id_var.get("")  # 获取根 span ID
    parent_id = stack[-1][0] if stack else root_id  # 父节点优先取栈顶，否则取根 span

    _enqueue(  # 发送 task-complete span 创建事件
        IngestionEvent_SpanCreate(  # 构造 span 创建事件
            id=_uid(),  # ingestion 事件 ID
            timestamp=_now().isoformat(),  # 时间戳
            type="span-create",  # 事件类型
            body=CreateSpanBody(  # span 创建体
                id=_uid(),  # span 自身 ID
                trace_id=_trace_id_var.get(""),  # 所属 trace
                parent_observation_id=parent_id or None,  # 父观测对象
                name="task-complete",  # span 名称
                start_time=_now(),  # 开始时间
                end_time=_now(),  # 结束时间
                input=task_desc or None,  # 任务描述作为输入
                output=raw_output or None,  # 原始输出作为输出
                metadata={"agent": ctx.agent_id},  # 元数据包含 agent ID
            ),
        )
    )


def subcrew_cleanup() -> None:
    """★ 机制二：sub-crew 子线程结束时的清理。

    sub-crew 在 ThreadPoolExecutor 子线程里执行，结束时本函数被调用：
    1. 关闭 sub-crew 内遗留的 generation 和 span（避免 trace 树里的"幽儽节点"）
    2. flush 子线程 buffer 里累积的 Langfuse 事件

    【关键约束：不重置 ContextVar】
    虽然把 _gen_id_var 设成 "" 看似合理，但 ContextVar 是共享给父线程的引用 ——
    如果在子线程里 reset，会破坏父线程的上下文（让父 Crew 后续调用看不到自己的 gen）。
    所以这里只 close span 不重置 ContextVar，依赖父线程的 after_turn_handler 自然清理。
    """
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回

    gen_id = _gen_id_var.get("")  # 获取子线程中可能遗留的 generation ID
    if gen_id:  # 如果存在
        from langfuse.api import UpdateGenerationBody  # 延迟导入更新体
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate  # 延迟导入事件类型

        _enqueue(  # 发送 generation 更新事件关闭它
            IngestionEvent_GenerationUpdate(  # 构造 generation 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="generation-update",  # 事件类型
                body=UpdateGenerationBody(id=gen_id, end_time=_now()),  # 关闭 generation
            )
        )
        _gen_id_var.set("")  # 清空子线程中的 generation ID

    stack = _span_stack_var.get(())  # 获取子线程中的 span 栈
    if stack:  # 如果栈不为空
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        for entry in stack:  # 遍历栈中每个 span
            _enqueue(  # 发送 span 更新事件
                IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                    id=_uid(),  # 事件 ID
                    timestamp=_now().isoformat(),  # 时间戳
                    type="span-update",  # 事件类型
                    body=UpdateSpanBody(  # span 更新体
                        id=entry[0],  # span ID
                        end_time=_now(),  # 结束时间
                        metadata={"phase": "subcrew-cleanup"},  # 标记为 sub-crew 清理阶段
                    ),
                )
            )
        _span_stack_var.set(())  # 清空子线程 span 栈

    _flush_batch()  #  flush 子线程中累积的事件


def flush_and_close(ctx) -> None:
    """★ 机制五：SESSION_END 触发的最终 flush —— 由 hooks.yaml 挂在 SESSION_END。

    runner 在 finally 中调用 adapter.cleanup() → 触发 SESSION_END → 本函数：
    1. 关闭整个 session 仍未 close 的 span（兜底）
    2. 强制把 buffer 里的全部事件推送到 Langfuse
    3. 在 sender.send(reply) 之前完成 —— 用户看到回复时 trace 已可见
    """
    if not _ENABLED:  # 如果追踪未启用
        return  # 直接返回

    stack = _span_stack_var.get(())  # 获取当前 span 栈
    if stack:  # 如果还有未关闭的 span
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        for entry in stack:  # 遍历栈中每个 span
            _enqueue(  # 发送 span 更新事件
                IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                    id=_uid(),  # 事件 ID
                    timestamp=_now().isoformat(),  # 时间戳
                    type="span-update",  # 事件类型
                    body=UpdateSpanBody(  # span 更新体
                        id=entry[0],  # span ID
                        level="WARNING",  # 标记为警告级别，因为是孤儿 span
                        status_message="orphaned-span-auto-closed",  # 状态消息
                        end_time=_now(),  # 结束时间
                    ),
                )
            )
        _span_stack_var.set(())  # 清空 span 栈

    gen_id = _gen_id_var.get("")  # 获取可能遗留的 generation ID
    if gen_id:  # 如果存在
        from langfuse.api import UpdateGenerationBody  # 延迟导入更新体
        from langfuse.api.ingestion.types import IngestionEvent_GenerationUpdate  # 延迟导入事件类型

        _enqueue(  # 发送 generation 更新事件
            IngestionEvent_GenerationUpdate(  # 构造 generation 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="generation-update",  # 事件类型
                body=UpdateGenerationBody(id=gen_id, end_time=_now()),  # 关闭 generation
            )
        )
        _gen_id_var.set("")  # 清空 generation ID

    _ensure_trace(ctx)  # 确保 trace 存在
    trace_id = _trace_id_var.get("")  # 获取当前 trace ID
    root_id = _root_span_id_var.get("")  # 获取根 span ID

    if trace_id:  # 如果 trace 存在
        from langfuse.api import CreateSpanBody  # 延迟导入 span 创建体
        from langfuse.api.ingestion.types import IngestionEvent_SpanCreate  # 延迟导入事件类型

        _enqueue(  # 发送 session_end span 创建事件
            IngestionEvent_SpanCreate(  # 构造 span 创建事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="span-create",  # 事件类型
                body=CreateSpanBody(  # span 创建体
                    id=_uid(),  # span ID
                    trace_id=trace_id,  # 所属 trace
                    parent_observation_id=root_id or None,  # 父节点为根 span
                    name="session_end",  # span 名称
                    start_time=_now(),  # 开始时间
                    end_time=_now(),  # 结束时间
                    metadata={  # 元数据
                        "event": "session_end",  # 事件名
                        "session_id": ctx.session_id,  # 会话 ID
                    },
                ),
            )
        )

    if root_id:  # 如果根 span 存在
        from langfuse.api import UpdateSpanBody  # 延迟导入 span 更新体
        from langfuse.api.ingestion.types import IngestionEvent_SpanUpdate  # 延迟导入事件类型

        _enqueue(  # 发送根 span 更新事件
            IngestionEvent_SpanUpdate(  # 构造 span 更新事件
                id=_uid(),  # 事件 ID
                timestamp=_now().isoformat(),  # 时间戳
                type="span-update",  # 事件类型
                body=UpdateSpanBody(id=root_id, end_time=_now()),  # 关闭根 span
            )
        )

    _flush_batch()  # 强制 flush 所有事件到 Langfuse

    _trace_id_var.set("")  # 清空 trace ID
    _session_id_var.set("")  # 清空 session ID
    _root_span_id_var.set("")  # 清空根 span ID
    _gen_id_var.set("")  # 清空 generation ID
    _gen_count_var.set(0)  # 重置 generation 计数器
    _tool_count_var.set(0)  # 重置 tool 计数器
    _closed_spans_var.set({})  # 清空已关闭 span 映射表
