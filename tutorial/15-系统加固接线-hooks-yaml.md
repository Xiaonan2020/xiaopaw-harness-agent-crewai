# 15 - 系统加固接线 — hooks.yaml

## 本节学习目标

读完本节后，你将能够：

1. **理解什么是"声明式加固"** —— 为什么不用侵入式代码改动也能给系统装上 9 道防线
2. **看懂 hooks.yaml 的两段结构** —— 观测层和策略层为什么要分开写
3. **掌握三条顺序约束** —— 哪三处顺序错了会让系统瘫痪
4. **理解 Runner 的 4 个接线点** —— 业务代码 0 行修改背后的秘密
5. **类比理解**：把 hooks.yaml 想象成一张"装修图纸/电路接线图"，框架就是按图施工的电工师傅

---

## 一、L33 的核心：声明式加固

### 1.1 什么是"系统加固接线"？

前几篇我们学习了 9 个策略（观测层 2 个 + 安全层 3 个 + 可靠性层 3 个）。但这些策略怎么"装"到系统里？就像装修房子时，你已经设计好了电路图（加固策略），但怎么让电线真正通到每个房间？

```
传统方式（侵入式）：
  在 runner.py 的每个位置手动插入：
  if security_check:                       # 安全检查
      sandbox_guard.check(...)            # 调用沙箱守卫
  if permission_check:                    # 权限检查
      permission_gate.check(...)          # 调用权限网关
  → 代码膨胀，维护困难
  → 每加一个策略，就要改一次业务代码
  → 就像装修时每装一盏灯都要砸一次墙

声明式（hooks.yaml）：
  写一份 YAML 声明"哪个策略挂哪个事件"     # 像画一张电路图
  框架自动在事件点分发                      # 电工师傅按图接线
  → 业务代码 0 行修改
  → 加新策略只需要改 YAML，不动业务代码
  → 就像装修时换灯泡不用重新走线
```

**生活类比**：hooks.yaml 就像是装修的"电路接线图"。你不用关心墙壁里电线怎么走，只要在图纸上标明"客厅灯接开关 A、卧室灯接开关 B"，电工（框架）就会按图施工。换灯时不用动墙，改图就行。

### 1.2 33 课的成就

这一节（L33）是整个加固章节的高潮，关键成就是"业务代码 0 行修改"。让我们看看具体改动统计：

| 改动类型 | 改动位置 | 行数/处数 | 说明 |
|---------|---------|----------|------|
| 新增代码 | `shared_hooks/` 目录 | **699 行** | 9 个策略的实现代码 |
| 改动代码 | `runner.py` | **+4 行** | 4 个接线点（创建 adapter、pre-flight、捕获 deny、SESSION_END） |
| 改动代码 | `main_crew.py` | **+2 处** | step_callback 和 task_callback 接线 |
| 业务代码修改 | 业务逻辑 | **0 行** | 真正做到了零侵入 |
| 新建配置 | `hooks.yaml` | **72 行** | 声明 9 个策略 + 7 个事件 × 2 层 |

**效果**：9 个策略全部接线启动，覆盖安全 + 可靠性 + 可观测性。

**为什么 0 行业务修改这么重要？**
- 业务代码（如消息路由、Agent 编排）是系统的核心资产
- 每次改动都可能引入 bug，需要重新测试
- 0 修改意味着加固可以独立开发、独立测试、独立部署
- 就像给房子装监控，不动承重墙也能完成

---

## 二、hooks.yaml 完整解析

### 2.0 YAML 语法基础（小白先看这里）

如果你没接触过 YAML，先理解这 5 条基础：

| 语法 | 含义 | 例子 |
|------|------|------|
| `key: value` | 键值对（注意冒号后有空格） | `name: audit_logger` |
| `- item` | 列表项（注意缩进） | `- handler: structured_log` |
| `#` | 注释 | `# 这是说明` |
| `parent:` + 缩进 | 嵌套 | `hooks:` 下面再缩进写子项 |
| `${VAR}` | 环境变量引用 | `${FEISHU_APP_ID}` |

