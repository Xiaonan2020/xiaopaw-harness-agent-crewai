# 13 - 可靠性策略 — Cost/Loop/Retry

## 本节学习目标

读完本节后，你将能够：

1. 理解 LLM Agent 的三种"失控"模式及其危害
2. 掌握 CostGuard 的成本计算公式与预算拦截逻辑
3. 理解 LoopDetector 的 MD5 哈希去重原理
4. 区分三个策略的"可阻断"vs"纯观测"特性
5. 解释为什么三个策略的执行顺序很重要
6. 配置一个完整的可靠性策略链路

> 类比提示：
> - cost_guard 像"预付费手机卡"——余额不足就停机
> - loop_detector 像"走迷宫时检测原地打转"——连续 3 次重复就喊停
> - retry_tracker 像"火灾警报器"——只响警报不灭火，让人工判断

---

## 一、可靠性策略概述

### 1.1 为什么需要可靠性策略？

LLM Agent 和传统软件最大的不同：**LLM 的行为是非确定性的**。同样的输入，LLM 可能做出完全不同的决策。这带来三种"失控"模式：

```
模式 1：成本爆炸（Cost Explosion）
  ─────────────────────────────────
  没有防护：
    LLM 反复调用工具，每次都消耗 token
    一个用户请求可能花掉 $10
    一天上百个用户 → $1000+ 账单
    
  实际场景：
    用户："帮我搜索 Python 新特性"
    LLM 思考："搜索没结果，再搜一次"（参数微调）
    LLM 思考："还没结果，换个词再搜"
    LLM 思考："再试一次"
    ...（无限循环）
    每次搜索都消耗 5000+ token
    
  → cost_guard 防护：超过 $1 立即 deny

模式 2：死循环（Infinite Loop）
  ─────────────────────────────────
  没有防护：
    LLM 不断重复"完全相同"的工具调用
    "搜索 → 失败 → 搜索 → 失败 → ..."
    永远不收敛，CPU 100%，用户等到超时
    
  实际场景：
    LLM 思考："调用 baidu_search('Python 新特性')"
    → 返回空
    LLM 思考："没搜到，再搜一次"
    → 调用 baidu_search('Python 新特性')  ← 完全相同的调用！
    → 返回空
    LLM 思考："再试一次"
    → 调用 baidu_search('Python 新特性')  ← 第三次！
    
  → loop_detector 防护：连续 3 次相同调用即 deny

模式 3：重试风暴（Retry Storm）
  ─────────────────────────────────
  没有防护：
    工具调用失败后疯狂重试
    每秒重试 100 次，把下游服务打挂
    
  实际场景：
    baidu_search 失败（网络抖动）
    LLM 立即重试
    又失败，再重试
    → 形成"重试风暴"
    → 下游搜索 API 被打垮
    
  → retry_tracker 防护：连续失败 5 次告警
```

### 1.2 三个策略的分工

| 策略 | 防范问题 | 挂载事件 | 能否 deny | 生活类比 |
|------|---------|---------|----------|---------|
| cost_guard | 成本超限 | BEFORE_TOOL_CALL + AFTER_TURN | 是 | 预付费手机卡 |
| loop_detector | 死循环 | AFTER_TOOL_CALL + AFTER_TURN | 是 | 走迷宫检测原地打转 |
| retry_tracker | 重试风暴 | AFTER_TOOL_CALL | 否（只告警） | 火灾警报器 |

**为什么 retry_tracker 不 deny？**
- 重试不一定是坏事（网络抖动需要重试）
- 重试太多说明有问题，但"该不该继续重试"需要人工判断
- 让 cost_guard 和 loop_detector 负责"硬阻断"，retry_tracker 负责"软告警"

---

## 二、CostGuard — 成本围栏

### 2.1 设计理念：预付费手机卡

```
类比：预付费手机卡
  ─────────────────────────
  你充了 $1 话费（budget）
  每打一个电话扣一点（每次 LLM 调用扣 token 费）
  余额不足时停机（deny，不再执行）
  不能透支（宁可中断也不让账单失控）

为什么是 $1？
  - 正常任务成本：搜索 + 几轮对话约 $0.01-0.05
  - $1 = 正常成本的 20-100 倍，足够完成正常任务
  - 但能防止失控场景造成大额损失（如 $10、$100）
  
设计原则：
  - 按 session 累计：每个会话独立预算，互不影响
  - 实时检查：每次工具调用前检查，不等结束
  - 双重检查：BEFORE_TOOL_CALL 预检 + AFTER_TURN 算账
```

### 2.2 没有 cost_guard 会怎样？