**YAML 与 JSON 的关系**：YAML 是 JSON 的超集，写法更简洁。`hooks:` 段就相当于 JSON 的 `{"hooks": {...}}`。

### 2.1 完整文件（逐行注释版）

```yaml
# shared_hooks/hooks.yaml
# XiaoPaw 的"装甲接线图"
# 这个文件是整个系统加固的"中央配置"，决定了 9 个策略挂在哪里、按什么顺序执行

# ══════════════════════════════════════════════════════
# 上半段：观测层（dispatch，fire-and-forget）
# "fire-and-forget" 意思是"触发就忘"，即只管记录不管结果
# 异常被吞掉，不影响业务 —— 因为日志失败不应该让用户请求也失败
# ══════════════════════════════════════════════════════
hooks:                      # 顶层关键字，开始定义观测层
  BEFORE_TURN:              # 事件名：每轮对话开始前触发
    - handler: structured_log.before_turn_handler   # 结构化日志：记录"对话开始"事件
    - handler: langfuse_trace.before_turn_handler   # Langfuse 追踪：开始本轮 trace span
  BEFORE_LLM:               # 事件名：每次调用 LLM 之前
    - handler: structured_log.before_llm_handler    # 记录 LLM 调用前的输入
    - handler: langfuse_trace.before_llm_handler    # 在 trace 中标记 LLM 调用
  BEFORE_TOOL_CALL:         # 事件名：每次工具调用之前
    - handler: structured_log.before_tool_handler   # 记录工具调用请求
    - handler: langfuse_trace.before_tool_handler   # 在 trace 中标记工具调用开始
  AFTER_TOOL_CALL:          # 事件名：每次工具调用之后
    - handler: structured_log.after_tool_handler    # 记录工具返回结果
    - handler: langfuse_trace.after_tool_handler    # 在 trace 中标记工具调用结束
  AFTER_TURN:               # 事件名：每轮对话结束后触发
    - handler: structured_log.after_turn_handler   # 记录"对话结束"事件和回复内容
    - handler: langfuse_trace.after_turn_handler   # 结束本轮 trace span
  TASK_COMPLETE:            # 事件名：任务完成时
    - handler: structured_log.task_complete_handler # 记录任务完成标志
    - handler: langfuse_trace.task_complete_handler # 在 trace 中标记任务完成
  SESSION_END:              # 事件名：会话结束时（整个会话彻底关闭）
    - handler: structured_log.session_end_handler   # 记录会话结束
    - handler: langfuse_trace.flush_and_close       # 把缓存的 trace 数据批量推送到 Langfuse 服务端

# ══════════════════════════════════════════════════════
# 下半段：策略层（dispatch_gate，可阻断）
# "dispatch_gate" 像保险丝，能熔断业务 —— GuardrailDeny 异常能穿透到上层
# 用来做安全、权限、成本控制等"硬约束"
# ══════════════════════════════════════════════════════
strategies:                 # 顶层关键字，开始定义策略层
  - name: audit_logger              # ★ 必须第一（被后面的 deps 引用）：审计日志器
    class: audit_logger.SecurityAuditLogger   # 实现类名（动态导入）
    config: {}                      # 该策略的配置参数（这里没有额外配置）
    hooks:                          # 该策略注册到哪些事件
      SESSION_END: session_end_handler   # 会话结束时调用，把整个会话的审计事件落盘

  - name: sandbox_guard             # 沙箱守卫，检测路径穿越/危险命令/注入
    class: sandbox_guard.SandboxGuard
    config: {}
    deps:                           # 依赖项：其他策略注入到这里
      audit: audit_logger           # 引用上面创建的 audit_logger，记 deny 事件
    hooks:
      BEFORE_TOOL_CALL: before_tool_handler   # 工具调用前检测，fail_closed=True

  - name: permission_gate           # 权限网关，检查用户能否调用某工具
    class: permission_gate.PermissionGate
    config: {}
    deps:
      audit: audit_logger           # 同样引用 audit_logger，保持审计一致
    hooks:
      BEFORE_TOOL_CALL: before_tool_handler   # 工具调用前检查权限

  - name: cost_guard                # 成本围栏，控制每会话花费
    class: cost_guard.CostGuard
    config:
      budget_usd: 1.0              # 单会话预算上限 1 美元
    hooks:
      AFTER_TURN: after_turn_handler       # 每轮结束算账
      BEFORE_TOOL_CALL: before_tool_handler # 工具调用前也检查预算

  - name: loop_detector             # 循环检测器，识别 LLM 反复调用同一工具
    class: loop_detector.LoopDetector
    config:
      threshold: 3                  # 同一工具连续调用 3 次以上算循环
    hooks:
      AFTER_TOOL_CALL: after_tool_handler  # 工具调用后统计
      AFTER_TURN: after_turn_handler       # 每轮结束后判断

  - name: retry_tracker             # 重试追踪器，统计工具调用失败重试次数
    class: retry_tracker.RetryTracker
    config:
      max_retries: 5                # 同一工具最多重试 5 次
    hooks:
      AFTER_TOOL_CALL: after_tool_handler  # 工具调用后记录结果（成功/失败）
```

### 2.2 两条执行链路（图解）

当 `BEFORE_TOOL_CALL` 事件触发时，框架会按"观测层先 → 策略层后"的顺序执行：

```
BEFORE_TOOL_CALL 事件触发时：

观测层（dispatch）           策略层（dispatch_gate）
─────────────                ──────────────
1. structured_log            3. sandbox_guard (fail_closed)
2. langfuse_trace            4. permission_gate (fail_closed)
                             5. cost_guard

执行顺序：1 → 2 → 3 → 4 → 5
                ↑           ↑
            先记录日志   再检查安全
            （留痕）   （决策是否放行）
```

**为什么观测层必须先于策略层？**

```
✅ 正确顺序（观测先）：
  如果 sandbox_guard deny 了：
    - 观测层已经记录了日志（步骤 1-2）
    - Langfuse 里能看到完整的调用链
    - 即使请求被拒绝，也有审计记录
    - 事后能查"为什么被拒绝、谁触发、输入是什么"

❌ 错误顺序（策略先）：
  如果观测层在策略层后面：
    - deny 后观测层不执行
    - 被拒绝的请求没有日志
    - 你只能看到"被拒了"，但查不到"被拒前的完整上下文"
    → 无法排查"为什么被拒绝了"
```

**生活类比**：就像机场安检。先让监控录像开着（观测层），再让安检员检查（策略层）。这样如果有人被拒绝登机，监控里能完整看到他从进门到被拒的全过程，方便事后查证。

---

## 三、三条执行顺序约束

这是本节最容易出错的地方。三条约束每一条都对应一种"违反了会怎样 → 具体错误现象 → 如何修复"的故障模式，必须牢记。

### 3.1 约束一：观测段必须整体先于策略段

```yaml
# 上半段（观测）必须完整出现在下半段（策略）之前
hooks:          # ← 上半段（先写）
  BEFORE_TOOL_CALL:
    - structured_log    # 1. 先记日志
    - langfuse_trace    # 2. 再记 trace

strategies:     # ← 下半段（后写）
  - sandbox_guard       # 3. 然后才检查安全
  - permission_gate     # 4. 然后检查权限
```

**代码强制**：

```python
# hook_framework/loader.py
def load_two_layers(self, ...):
    # ★ 先加载 hooks 段（观测层）
    # 这一步把所有 structured_log、langfuse_trace 等 handler 注册到 registry
    self._load_hooks_section(config.get("hooks", {}))

    # ★ 后加载 strategies 段（策略层）
    # 这一步把 sandbox_guard、permission_gate 等 handler 注册到 registry
    # 顺序由代码保证，YAML 里的位置不影响（即使你把 strategies 写在前面也无效）
    self._load_strategies_section(config.get("strategies", []), ...)
```