```
反面场景：
  ─────────────────────────
  用户 8:00 发消息："帮我分析这个 100 页文档"
  
  没有 cost_guard：
    8:00:01 - LLM 调用 1（理解意图）：5000 token → $0.01
    8:00:03 - 工具调用：读取文档（无 LLM 成本）
    8:00:05 - LLM 调用 2（分析第 1-10 页）：8000 token → $0.016
    8:00:10 - LLM 调用 3（分析第 11-20 页）：8000 token → $0.016
    ...
    8:00:50 - LLM 调用 10（分析第 91-100 页）：8000 token → $0.016
    8:01:00 - LLM 调用 11（总结）：10000 token → $0.02
    8:01:05 - LLM 觉得"不够详细"，再来一轮
    8:01:10 - LLM 调用 12-22（重新分析）
    8:02:00 - LLM 觉得"需要对比"，再来一轮
    ...
    8:10:00 - 终于结束
    总成本：$2.5（远超合理范围）
  
  有 cost_guard（budget=$1）：
    8:00:01 ~ 8:01:00 - 正常分析，累计 $0.5
    8:01:05 - LLM 想再来一轮
    8:01:06 - before_tool_handler 检查：$0.5 < $1，放行
    8:01:10 ~ 8:01:50 - 第二轮，累计 $0.95
    8:01:55 - LLM 想第三轮
    8:01:56 - before_tool_handler 检查：$0.95 < $1，放行
    8:02:00 ~ 8:02:30 - 第三轮，累计 $1.05
    8:02:31 - after_turn_handler 算账：$1.05 > $1
    8:02:32 - 记录 WARNING，下一轮 before_tool_handler 会 deny
    8:02:35 - LLM 想第四轮
    8:02:36 - before_tool_handler 检查：$1.05 >= $1 → deny！
    → 回复用户："预算超限，请缩小任务范围"
    总成本：$1.05（控制在合理范围）
```

### 2.3 实现

```python
# shared_hooks/cost_guard.py
"""CostGuard —— 实时成本监控，超 $1 拒绝。

设计要点：
1. 按 session_id 累计成本（每个会话独立预算）
2. BEFORE_TOOL_CALL 检查预算（预防）
3. AFTER_TURN 累计本次开销（结算）
4. deny 时抛 GuardrailDeny，框架会捕获并中断执行
"""

import logging
from collections import defaultdict  # defaultdict：访问不存在的 key 时返回默认值

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny, HookContext

logger = logging.getLogger(__name__)

# Token 价格（美元/千 token）
# 这是 Qwen3-max 的定价（示例值，实际以官网为准）
# 为什么 input 和 output 价格不同？
# - input（输入）：模型需要"读"这些 token，成本较低
# - output（输出）：模型需要"生成"这些 token，成本较高（约 3 倍）
INPUT_PRICE_PER_1K = 0.002   # $0.002 / 1K input tokens
                            # 即每 1000 个输入 token 花费 $0.002
OUTPUT_PRICE_PER_1K = 0.006  # $0.006 / 1K output tokens
                            # 即每 1000 个输出 token 花费 $0.006


class CostGuard:
    """成本围栏 —— 按 session 累计，超预算 deny。

    挂载事件：
    - BEFORE_TOOL_CALL：检查预算（工具调用前预防）
    - AFTER_TURN：累计本次开销（轮次结束结算）

    成本计算公式：
    ─────────────────
    单次成本 = (input_tokens / 1000) × INPUT_PRICE_PER_1K
             + (output_tokens / 1000) × OUTPUT_PRICE_PER_1K
    
    示例：
      input_tokens = 2000, output_tokens = 100
      成本 = (2000/1000) × 0.002 + (100/1000) × 0.006
           = 0.004 + 0.0006
           = $0.0046

    使用示例：
    ----------
    >>> guard = CostGuard(budget_usd=1.0)
    >>> # 框架在 BEFORE_TOOL_CALL 自动调 guard.before_tool_handler(ctx)
    >>> # 框架在 AFTER_TURN 自动调 guard.after_turn_handler(ctx)

    注意事项：
    ----------
    - budget_usd 不要设太小（正常任务都跑不完）
    - 也不要设太大（失去保护意义）
    - $1 是经验值，可根据业务调整
    """

    def __init__(self, budget_usd: float = 1.0):
        """初始化成本围栏。

        参数表：
        ----------
        budget_usd : float
            单个 session 的预算上限（美元）
            默认 $1.0
            超过此值后 BEFORE_TOOL_CALL 会 deny

        返回值：
        ----------
        None
        """
        self._budget = budget_usd
        # session_id → 累计成本
        # defaultdict(float)：访问不存在的 key 时返回 0.0
        # 这样 self._session_costs["new_session"] 不会报 KeyError
        self._session_costs: dict[str, float] = defaultdict(float)

    def before_tool_handler(self, ctx: HookContext):
        """工具调用前 —— 检查是否超预算。

        参数表：
        ----------
        ctx : HookContext
            包含 session_id

        返回值：
        ----------
        None（正常放行）
        或抛 GuardrailDeny（超预算时）

        注意事项：
        ----------
        - 这是预防性检查，在工具执行前拦截
        - deny 后框架会捕获异常，中断当前流程
        - 用户会收到"预算超限"的提示
        """
        # 取当前 session 的累计成本
        # .get() 找不到返回 0（新 session）
        cost = self._session_costs.get(ctx.session_id, 0)

        if cost >= self._budget:
            # ★ 超预算 → deny
            # GuardrailDeny 是框架定义的异常
            # DenyReason.BUDGET_EXCEEDED 是拒绝原因枚举
            # 第二个参数是详细信息（会显示给用户）
            raise GuardrailDeny(
                DenyReason.BUDGET_EXCEEDED,
                f"Budget exceeded: ${cost:.4f} >= ${self._budget:.2f}"
                # ${cost:.4f} 保留 4 位小数（如 $0.0123）
                # ${self._budget:.2f} 保留 2 位小数（如 $1.00）
            )

    def after_turn_handler(self, ctx: HookContext):
        """轮次结束 —— 累计本次 token 成本。

        参数表：
        ----------
        ctx : HookContext
            包含 session_id、input_tokens、output_tokens

        返回值：
        ----------
        None

        注意事项：
        ----------
        - AFTER_TURN 的 deny 不会阻断当前轮次（已经完成了）
        - 但会记录 WARNING，下一轮的 before_tool_handler 会 deny
        - 这是"事后算账"模式
        """
        # 从 context 读取 token 数
        input_tokens = ctx.input_tokens    # 输入 token 数
        output_tokens = ctx.output_tokens  # 输出 token 数

        # 计算本次成本
        # 公式：(input/1000) × 输入价格 + (output/1000) × 输出价格
        cost = (
            (input_tokens / 1000) * INPUT_PRICE_PER_1K
            + (output_tokens / 1000) * OUTPUT_PRICE_PER_1K
        )

        # 累加到 session
        self._session_costs[ctx.session_id] += cost

        # 打日志（INFO 级别，正常运维信息）
        logger.info(
            "cost_guard: session=%s, this_turn=$%.6f, total=$%.4f, budget=$%.2f",
            ctx.session_id, cost,
            self._session_costs[ctx.session_id],
            self._budget,
        )
        # %.6f 保留 6 位小数（如 $0.000004）
        # %.4f 保留 4 位小数（如 $0.0123）
        # %.2f 保留 2 位小数（如 $1.00）

        # 再次检查（AFTER_TURN 算账后可能超限）
        if self._session_costs[ctx.session_id] >= self._budget:
            # 打 WARNING（运维需要关注）
            logger.warning(
                "cost_guard: budget exceeded for %s: $%.4f",
                ctx.session_id,
                self._session_costs[ctx.session_id],
            )
            # 注意：AFTER_TURN 的 deny 不会阻断当前轮次（已经完成了）
            # 但会记录到审计日志，下一轮的 before_tool_handler 会 deny
```