| 违反方式 | 具体错误现象 | 如何修复 |
|---------|-------------|---------|
| 把 strategies 写在 hooks 之前 | 代码层面强制保证顺序，所以 YAML 里乱写也不会错（loader 写死顺序） | 但为了可读性，请保持 hooks 在上、strategies 在下的惯例 |
| 观测层和策略层 handler 互相依赖 | 如果观测层依赖策略层结果，会循环依赖 | 观测层设计上不能依赖策略层结果，只做记录 |

### 3.2 约束二：audit_logger 必须排第一

```yaml
strategies:
  - name: audit_logger      # ← 必须第一个！
    # ...

  - name: sandbox_guard      # 引用 audit_logger
    deps:
      audit: audit_logger    # ← 如果 audit_logger 还没创建，这里会是 None

  - name: permission_gate
    deps:
      audit: audit_logger    # ← 同上
```

**不按顺序的后果**：

```python
# 假设顺序是 sandbox_guard → audit_logger（错误顺序）
# 加载 sandbox_guard 时：
#   sandbox_guard.__init__(audit=None)  ← audit_logger 还没创建，注入 None
#
# 运行时（用户发消息触发工具调用）：
#   sandbox_guard 检测到违规，调用 self._audit.record_event(...)
#   → AttributeError: 'NoneType' object has no attribute 'record_event'
#   → 因为 sandbox_guard 配置了 fail_closed=True
#   → 异常被转成 GuardrailDeny
#   → 所有请求被 deny
#   → 系统完全瘫痪！用户任何消息都收不到回复
```

| 违反方式 | 具体错误现象 | 如何修复 |
|---------|-------------|---------|
| 把 sandbox_guard 写在 audit_logger 之前 | 启动时不报错，但运行时所有请求被 deny，日志显示 `AttributeError: 'NoneType' has no attribute 'record_event'` | 调整 YAML 顺序，把 audit_logger 移到 strategies 列表第一个 |
| 拼错 audit_logger 名字（如写成 auditloger） | deps 引用不到，注入 None，运行时同样崩溃 | 仔细核对 name 字段拼写 |

### 3.3 约束三：cost_guard 必须先于 loop_detector

```yaml
strategies:
  - name: cost_guard          # ← AFTER_TURN 先算账
    hooks:
      AFTER_TURN: after_turn_handler

  - name: loop_detector       # ← AFTER_TURN 再检测
    hooks:
      AFTER_TURN: after_turn_handler
```

**不按顺序的后果**：

```
场景：LLM 反复调用同一工具（典型循环）
  每次调用都消耗 token（成本）

❌ 如果 loop_detector 先执行：
  → loop_detector 检测到循环，deny
  → 链路中止
  → cost_guard 的 AFTER_TURN 不执行（因为异常已经抛出）
  → 本次 token 成本没被计入
  → 预算统计偏低
  → 你以为只花了 0.5 美元，实际花了 2 美元
  → 看不出循环场景的高消耗，无法及时告警

✅ 如果 cost_guard 先执行：
  → cost_guard 先算账（记录本次成本）
  → loop_detector 再检测
  → 如果 deny，cost_guard 已经记过账了
  → 预算统计准确
  → 你能在 Langfuse 看到完整成本曲线
```

| 违反方式 | 具体错误现象 | 如何修复 |
|---------|-------------|---------|
| loop_detector 写在 cost_guard 之前 | 预算统计偏低，循环场景的成本"消失"，可能超支但不知道 | 把 cost_guard 移到 loop_detector 之前 |

---

## 四、Runner 的 4 个接线点

Runner 是消息处理的核心入口。要让加固策略"接到"业务流程上，需要在 4 个关键位置插入接线代码。每个接线点都有它的"位置 → 做什么 → 为什么在这里"。