### 2.4 成本计算公式详解

**核心公式**：

```
单次成本 = (input_tokens / 1000) × INPUT_PRICE_PER_1K
         + (output_tokens / 1000) × OUTPUT_PRICE_PER_1K
```

**为什么除以 1000？**
因为价格是"每 1000 token 多少钱"，所以要先换算成"每 1 token 多少钱"：
- INPUT_PRICE_PER_1K = $0.002 → 每 token $0.000002
- 2000 token × $0.000002 = $0.004

**完整示例**：

```
正常请求（搜索 Python 新特性）：
  ─────────────────────────────────

  LLM 调用 1（理解意图）：
    input: 2000 tokens × $0.002/1K = $0.004
    output: 100 tokens × $0.006/1K = $0.0006
    小计: $0.0046

  工具调用（skill_loader）：无 LLM 成本
    → 工具调用本身不消耗 LLM token

  Sub-Crew LLM 调用（执行搜索）：
    input: 5000 tokens × $0.002/1K = $0.010
    output: 500 tokens × $0.006/1K = $0.003
    小计: $0.013

  LLM 调用 2（整理结果）：
    input: 3000 tokens × $0.002/1K = $0.006
    output: 800 tokens × $0.006/1K = $0.0048
    小计: $0.0108

  总计: $0.0284

→ 远低于 $1 预算，正常请求不会被拦截
→ 剩余预算：$1 - $0.0284 = $0.9716


失控请求（LLM 陷入循环）：
  ─────────────────────────────────

  第 1 次搜索：$0.0284（正常）
  第 2 次搜索（重复）：$0.0284
  第 3 次搜索（重复）：$0.0284
  ...
  第 35 次搜索：累计 $0.0284 × 35 = $0.994
  第 36 次搜索前：
    before_tool_handler 检查：$0.994 < $1，放行
    执行后累计：$0.994 + $0.0284 = $1.0224
    after_turn_handler 打 WARNING
  第 37 次搜索前：
    before_tool_handler 检查：$1.0224 >= $1 → deny！
    → 回复用户："Budget exceeded: $1.0224 >= $1.00"

对比：
  正常请求：$0.0284（35 倍富余）
  失控请求：被限制在 $1.02（如果不拦截会到 $10+）
```

---

## 三、LoopDetector — 循环检测

### 3.1 什么是 Agent 死循环？

```
LLM 思考：我需要搜索
  → 调用 baidu_search("Python 新特性")
  → 结果为空

LLM 思考：没搜到，再搜一次
  → 调用 baidu_search("Python 新特性")  ← 完全相同的调用！
  → 结果还是为空

LLM 思考：再试一次
  → 调用 baidu_search("Python 新特性")  ← 第三次！
  → ...（无限循环）
```

**为什么 LLM 会陷入死循环？**
- LLM 没有"记忆"——它看不到"我刚才已经试过同样的参数了"
- LLM 的"重试"策略太简单：失败就重试，不改变参数
- 没有外部干预，LLM 会一直试下去

### 3.2 检测原理：走迷宫时检测原地打转

```
类比：走迷宫
  ─────────────────
  你在迷宫里走，每走一步在脚下放一颗石头
  如果你发现"刚才放石头的位置"和"现在放石头的位置"一样
  → 说明你在原地打转
  → 连续 3 次原地打转 → 喊停

技术实现：
  把每次工具调用（tool_name + tool_input）做成一个"指纹"（MD5 哈希）
  连续 3 次相同的指纹 → 判定循环 → deny
```

### 3.3 MD5 哈希去重原理

> 背景知识：MD5 是什么？
> MD5 是一种"哈希函数"——把任意长度的输入（如字符串）变成固定长度的输出（32 位十六进制字符串）。
> 特点：
> 1. 同样的输入永远得到同样的输出（确定性）
> 2. 不同的输入几乎不可能得到同样的输出（抗碰撞）
> 3. 无法从输出反推输入（单向性）
>
> 用途：这里用 MD5 给每次工具调用生成"指纹"，比较指纹比比较原字符串快。

```python
# shared_hooks/loop_detector.py
"""LoopDetector —— MD5 哈希去重，连续 3 次相同则 deny。

设计要点：
1. 对 tool_name + tool_input 做 MD5 哈希 → 生成"指纹"
2. 记录最近 10 次的指纹（deque maxlen=10）
3. 如果连续 N 次相同 → deny
"""

import hashlib  # Python 标准库，提供 MD5 等哈希算法
from collections import defaultdict, deque  # deque：双端队列，自动淘汰旧数据

from xiaopaw.hook_framework.registry import DenyReason, GuardrailDeny, HookContext


class LoopDetector:
    """循环检测器。

    挂载事件：
    - AFTER_TOOL_CALL：记录工具调用，检测循环
    - AFTER_TURN：检测步骤级循环（输出文本重复）

    检测策略：
    ─────────────────
    对 tool_name + tool_input 做 MD5 哈希
    如果连续 N 次相同的哈希 → deny

    参数表：
    ----------
    threshold : int
        连续相同多少次触发 deny，默认 3

    使用示例：
    ----------
    >>> detector = LoopDetector(threshold=3)
    >>> # 框架自动调用 after_tool_handler / after_turn_handler

    注意事项：
    ----------
    - threshold 不要设 1（一次调用就 deny，太激进）
    - threshold 不要设太大（10次循环才拦截，浪费 token）
    - 3 是经验值：2 次可能是合理重试，3 次基本是死循环
    """

    def __init__(self, threshold: int = 3):
        """初始化循环检测器。

        参数表：
        ----------
        threshold : int
            连续相同多少次触发 deny，默认 3

        返回值：
        ----------
        None
        """
        self._threshold = threshold
        # session_id → 最近调用的哈希队列
        # deque(maxlen=10)：双端队列，最多保留 10 个元素
        # 超过 10 个会自动淘汰最旧的
        # 为什么保留 10 个？方便事后排查"最近 10 次调了什么"
        self._recent_hashes: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=10)  # 保留最近 10 次
        )
        # session_id → 连续相同计数
        # 记录"当前已经连续相同多少次"
        self._consecutive_counts: dict[str, int] = defaultdict(int)

    def after_tool_handler(self, ctx: HookContext):
        """工具调用后 —— 记录并检测循环。

        参数表：
        ----------
        ctx : HookContext
            包含 tool_name、tool_input、session_id

        返回值：
        ----------
        None（正常）或抛 GuardrailDeny（循环时）

        检测流程：
        ─────────────────
        1. 拼接 tool_name:tool_input
        2. 计算 MD5 哈希
        3. 与上一次比较
        4. 相同 → 计数 +1
        5. 不同 → 计数重置为 1
        6. 计数 >= threshold → deny
        """
        # 拼接 tool_name 和 tool_input 作为"调用指纹"的输入
        # 如 "baidu_search:{'query': 'Python 新特性'}"
        tool_desc = f"{ctx.tool_name}:{str(ctx.tool_input)}"
        
        # 计算 MD5 哈希
        # .encode() 把字符串转成字节（MD5 需要字节输入）
        # .hexdigest() 返回 32 位十六进制字符串（如 "abc123def456..."）
        hash_val = hashlib.md5(tool_desc.encode()).hexdigest()

        session_id = ctx.session_id
        recent = self._recent_hashes[session_id]  # 取该 session 的历史哈希
        counts = self._consecutive_counts

        # 检查是否与上一次相同
        # recent[-1] 取队列最后一个元素（即上一次的哈希）
        if recent and recent[-1] == hash_val:
            # 与上次相同 → 计数 +1
            counts[session_id] += 1
        else:
            # 与上次不同 → 重置计数为 1
            # 为什么是 1 而不是 0？因为当前这次也算"1 次相同"
            counts[session_id] = 1

        # 把当前哈希加入队列（自动淘汰最旧的）
        recent.append(hash_val)

        # 超过阈值 → deny
        if counts[session_id] >= self._threshold:
            raise GuardrailDeny(
                DenyReason.LOOP_DETECTED,
                f"Loop detected: {ctx.tool_name} called {counts[session_id]} times "
                f"with same input"
            )

    def after_turn_handler(self, ctx: HookContext):
        """轮次结束 —— 检测步骤级循环。

        如果 Agent 的输出文本连续 N 轮相同，也算循环。

        参数表：
        ----------
        ctx : HookContext
            ctx.metadata 包含 output

        返回值：
        ----------
        None 或抛 GuardrailDeny
        """
        output = ctx.metadata.get("output", "")
        if not output:
            return  # 没有输出，不检测

        # 对输出文本做 MD5
        hash_val = hashlib.md5(output.encode()).hexdigest()
        session_id = ctx.session_id
        recent = self._recent_hashes[session_id]
        counts = self._consecutive_counts

        if recent and recent[-1] == hash_val:
            counts[session_id] += 1
        else:
            counts[session_id] = 1

        recent.append(hash_val)

        if counts[session_id] >= self._threshold:
            raise GuardrailDeny(
                DenyReason.LOOP_DETECTED,
                f"Output loop detected: same output {counts[session_id]} times"
            )
```