### 4.1 接线点 1：创建 Adapter

```python
# xiaopaw/runner.py 的 _handle 方法中

# ★ 接线点 1：为本次请求创建 Hook adapter
# 位置：_handle 方法开头，从队列取到消息之后
# 做什么：把"全局 hook_registry"和"当前会话 ID"绑定，得到一个 adapter
# 为什么在这里：每条消息都要有独立的会话上下文，adapter 是 per-request 的
if self._hook_registry:                                    # 如果系统配置了 hook 框架
    adapter = CrewObservabilityAdapter(                    # 创建适配器
        registry=self._hook_registry,                      # 注入全局注册中心
        session_id=session.id,                             # 绑定当前会话 ID
    )
```

| 参数 | 类型 | 含义 |
|------|------|------|
| `registry` | `HookRegistry` | 全局 hook 注册中心，所有 handler 都在里面 |
| `session_id` | `str` | 当前消息所属会话 ID，用于关联 trace |

### 4.2 接线点 2：pre-flight 安全检查

```python
# ★ 接线点 2：pre-flight 安全检查
# 位置：创建 adapter 之后、调用 LLM 之前
# 做什么：把"用户输入的前 500 字"当作虚拟工具调用，先过一遍安全检查
# 为什么在这里：避免恶意请求直接进入 LLM（哪怕 LLM 拒绝，token 已经消耗）
if adapter:                                                # 如果有 adapter
    adapter.on_before_tool_call(                           # 触发 BEFORE_TOOL_CALL 事件
        tool_name="agent_execution",                       # 虚拟工具名："agent 执行"
        tool_input={"content": inbound.content[:500]},    # 取前 500 字（节省内存）
    )
    if adapter._pending_deny:                              # 如果触发了 deny
        pending = adapter._pending_deny                    # 取出待处理异常
        adapter._pending_deny = None                       # 清空缓存
        raise pending                                     # 抛给 except GuardrailDeny 捕获
```

### 4.3 接线点 3：捕获 GuardrailDeny

```python
# ★ 接线点 3：兜底捕获 GuardrailDeny
# 位置：try/except 块，包裹 LLM 调用
# 做什么：拦截所有策略层抛出的 deny，转成友好提示发给用户
# 为什么在这里：业务代码可能不知道 deny 的存在，统一在这里兜底
except GuardrailDeny as deny:                              # 捕获 deny 异常
    elapsed = time.monotonic() - start                     # 计算耗时
    logger.warning("guardrail deny for %s: %s", key, deny) # 记录到应用日志
    deny_reply = f"安全策略拦截：{deny.detail or deny.reason_code}"  # 拼接用户提示

    # 记录到 AFTER_TURN（让观测层知道这次"轮次"结束）
    if adapter and self._hook_registry:
        self._hook_registry.dispatch(                      # 用 dispatch（不阻断）
            EventType.AFTER_TURN,                          # 事件类型：轮次结束
            HookContext(                                  # 构造上下文
                event_type=EventType.AFTER_TURN,
                session_id=adapter._session_id,
                metadata={                                # 附加元数据
                    "reply": deny_reply,                  # 用户收到的回复
                    "guardrail_deny": True,               # 标记本次是 deny
                    "deny_reason": deny.reason_code,      # deny 原因
                },
            ),
        )

    # 发送拦截提示给用户
    await self._sender.send_text(key, deny_reply)          # 通过飞书发消息
```

### 4.4 接线点 4：SESSION_END

```python
# ★ 接线点 4：finally 触发 SESSION_END
# 位置：try/except/finally 块的最末尾
# 做什么：无论成功失败，都触发会话结束事件
# 为什么在这里：finally 保证一定执行，避免资源泄漏（如 Langfuse 连接没关）
finally:
    if adapter:                                            # 如果有 adapter
        try:
            adapter.cleanup()                              # 触发 SESSION_END → 审计落盘 + flush Langfuse
        except GuardrailDeny:
            pass  # cleanup 也可能 deny，但用户已收到回复，吞掉即可
    bind_trace_id("-")                                     # 重置 trace_id（清空当前线程绑定）
```