### 3.4 检测示例：连续调用→计数→deny

```
场景：LLM 反复搜索同一关键词

调用 1: baidu_search("Python 新特性")
  拼接："baidu_search:{'query': 'Python 新特性'}"
  MD5 哈希："abc123def456..."
  recent = ["abc123def456..."]
  consecutive = 1  ← 不触发（< 3）

调用 2: baidu_search("Python 新特性")  ← 完全相同！
  拼接："baidu_search:{'query': 'Python 新特性'}"
  MD5 哈希："abc123def456..."  ← 与上次相同！
  recent[-1] == hash_val → True
  consecutive = 2  ← 不触发（< 3）
  recent = ["abc123...", "abc123..."]

调用 3: baidu_search("Python 新特性")  ← 第三次！
  拼接："baidu_search:{'query': 'Python 新特性'}"
  MD5 哈希："abc123def456..."  ← 还是相同！
  consecutive = 3  ← 触发 deny！
  → GuardrailDeny(LOOP_DETECTED, "baidu_search called 3 times with same input")

假设没 deny，调用 4: baidu_search("Java 新特性")  ← 参数变了
  拼接："baidu_search:{'query': 'Java 新特性'}"
  MD5 哈希："def456abc123..."  ← 不同！
  consecutive = 1  ← 重置
  recent = [..., "def456..."]
```

**关键点**：
- 只要参数有一丁点不同，MD5 就完全不同 → 不会误判
- 连续相同才计数，中间有一次不同就重置 → 容忍偶发重试

---

## 四、RetryTracker — 重试追踪

### 4.1 设计理念：火灾警报器

```
类比：火灾警报器
  ─────────────────
  火灾警报器只做两件事：
  1. 检测到烟雾 → 响警报
  2. 不灭火（灭火是消防员的事）
  
  retry_tracker 同理：
  1. 检测到连续失败 → 打 WARNING
  2. 不 deny（阻断是 cost_guard 和 loop_detector 的事）

为什么这样设计？
  ─────────────────
  cost_guard 和 loop_detector 都是"可阻断"的（抛 deny）
  retry_tracker 是"纯观测"的（只打 WARNING，不 deny）

  为什么 retry_tracker 不 deny？
    重试不一定是坏事（网络抖动需要重试）
    但重试太多说明有问题（下游服务挂了）
    所以只告警不阻断，让人工判断
```

### 4.2 没有 retry_tracker 会怎样？

```
反面场景：
  ─────────────────
  baidu_search 调用百度 API
  百度 API 突然挂了（返回 500）
  
  没有 retry_tracker：
    LLM 调用 baidu_search → 失败
    LLM 立即重试 → 失败
    LLM 再重试 → 失败
    ...（每秒 10 次）
    → 形成"重试风暴"
    → 百度 API 本来只是临时故障
    → 被你的重试彻底打挂
    → 影响其他用户
  
  有 retry_tracker：
    第 1 次失败：记录
    第 2 次失败：记录
    ...
    第 5 次失败：打 WARNING
      [RetryTracker] WARNING: baidu_search failed 5 times
      Consider checking downstream service.
    运维看到 WARNING → 检查百度 API 状态
    → 发现 API 挂了 → 临时切换到备用搜索
```

### 4.3 实现

```python
# shared_hooks/retry_tracker.py
"""RetryTracker —— 重试追踪，纯观测不阻断。

设计要点：
1. 只挂 AFTER_TOOL_CALL（工具调用后才知道成功/失败）
2. 按 (session_id, tool_name) 统计失败次数
3. 超过 max_retries 打 WARNING（不 deny）
4. 成功后重置计数
"""

import logging
import sys
from collections import defaultdict, deque

from xiaopaw.hook_framework.registry import HookContext

logger = logging.getLogger(__name__)


class RetryTracker:
    """重试追踪器。

    挂载事件：AFTER_TOOL_CALL
    特点：纯观测，只打 WARNING 不 deny

    检测逻辑：
    ─────────────────
    如果同一个工具连续失败（success=False）超过 max_retries 次
    → 打 WARNING 日志

    参数表：
    ----------
    max_retries : int
        连续失败多少次触发告警，默认 5

    使用示例：
    ----------
    >>> tracker = RetryTracker(max_retries=5)
    >>> # 框架自动调用 after_tool_handler

    注意事项：
    ----------
    - 成功后计数会重置（失败-失败-成功-失败 → 重新从 1 开始）
    - 只统计"连续"失败，偶发失败不告警
    - max_retries=5 是经验值：3 次可能误报，10 次太迟钝
    """

    def __init__(self, max_retries: int = 5):
        """初始化重试追踪器。

        参数表：
        ----------
        max_retries : int
            连续失败多少次触发告警，默认 5

        返回值：
        ----------
        None
        """
        self._max_retries = max_retries
        # session_id → (tool_name → 失败计数)
        # 嵌套 defaultdict：第一层按 session，第二层按 tool
        self._failure_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 保留最近的重试记录（用于事后排查）
        # deque(maxlen=20) 最多保留 20 条
        self._recent_retries: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=20)
        )

    def after_tool_handler(self, ctx: HookContext):
        """工具调用后 —— 记录成功/失败。

        参数表：
        ----------
        ctx : HookContext
            包含 session_id、tool_name、success

        返回值：
        ----------
        None（纯观测，永不 deny）

        检测流程：
        ─────────────────
        1. 如果 success=True → 重置该工具的失败计数
        2. 如果 success=False → 失败计数 +1
        3. 如果计数 >= max_retries → 打 WARNING
        """
        session_id = ctx.session_id
        tool_name = ctx.tool_name

        if ctx.success:
            # 成功 → 重置计数
            # 为什么成功就重置？
            # 因为只统计"连续"失败
            # 失败-失败-成功-失败 → 重新从 1 开始
            self._failure_counts[session_id][tool_name] = 0
        else:
            # 失败 → 计数 + 1
            self._failure_counts[session_id][tool_name] += 1
            count = self._failure_counts[session_id][tool_name]

            # 记录到最近重试队列（用于事后排查）
            self._recent_retries[session_id].append({
                "tool": tool_name,
                "count": count,
                "ts": ctx.timestamp,
            })

            # 超过阈值 → 打 WARNING（不 deny）
            if count >= self._max_retries:
                # 同时输出到 stderr 和 logger
                # stderr：运维在终端能直接看到
                # logger：写入日志文件
                print(
                    f"[RetryTracker] WARNING: {tool_name} failed {count} times "
                    f"in session {session_id}. Consider checking downstream service.",
                    file=sys.stderr,
                )
                logger.warning(
                    "retry_tracker: %s failed %d times in %s",
                    tool_name, count, session_id,
                )

    def get_retry_stats(self, session_id: str) -> dict:
        """获取重试统计（调试用）。

        参数表：
        ----------
        session_id : str
            会话 ID

        返回值：
        ----------
        dict
            {
                "failure_counts": {"baidu_search": 3, "skill_loader": 0},
                "recent_retries": [{"tool": "baidu_search", "count": 3, ...}]
            }

        使用示例：
        ----------
        >>> stats = tracker.get_retry_stats("abc123")
        >>> print(stats["failure_counts"])
        {'baidu_search': 3}
        """
        return {
            "failure_counts": dict(self._failure_counts.get(session_id, {})),
            "recent_retries": list(self._recent_retries.get(session_id, [])),
        }
```

### 4.4 日志输出示例

```
[RetryTracker] WARNING: baidu_search failed 5 times in session abc123.
  Consider checking downstream service.
```

**看到这条 WARNING 后运维该怎么做**：
1. 检查 baidu_search 的下游（百度 API）是否可用
2. 看最近的重试记录：`tracker.get_retry_stats("abc123")`
3. 如果是 API 挂了 → 切换备用搜索
4. 如果是参数问题 → 检查 LLM 生成的搜索词

---

## 五、三个策略的协同

### 5.1 执行顺序

在 `hooks.yaml` 中，三个策略的声明顺序很重要：

```yaml
strategies:
  - name: cost_guard           # 1. 先算账
    hooks:
      AFTER_TURN: after_turn_handler
      BEFORE_TOOL_CALL: before_tool_handler

  - name: loop_detector         # 2. 再检测循环
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
      AFTER_TURN: after_turn_handler

  - name: retry_tracker         # 3. 最后追踪重试
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
```

### 5.2 同一请求中三个策略的执行顺序