### 4.5 接线点总览表

| 接线点 | 位置 | 做什么 | 为什么在这里 |
|-------|------|--------|------------|
| 1. 创建 adapter | `_handle` 开头 | 绑定 registry 和 session_id | 每个请求都要独立上下文 |
| 2. pre-flight 检查 | LLM 调用前 | 提前过安全检查 | 避免恶意请求消耗 token |
| 3. 捕获 deny | try/except 块 | 兜底处理拦截 | 业务代码无感知 |
| 4. SESSION_END | finally 块 | 触发会话结束事件 | 保证资源清理 |

---

## 五、MainCrew 的 2 处接线

除了 Runner 的 4 处，CrewAI 的 Agent 层还需要 2 处接线，用于让 CrewAI 把"每一步执行"通知给 hook 框架。

### 5.1 step_callback 接线

```python
# xiaopaw/agents/main_crew.py 的 crew() 方法中

@crew
def crew(self) -> Crew:
    adapter = get_current_adapter()                       # 从上下文获取当前 adapter
    return Crew(
        agents=self.agents,                              # Agent 列表
        tasks=self.tasks,                                # Task 列表
        process=Process.sequential,                      # 顺序执行模式
        verbose=self._verbose,                           # 详细日志开关
        # ★ 接线 1：step_callback
        # CrewAI 每执行完一步就调用这个回调
        # 用来记录"Agent 在思考什么、调用了什么工具"
        step_callback=self._step_callback,
        # ★ 接线 2：task_callback
        # 每个 Task 完成时调用
        # 用来记录"任务输出"，并刷新 Langfuse trace
        task_callback=adapter.make_task_callback() if adapter else None,
    )
```

### 5.2 接线总览（完整改动统计图）

```
接线点汇总（完整改动统计）：
┌────────────────────────────────────────────────────────┐
│  runner.py                                             │
│    +4 行：                                              │
│    1. 创建 adapter（接线点 1）                          │
│    2. pre-flight 安全检查（接线点 2）                   │
│    3. except GuardrailDeny（接线点 3）                 │
│    4. finally SESSION_END（接线点 4）                   │
├────────────────────────────────────────────────────────┤
│  main_crew.py                                          │
│    +2 处：                                              │
│    1. step_callback = self._step_callback              │
│    2. task_callback = adapter.make_task_callback()     │
├────────────────────────────────────────────────────────┤
│  hooks.yaml                                            │
│    新建：72 行                                         │
│    声明 9 个策略 + 7 个事件 × 2 层                     │
├────────────────────────────────────────────────────────┤
│  shared_hooks/                                         │
│    新增：699 行                                        │
│    9 个策略实现                                        │
├────────────────────────────────────────────────────────┤
│  业务代码修改：0 行                                    │
└────────────────────────────────────────────────────────┘
```

---

## 六、加载流程

### 6.1 启动时加载

```python
# xiaopaw/main.py 中

# 创建 Hook 注册中心（一个全局对象，所有 handler 注册到这里）
hook_registry = HookRegistry()

# 创建加载器（专门负责读取 YAML、实例化策略类、注册 handler）
hook_loader = HookLoader(hook_registry)

# 加载 hooks.yaml（两段式）
shared_hooks_dir = Path(__file__).parent.parent / "shared_hooks"  # 找到 shared_hooks 目录
fail_closed = {"sandbox_guard", "permission_gate"}  # 这两个策略 fail_closed=True
hook_loader.load_two_layers(
    global_dir=shared_hooks_dir,                  # 全局策略目录
    workspace_dir=workspace_dir,                  # 工作区目录（可覆盖全局配置）
    fail_closed_names=fail_closed,                # 标记 fail_closed 的策略名
)

# 打印加载结果（启动时可见所有 handler，便于审计）
logger.info("hook framework loaded: %s", hook_registry.summary())
```