```
用户发消息 → Agent 开始处理

BEFORE_TOOL_CALL 事件触发：
  ┌─ 1. cost_guard.before_tool_handler
  │     检查预算：$0.5 < $1 → 放行
  │     （如果超预算 → deny，后续不执行）
  │
  └─ 2. （permission_gate 等其他策略）
        （这里只讨论 cost/loop/retry）

→ 工具执行（如 baidu_search）

AFTER_TOOL_CALL 事件触发：
  ┌─ 1. loop_detector.after_tool_handler
  │     记录哈希，检查循环
  │     （如果循环 → deny）
  │
  └─ 2. retry_tracker.after_tool_handler
        记录成功/失败，失败计数
        （永不 deny，只 WARNING）

→ Agent 整理结果，回复用户

AFTER_TURN 事件触发：
  ┌─ 1. cost_guard.after_turn_handler
  │     累计本次 token 成本
  │     （如果超预算 → WARNING，下一轮 deny）
  │
  └─ 2. loop_detector.after_turn_handler
        检测输出文本循环
```

### 5.3 为什么 cost_guard 必须先于 loop_detector？

```
循环场景：
  LLM 反复调用同一个工具（每次都消耗 token）

如果 loop_detector 先执行：
  → loop_detector deny
  → 链路中止
  → cost_guard 的 AFTER_TURN 不会执行
  → 这次循环的 token 成本没被计入
  → 预算统计偏低，看不出问题严重性
  → 运维以为"没花多少钱"，实际已经花了很多

如果 cost_guard 先执行：
  → cost_guard 先算账（记录本次 token 消耗）
  → loop_detector 再检测
  → 如果 loop_detector deny
  → cost_guard 已经记过账了
  → 预算统计准确
  → 运维能看到"循环了 3 次，花了 $0.15"
```

### 5.4 为什么 retry_tracker 排最后？

```
重试的常见原因：
  1. 网络抖动 → 重试是合理的，不应该 deny
  2. 下游服务临时不可用 → 重试可能成功
  3. 参数错误 → 重试不会成功，但 cost_guard 会拦截成本
  4. loop_detector 会拦截无限重试

所以 retry_tracker 只需要"告警"
让 cost_guard 和 loop_detector 负责阻断

为什么排最后？
  如果 loop_detector 已经 deny 了
  retry_tracker 再记录"失败"也没意义（已被中断）
  所以先让 loop_detector 检查，再让 retry_tracker 记录
```

### 5.5 三策略协同示例

```
完整场景：用户让 Agent 搜索一个非常冷门的关键词

第 1 轮：
  BEFORE_TOOL_CALL:
    cost_guard: $0 < $1 → 放行
  → baidu_search("xyz冷门词") 执行
  AFTER_TOOL_CALL:
    loop_detector: 哈希="abc", consecutive=1 → 不触发
    retry_tracker: success=False, count=1 → 不告警
  AFTER_TURN:
    cost_guard: 累计 $0.03
    loop_detector: 输出哈希="def", consecutive=1 → 不触发

第 2 轮（LLM 重试同样参数）：
  BEFORE_TOOL_CALL:
    cost_guard: $0.03 < $1 → 放行
  → baidu_search("xyz冷门词") 执行
  AFTER_TOOL_CALL:
    loop_detector: 哈希="abc", 与上次相同, consecutive=2 → 不触发
    retry_tracker: success=False, count=2 → 不告警
  AFTER_TURN:
    cost_guard: 累计 $0.06
    loop_detector: 输出哈希="def", consecutive=2 → 不触发

第 3 轮（LLM 第三次重试）：
  BEFORE_TOOL_CALL:
    cost_guard: $0.06 < $1 → 放行
  → baidu_search("xyz冷门词") 执行
  AFTER_TOOL_CALL:
    loop_detector: 哈希="abc", consecutive=3 → ★ deny！
    → GuardrailDeny(LOOP_DETECTED)
    → retry_tracker 不执行（链路已中断）
  → 回复用户："检测到循环，已停止"

结果：
  - 总成本：$0.06（远低于 $1）
  - 循环被 loop_detector 拦截
  - retry_tracker 没机会告警（因为 loop_detector 先 deny）
```

---

## 六、配置

```yaml
# shared_hooks/hooks.yaml 片段
strategies:
  - name: cost_guard
    class: cost_guard.CostGuard
    config:
      budget_usd: 1.0          # 单 session 预算 $1
    hooks:
      AFTER_TURN: after_turn_handler
      BEFORE_TOOL_CALL: before_tool_handler

  - name: loop_detector
    class: loop_detector.LoopDetector
    config:
      threshold: 3             # 连续 3 次相同则 deny
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
      AFTER_TURN: after_turn_handler

  - name: retry_tracker
    class: retry_tracker.RetryTracker
    config:
      max_retries: 5           # 连续失败 5 次告警
    hooks:
      AFTER_TOOL_CALL: after_tool_handler
```

**配置参数说明**：

| 策略 | 参数 | 默认值 | 调优建议 |
|------|------|--------|---------|
| cost_guard | budget_usd | 1.0 | 复杂任务可调到 5.0，简单任务可调到 0.5 |
| loop_detector | threshold | 3 | 保守可设 5，激进可设 2 |
| retry_tracker | max_retries | 5 | 网络差的环境可调到 10 |

---

## 七、设计优势与局限性

### 优势

1. **实时成本控制**：每次工具调用前检查预算，防止失控
2. **死循环检测**：MD5 哈希去重，高效准确
3. **纯观测不误杀**：retry_tracker 只告警不阻断，避免误伤合理重试
4. **协同工作**：三个策略互相补充，覆盖不同失控模式

### 局限性

1. **成本估算不精确**：token 价格可能变化，估算与实际有偏差
2. **循环检测的假阳性**：合法的重复调用可能被误判（如分页搜索）
3. **重试阈值固定**：不同工具的合理重试次数不同

---

## 八、常见问题

### ❓ 常见问题

**Q1：cost_guard 的成本估算和实际账单对不上？**

A：常见原因：
- token 价格变了：检查 LLM 服务商官网最新定价，更新 `INPUT_PRICE_PER_1K` 和 `OUTPUT_PRICE_PER_1K`
- 漏算了某些调用：CrewAI 内部可能有"隐式 LLM 调用"没被 hook 捕获
- 缓存命中：有些 LLM 服务商对相同 prompt 打折，但 cost_guard 按原价算
- 解决：定期对账，调整价格系数（如实际 = 估算 × 0.9）

**Q2：loop_detector 误报，合法的重复调用被 deny？**

A：场景：分页搜索时，LLM 可能合法地调用 `baidu_search("Python", page=1)`、`baidu_search("Python", page=2)`——参数不同，不会误判。但如果 LLM 真的调了完全相同的参数：
- 检查是不是 LLM 的 prompt 没写好，让它"换关键词重试"而不是"原样重试"
- 临时调高 threshold 到 5
- 或者在 tool_input 里加时间戳，让每次调用参数都不同

**Q3：retry_tracker 告警了，但不知道是哪个工具失败？**

A：看 WARNING 日志：
```
[RetryTracker] WARNING: baidu_search failed 5 times in session abc123.
```
- `baidu_search` 就是失败的工具
- 用 `tracker.get_retry_stats("abc123")` 查看详细记录
- 检查该工具的下游服务是否可用

**Q4：budget 设多少合适？**

A：经验值：
- 简单对话（问答）：$0.1 足够
- 搜索 + 工具调用：$0.5
- 复杂任务（多轮 + Sub-crew）：$1.0 ~ $5.0
- 先设 $1 跑一周，看实际消耗分布，再调整

**Q5：三个策略都没触发，但用户反馈"Agent 卡住了"？**

A：可能是其他原因：
- LLM 服务本身慢（看 `before_llm` 和 `after_llm` 的间隔）
- 工具调用卡住（看 `before_tool_call` 和 `after_tool_call` 的间隔）
- 网络问题（看 Langfuse trace 里各 span 的耗时）
- 排查方法：打开 Langfuse，找最耗时的 span

**Q6：cost_guard 的 deny 用户能看到吗？**

A：能。框架捕获 `GuardrailDeny` 后会回复用户：
```
安全策略拦截：Budget exceeded: $1.0224 >= $1.00
```
用户知道是预算超限，可以缩小任务范围重试。

**Q7：loop_detector 的 threshold 设 2 会不会太激进？**

A：会。场景：网络抖动导致第一次失败，LLM 合理重试——如果 threshold=2，第二次调用就会被 deny。建议至少 3，给合理重试留余地。

### 🔧 调试技巧

1. **打印 cost_guard 状态**：
   ```python
   # 在 after_turn_handler 里加：
   print(f"[cost] session={ctx.session_id}, total=${guard._session_costs[ctx.session_id]:.4f}")
   ```

2. **查看 loop_detector 的哈希队列**：
   ```python
   # 调试时打印：
   print(f"[loop] recent={list(detector._recent_hashes[session_id])}")
   print(f"[loop] consecutive={detector._consecutive_counts[session_id]}")
   ```

3. **模拟循环测试**：
   ```python
   # 手动触发 3 次相同调用，看是否 deny
   for i in range(5):
       try:
           detector.after_tool_handler(fake_ctx)
       except GuardrailDeny as e:
           print(f"第 {i+1} 次被 deny: {e}")
           break
   ```

4. **调整预算快速测试**：
   ```yaml
   # 临时把预算调到 $0.01，测试 deny 逻辑
   config:
     budget_usd: 0.01
   ```

5. **看 retry_tracker 统计**：
   ```python
   stats = tracker.get_retry_stats(session_id)
   print(json.dumps(stats, indent=2, ensure_ascii=False))
   ```

---

## 九、验证你的理解

- [ ] cost_guard 在什么事件检查预算？什么事件累计成本？
- [ ] 成本计算公式是什么？input 和 output 价格为什么不同？
- [ ] 为什么 cost_guard 必须在 loop_detector 之前执行？
- [ ] loop_detector 的检测原理是什么？阈值是多少？
- [ ] MD5 哈希为什么能用来检测循环？
- [ ] retry_tracker 为什么不 deny？它的作用是什么？
- [ ] 三个策略在同一请求中的执行顺序是什么？
- [ ] 如果 loop_detector 先于 cost_guard 执行会有什么问题？

---

> 下一篇：[14-安全层-sandbox-permission-audit](./14-安全层-sandbox-permission-audit.md)