#### HookLoader.load_two_layers 参数表

| 参数 | 类型 | 含义 | 示例 |
|------|------|------|------|
| `global_dir` | `Path` | 全局策略实现所在目录 | `shared_hooks/` |
| `workspace_dir` | `Path` | 工作区目录，可放覆盖配置 | `workspace/` |
| `fail_closed_names` | `set[str]` | 哪些策略名 fail_closed=True | `{"sandbox_guard", "permission_gate"}` |

### 6.2 加载输出（控制台实际打印）

启动后控制台会输出以下内容，你可以对照检查加载是否正确：

```
hook framework loaded: {
    'before_turn': ['structured_log.before_turn_handler', 'langfuse_trace.before_turn_handler'],
    'before_llm': ['structured_log.before_llm_handler', 'langfuse_trace.before_llm_handler'],
    'before_tool_call': [
        'structured_log.before_tool_handler',      # 观测层 1
        'langfuse_trace.before_tool_handler',      # 观测层 2
        'sandbox_guard.before_tool_handler',       # 策略层 3（fail_closed）
        'permission_gate.before_tool_handler',     # 策略层 4（fail_closed）
        'cost_guard.before_tool_handler'            # 策略层 5
    ],
    'after_tool_call': [
        'structured_log.after_tool_handler',
        'langfuse_trace.after_tool_handler',
        'loop_detector.after_tool_handler',
        'retry_tracker.after_tool_handler'
    ],
    'after_turn': [
        'structured_log.after_turn_handler',
        'langfuse_trace.after_turn_handler',
        'cost_guard.after_turn_handler',            # ★ cost_guard 在前
        'loop_detector.after_turn_handler'          # ★ loop_detector 在后
    ],
    'session_end': [
        'structured_log.session_end_handler',
        'langfuse_trace.flush_and_close',
        'audit_logger.session_end_handler'         # ★ audit_logger 始终在策略层
    ]
}
```

**如何验证加载正确**：
1. `before_tool_call` 列表前两项必须是 `structured_log` 和 `langfuse_trace`（观测层先）
2. `after_turn` 列表里 `cost_guard` 必须在 `loop_detector` 前面
3. `session_end` 列表必须有 `audit_logger.session_end_handler`

---

## 七、设计优势与局限性

### 优势

1. **零侵入**：业务代码 0 行修改。新增策略只改 YAML + 实现 handler，业务代码完全不动。
2. **声明式**：72 行 YAML 描述整个加固策略，一目了然。改一处 YAML 就能调整全局行为。
3. **可审计**：加载时打印所有 handler，启动即可见。运维人员能直接看到当前生效了哪些策略。
4. **可扩展**：新增策略只需"加 YAML + 实现 handler"两步，不需要改框架核心。

### 局限性

1. **顺序敏感**：YAML 声明顺序就是执行顺序，配错会出严重问题（如系统瘫痪）。
2. **调试困难**：多 handler 链路出错时定位较难，需要看 Langfuse trace 才能跟踪。
3. **无静态检查**：YAML 里的类名/方法名拼错，运行时才发现（启动时才报错）。

---

## 八、验证你的理解

- [ ] hooks.yaml 分为哪两段？各用什么分发机制？（dispatch vs dispatch_gate）
- [ ] 三条执行顺序约束分别是什么？不遵守会怎样？
- [ ] Runner 的 4 个接线点分别在哪里？各做什么？
- [ ] MainCrew 的 2 处接线是什么？
- [ ] 33 课的改动统计是什么？（新增 699 行，改动 6 行，业务代码 0 行）
- [ ] 用一句话解释"L33 改了业务代码 0 行，靠什么实现的？"（靠声明式 YAML + 框架自动分发）

---

## ❓ 常见问题

### Q1：YAML 缩进错了会怎样？
**A**：YAML 对缩进敏感。如果缩进错了，启动时会报 `yaml.scanner.ScannerError`，比如：
```
yaml.scanner.ScannerError: mapping values are not allowed here
```
**修复**：检查每层缩进是否统一为 2 个空格（不要用 Tab），冒号后必须有一个空格。

### Q2：类名拼错了会怎样？
**A**：例如把 `class: sandbox_guard.SandboxGuard` 写成 `class: sandboxguard.SandboxGuard`（少了下划线），启动时会报：
```
ModuleNotFoundError: No module named 'sandboxguard'
```
**修复**：核对类名和模块名，确保和文件名/类名一致。

### Q3：为什么我的策略没生效？
**A**：可能原因：
1. `hooks.yaml` 路径不对（loader 找不到文件）
2. 事件名拼错（如 `BEFORE_TOOL` 应该是 `BEFORE_TOOL_CALL`）
3. handler 方法名拼错（如 `before_tool` 应该是 `before_tool_handler`）
**排查**：看启动时打印的 `hook framework loaded: {...}` 输出，对照检查是否包含你的 handler。

### Q4：fail_closed 怎么配置？
**A**：`fail_closed` 不是写在 YAML 里，而是在 `main.py` 加载时通过 `fail_closed_names` 参数传入：
```python
fail_closed = {"sandbox_guard", "permission_gate"}
hook_loader.load_two_layers(..., fail_closed_names=fail_closed)
```
只有安全相关的策略才需要 fail_closed，其他策略 fail_open（即崩溃时只记录不阻断）。

### Q5：能不能在运行时动态加 handler？
**A**：可以，但需要通过 `registry.register(event_type, handler)` 直接调用，不经过 YAML。一般用于测试或临时调试。生产环境不建议动态加，因为重启会丢失。

### Q6：观测层 handler 崩溃了会怎样？
**A**：观测层用 `dispatch`（不阻断），handler 异常被框架吞掉，记录一条 warning 日志，不影响业务。这是设计上的取舍：宁可丢日志，也不能让日志故障影响用户请求。

### Q7：策略层 deny 了，观测层还会执行吗？
**A**：同一次事件触发时，观测层在策略层之前已经执行完了，所以观测层一定执行。但 deny 之后，**后续事件**的观测层是否执行取决于业务代码的处理逻辑。例如 `BEFORE_TOOL_CALL` deny 后，`AFTER_TOOL_CALL` 不会触发（因为工具没被调用）。

---

## 🔧 调试技巧

### 技巧 1：看启动输出验证加载

启动 XiaoPaw 后第一时间看日志里的 `hook framework loaded: {...}`，确认每个事件下的 handler 顺序是否符合预期。

### 技巧 2：用单元测试验证链路

```bash
# 运行 hook 框架测试，验证 dispatch 顺序
pytest tests/unit/hook_framework/ -v

# 验证两层配置正确
pytest tests/integration/test_two_layer_config.py -v
```

### 技巧 3：在 handler 里加临时日志

如果怀疑某个 handler 没被调用，临时在 handler 第一行加：
```python
def before_tool_handler(self, ctx):
    print(f"[DEBUG] sandbox_guard called for {ctx.tool_name}")  # 临时调试
    ...
```
**注意**：调试完一定要删掉，避免污染生产日志。

### 技巧 4：用 Langfuse trace 排查

在 Langfuse 控制台打开一个 trace，看每个 span 的顺序和耗时。如果发现某个 handler 缺失，对照本节的"加载输出"检查。

### 技巧 5：YAML 语法校验

修改 hooks.yaml 后，先本地校验语法：
```bash
python -c "import yaml; yaml.safe_load(open('shared_hooks/hooks.yaml'))"
```
没报错再启动服务。

---

> 下一篇：[16-测试体系设计](./16-测试体系设计.md)
