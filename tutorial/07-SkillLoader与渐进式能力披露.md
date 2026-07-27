# 07 - SkillLoader 与渐进式能力披露

> 本篇是 XiaoPaw"技能调度中枢"的实现。学完本篇，你会理解为什么 Agent 不能一次性把所有技能的细节都塞进 prompt，以及如何用"渐进式披露"模式让 Agent 在需要时才加载具体技能。

---

## 本节学习目标

读完这一篇，你应该能够回答以下问题：

1. 什么是"渐进式能力披露"？它解决了什么问题？
2. `SkillLoaderTool._build_description` 方法是怎么从 YAML 构建 LLM 看到的技能清单的？
3. `reference` 类型和 `task` 类型的技能有什么区别？
4. `ContextVar` 是什么？为什么 Sub-Crew 在子线程执行时需要 `copy_context()` 来传递它？
5. `_run` 方法的 7 个步骤分别做了什么？
6. 为什么用 `ThreadPoolExecutor` 而不是 `asyncio.to_thread`？
7. 为什么要在子线程里"部分重置" ContextVar，而不是全部保留或全部清空？

如果你对 Python 的 `ContextVar`、`ThreadPoolExecutor`、`asyncio` 还不熟，建议先看一些 Python 并发基础。

---

## 一、渐进式能力披露的概念

### 1.1 用"自助餐"类比理解

想象你去吃自助餐：

- **错误做法**：服务员把所有菜品的完整做法、原料、卡路里、烹饪时间全部讲一遍（5000 字）—— 你只想吃个牛排，听完已经饱了。
- **正确做法**：服务员给你一份菜单（每道菜只有名字和一句话描述）—— 你看了说"要牛排"，服务员再把牛排的详细做法告诉你。

SkillLoader 就是这个"服务员"：
- 给 LLM 看的是"技能菜单"（每个技能只有名字和描述）
- LLM 决定要哪个技能时，才加载那个技能的详细说明
- 这样 LLM 的"脑容量"（context window）不会被技能细节撑爆

### 1.2 问题：上下文爆炸

如果把所有技能的详细说明都放进 Agent 的 prompt：

```python
# ❌ 坏设计：全部技能细节塞进 prompt
agent_prompt = """
你可以使用以下技能：

1. 百度搜索：
   参数：query (str), time_filter (str)
   用法：skill_loader(skill_name="baidu_search", ...)
   详细说明：百度搜索 API 通过...（500 字）
   示例代码：...（200 字）

2. 网页浏览：
   参数：url (str), action (str)
   详细说明：...（500 字）
   ...

# 13 个技能 × 700 字 = 9100 字 的技能说明！
# 加上系统 prompt + 历史 + 用户消息 → 超出 token 限制
# 而且 LLM 每次思考都要看完所有技能，浪费时间浪费钱
"""
```

### 1.3 解决方案：渐进式披露

```python
# ✅ 好设计：只暴露技能清单（这就是 SkillLoaderTool 的 description）
agent_prompt = """
你可以使用以下技能：
  <skill>
    <name>baidu_search</name>
    <type>task</type>
    <description>百度搜索（支持时间过滤）</description>
  </skill>
  <skill>
    <name>web_browse</name>
    <type>task</type>
    <description>网页浏览、内容提取、截图</description>
  </skill>
  ...

# 13 个技能 × 50 字 = 650 字（只有原来的 7%！）
# 详细说明在调用时才加载（延迟加载，lazy loading）
"""
```

**关键对比**：

| 对比项 | 全部塞进 prompt | 渐进式披露 |
|--------|----------------|-----------|
| Prompt 大小 | 9100 字 | 650 字 |
| 每次思考耗时 | 长（要看 13 个技能细节） | 短（只看清单） |
| Token 消耗 | 高 | 低 |
| 维护性 | 改一个技能要改 prompt | 改 SKILL.md 即可 |

### 1.4 两层 Crew 的分工

```
Main Crew（知道"有什么技能"）           ← 只看技能清单
  │
  │ Agent 决定调用 baidu_search
  │ skill_loader(skill_name="baidu_search", task_context="搜索 Python 新特性")
  ▼
SkillLoader（读取 SKILL.md 详细说明）   ← 加载具体技能的指令
  │
  │ 构建并执行 Sub-Crew
  ▼
Sub-Crew（知道"怎么执行技能"）          ← 在沙箱里运行代码
  │
  │ 调用 MCP 工具（execute_command 等）
  ▼
AIO-Sandbox（隔离执行环境）            ← Docker 容器
```

这就是"两层 Crew"架构：
- **Main Crew**：知道"有什么技能"，负责理解用户意图、选技能
- **Sub-Crew**：知道"怎么执行技能"，在沙箱里跑代码（详见第 08 篇）

---

## 二、技能清单管理

### 2.1 load_skills.yaml —— 技能清单文件

```yaml
# 文件路径：xiaopaw/skills/load_skills.yaml
# 技能清单 —— Main Crew 通过这个文件知道有哪些技能可用
# 这个文件会被 SkillLoaderTool._build_description 读取

baidu_search:              # 技能名（skill_name 参数值）
  enabled: true            # 是否启用
  type: task               # task = 需要在沙箱执行
  path: baidu_search       # 技能目录名（在 xiaopaw/skills/ 下）

web_browse:
  enabled: true
  type: task
  path: web_browse

pdf:
  enabled: true
  type: task
  path: pdf

docx:
  enabled: true
  type: task
  path: docx

feishu_ops:
  enabled: true
  type: task
  path: feishu_ops

memory-save:
  enabled: true
  type: task
  path: memory-save

search_memory:
  enabled: true
  type: task
  path: search_memory

history_reader:
  enabled: true
  type: reference         # reference = 不需要沙箱，直接返回说明
  path: history_reader

# ... 更多技能
```

**两种技能类型的区别**：

| 类型 | 说明 | 例子 | 执行方式 |
|------|------|------|---------|
| `task` | 需要在沙箱执行代码 | baidu_search、pdf、docx | 构建 Sub-Crew → 沙箱执行 |
| `reference` | 只返回说明文档 | history_reader | 直接返回 SKILL.md 内容 |

### 2.2 SKILL.md 文件结构

每个技能目录下都有一个 `SKILL.md` 文件，包含技能的详细说明：

```markdown
<!-- 文件路径：xiaopaw/skills/baidu_search/SKILL.md -->
---
name: baidu_search
description: 百度搜索（支持时间过滤）
type: task
---

# 百度搜索技能

## 任务说明
使用百度搜索 API 搜索信息。

## 执行步骤
1. 调用百度搜索 API
2. 解析搜索结果
3. 提取标题、摘要、链接

## 脚本位置
搜索脚本在 `{skill_base}/scripts/search.py`
<!-- {skill_base} 运行时替换为实际路径 -->

## 参数
- query: 搜索关键词
- time_filter: 时间过滤（可选）
```

**SKILL.md 的两部分**：
1. **Frontmatter**（`---` 包围的部分）：元数据，用于构建技能清单
2. **正文**：详细执行指令，在调用时才加载给 Sub-Crew

### 2.3 技能目录结构

```
xiaopaw/skills/
├── load_skills.yaml          ← 技能清单（所有技能的注册表）
├── baidu_search/
│   ├── SKILL.md              ← 技能说明
│   └── scripts/
│       └── search.py         ← 实际执行脚本
├── web_browse/
│   ├── SKILL.md
│   └── scripts/
└── ...
```

---

## 三、SkillLoaderTool 实现

### 3.1 输入模型 SkillLoaderInput

```python
# 文件路径：xiaopaw/tools/skill_loader.py
from pydantic import BaseModel, Field, PrivateAttr, field_validator

class SkillLoaderInput(BaseModel):
    """SkillLoader 的输入参数定义。

    这个模型会被 CrewAI 转成 JSON Schema，塞进 LLM 的 tools 参数。
    LLM 看到的工具定义大致长这样：
    {
      "name": "skill_loader",
      "description": "...技能清单...",
      "parameters": {
        "type": "object",
        "properties": {
          "skill_name": {"type": "string", "description": "..."},
          "task_context": {"type": "string", "description": "..."}
        },
        "required": ["skill_name"]
      }
    }
    """

    skill_name: str = Field(
        ...,    # ... 表示必填
        description="Skill 名称，必须与可用列表中的 <name> 匹配"
    )
    task_context: str = Field(
        default="",    # 默认空字符串
        description="任务上下文，详细描述需要执行的操作。对于 task 类型的 Skill，建议使用 JSON 格式。"
    )

    @field_validator("task_context", mode="before")
    @classmethod
    def coerce(cls, v):
        """字段验证器：在赋值前转换值。

        为什么要这个？
        LLM 有时会把 task_context 传成 dict 或 list（而不是字符串），
        这里统一转成 JSON 字符串，避免后续处理出错。

        参数：
            v: 原始值（可能是 None/dict/list/str）

        返回：
            str: 转换后的字符串
        """
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)
```

### 3.2 SkillLoaderTool 类定义

```python
from crewai.tools import BaseTool

class SkillLoaderTool(BaseTool):
    """SkillLoader 工具 —— 渐进式能力披露 + Sub-Crew 触发器。

    这是整个系统的"枢纽"：
    - Main Crew 只看到技能清单（通过 description）
    - 调用时才加载详细说明（通过 _get_skill_instructions）
    - 在子线程构建 Sub-Crew 执行（通过 _run + copy_context）
    - 通过 ContextVar 传递追踪信息（adapter/trace_id）

    继承 BaseTool 的好处：
    - 自动适配 CrewAI 的工具调用机制
    - 自动生成 JSON Schema 给 LLM
    - 支持 _run（同步）和 _arun（异步）两种调用方式
    """

    name: str = "skill_loader"            # 工具名（LLM 看到的）
    description: str = ""                 # 工具描述（运行时动态构建，见 _build_description）
    args_schema: type = SkillLoaderInput  # 参数模型（用于生成 JSON Schema）

    # PrivateAttr：不暴露给 LLM 的内部状态
    # 为什么用 PrivateAttr？因为这些字段是内部状态，不应该出现在 JSON Schema 里
    _session_id: str = PrivateAttr(default="")
    _sandbox_url: str = PrivateAttr(default="")
    _routing_key: str = PrivateAttr(default="")
    _skill_registry: dict = PrivateAttr(default_factory=dict)    # 技能注册表
    _instruction_cache: dict = PrivateAttr(default_factory=dict) # 指令缓存
    _history_all: list = PrivateAttr(default_factory=list)        # 完整历史

    def __init__(
        self,
        session_id: str = "",
        sandbox_url: str = "",
        routing_key: str = "",
        history_all: list | None = None,
        **kwargs,
    ) -> None:
        """初始化 SkillLoaderTool。

        参数：
            session_id: 会话 ID（用于构建会话目录路径）
            sandbox_url: 沙箱 MCP 地址（如 http://localhost:8030/mcp）
            routing_key: 路由键（写入 SKILL.md 的 sandbox_directive）
            history_all: 完整历史消息（history_reader 技能用）
            **kwargs: 传给父类的额外参数

        注意事项：
            - session_id 会做正则校验（防止路径遍历攻击）
            - _build_description() 在初始化时调用，构建工具描述
        """
        super().__init__(**kwargs)

        # 安全校验：session_id 只允许字母数字下划线横线
        if session_id and not _SESSION_ID_PATTERN.match(session_id):
            raise ValueError(f"Invalid session_id: {session_id!r}")

        self._session_id = session_id
        self._sandbox_url = sandbox_url
        self._routing_key = routing_key
        self._history_all = history_all or []
        self._skill_registry = {}        # {skill_name: {type, path, dir}}
        self._instruction_cache = {}     # {skill_name: instructions_str}

        # ★ 初始化时构建工具描述（读 load_skills.yaml）
        self._build_description()
```

### 3.3 _build_description —— 动态构建工具描述

这是"渐进式披露"的核心：从 YAML 读取技能清单，构建成 LLM 看到的工具描述。

```python
    def _build_description(self) -> None:
        """从 load_skills.yaml 构建工具描述。

        这个描述会显示给 LLM，让它知道有哪些技能可用。
        构建结果大致长这样：

        加载并调用 Skill。会话目录: /workspace/sessions/xxx
        上传文件: /workspace/sessions/xxx/uploads/
        输出文件: /workspace/sessions/xxx/outputs/

        [重要] 下方 <name> 标签内容是 skill_name 参数值...
        正确调用方式：skill_loader(skill_name="baidu_search", task_context="...")

        <available_skills>
          <skill>
            <name>baidu_search</name>
            <type>task</type>
            <description>百度搜索（支持时间过滤）</description>
          </skill>
          <skill>
            <name>web_browse</name>
            ...
          </skill>
        </available_skills>

        副作用：
            - 填充 self.description（LLM 看到的工具描述）
            - 填充 self._skill_registry（内部技能注册表）
        """
        # 1. 找到技能清单文件
        manifest_path = _SKILLS_DIR / "load_skills.yaml"
        if not manifest_path.exists():
            self.description = "No skills available."
            return

        # 2. 解析 YAML
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        skills_xml: list[str] = []    # 收集每个技能的 XML 片段

        # 3. 遍历每个技能
        for skill_name, skill_cfg in manifest.items():
            # 跳过禁用的技能
            if not skill_cfg.get("enabled", True):
                continue

            # 获取技能路径（如果没配 path，就用 skill_name）
            skill_path = skill_cfg.get("path", skill_name)
            # 安全检查：防止路径遍历攻击（如 ../../etc/passwd）
            if ".." in skill_path:
                logger.warning("path traversal blocked in skill: %s", skill_name)
                continue

            # 找到技能目录和 SKILL.md
            skill_dir = _SKILLS_DIR / skill_path
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue    # 没有 SKILL.md 就跳过

            # 从 SKILL.md 的 frontmatter 提取描述
            desc = self._extract_frontmatter_description(
                skill_md.read_text(encoding="utf-8")
            )
            skill_type = skill_cfg.get("type", "reference")

            # 注册到内部表（后续 _get_skill_instructions 用）
            self._skill_registry[skill_name] = {
                "type": skill_type,
                "path": skill_path,
                "dir": skill_dir,
            }

            # 构建 XML 片段（这就是 LLM 看到的技能清单条目）
            skills_xml.append(
                f"  <skill>\n"
                f"    <name>{skill_name}</name>\n"
                f"    <type>{skill_type}</type>\n"
                f"    <description>{desc}</description>\n"
                f"  </skill>"
            )

        # 4. 构建会话目录路径（用于告诉 LLM 文件存哪）
        session_dir = f"/workspace/sessions/{self._session_id}" if self._session_id else "/workspace"

        # 5. 构建头部说明（告诉 LLM 怎么调用）
        header = (
            f"加载并调用 Skill。会话目录: {session_dir}\n"
            f"上传文件: {session_dir}/uploads/\n"
            f"输出文件: {session_dir}/outputs/\n\n"
            f"[重要] 下方 <name> 标签内容是 skill_name 参数值，不是工具名称。\n"
            f"正确调用方式：skill_loader(skill_name=\"baidu_search\", task_context=\"...\")\n"
            f"错误做法：直接以 baidu_search 为工具名调用（会报 Tool not found）\n\n"
        )

        # 6. 拼接完整 description
        self.description = (
            header
            + "<available_skills>\n"
            + "\n".join(skills_xml)
            + "\n</available_skills>"
        )
```

**逐行解释关键点**：

1. **`yaml.safe_load`**：安全解析 YAML，避免任意代码执行
2. **`if not skill_cfg.get("enabled", True)`**：默认启用（没配 enabled 就当 True）
3. **`if ".." in skill_path`**：路径遍历防护，防止 `../../etc/passwd` 这种攻击
4. **XML 标签格式**：用 XML 而不是 JSON，因为 LLM 对 XML 标签的解析更稳定

### 3.4 _extract_frontmatter_description —— 提取 frontmatter

```python
    def _extract_frontmatter_description(self, content: str) -> str:
        """从 SKILL.md 的 frontmatter 提取 description 字段。

        SKILL.md 格式：
        ---
        name: baidu_search
        description: 百度搜索（支持时间过滤）
        type: task
        ---
        # 正文...

        参数：
            content: SKILL.md 的完整内容

        返回：
            str: description 字段的值（截断到 200 字）
        """
        # 用正则匹配 frontmatter（--- 包围的部分）
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            # 没有 frontmatter，返回正文前 200 字
            return content[:200]
        try:
            # 解析 frontmatter 为 YAML
            fm = yaml.safe_load(match.group(1))
            # 取 description 字段，截断到 200 字
            return str(fm.get("description", ""))[:200]
        except yaml.YAMLError:
            # YAML 解析失败，兜底返回正文前 200 字
            return content[:200]
```

### 3.5 _get_skill_instructions —— 获取技能详细指令

```python
    def _get_skill_instructions(self, skill_name: str) -> str:
        """加载技能的详细指令（延迟加载，带缓存）。

        只在第一次调用时读取文件，后续从缓存取。
        这样如果一个技能被调用多次，只读一次磁盘。

        参数：
            skill_name: 技能名

        返回：
            str: 完整的技能指令（包含正文 + 模板替换 + 沙箱指令）

        处理流程：
        1. 检查缓存，命中直接返回
        2. 读 SKILL.md，去掉 frontmatter
        3. 替换模板变量（{skill_base} 等）
        4. 添加沙箱执行指令
        5. 存入缓存并返回
        """
        # 1. 检查缓存
        if skill_name in self._instruction_cache:
            return self._instruction_cache[skill_name]

        # 2. 读取 SKILL.md
        info = self._skill_registry[skill_name]
        skill_md = info["dir"] / "SKILL.md"
        raw = skill_md.read_text(encoding="utf-8")

        # 3. 去掉 frontmatter（--- ... ---）
        # re.sub 用 DOTALL 标志让 . 匹配换行符
        instructions = re.sub(r"^---\n.*?\n---\n?", "", raw, count=1, flags=re.DOTALL)

        # 4. 替换模板变量
        session_dir = f"/workspace/sessions/{self._session_id}" if self._session_id else "/workspace"
        if self._sandbox_url:
            # 有沙箱：技能脚本在沙箱里的挂载路径
            skill_base = f"{_SANDBOX_SKILLS_MOUNT}/{info['path']}"
            # _SANDBOX_SKILLS_MOUNT = "/mnt/skills"
        else:
            # 无沙箱：用宿主机路径（本地开发用）
            skill_base = str(info["dir"])

        # 替换 SKILL.md 里的占位符
        instructions = instructions.replace("{skill_base}", skill_base)
        instructions = instructions.replace("{_skill_base}", skill_base)
        instructions = instructions.replace("{session_id}", self._session_id)
        instructions = instructions.replace("{session_dir}", session_dir)

        # 5. 转义未识别的花括号（防止 CrewAI 模板报错）
        # CrewAI 会把 {xxx} 当模板变量，如果 SKILL.md 里有 {xxx} 但没对应变量会报错
        def _escape_unresolved(text: str) -> str:
            """把 {xxx} 转成 {{xxx}}，让 CrewAI 当字面量处理。"""
            return _CREWAI_VAR_PATTERN.sub(
                lambda m: "{{" + m.group(1) + "}}", text
            )
        instructions = _escape_unresolved(instructions)

        # 6. 添加沙箱执行指令
        sandbox_directive = (
            f"\n\n<sandbox_execution_directive>\n"
            f"会话目录: {session_dir}\n"
            f"技能脚本目录: {skill_base}\n"
            f"routing_key: {self._routing_key}\n"
            f"执行脚本前请先 cd {skill_base}\n"
            f"</sandbox_execution_directive>"
        )
        instructions += sandbox_directive

        # 7. 存入缓存并返回
        self._instruction_cache[skill_name] = instructions
        return instructions
```

### 3.6 _execute_skill_async —— 异步执行技能

```python
    async def _execute_skill_async(
        self, skill_name: str, task_context: str
    ) -> str:
        """异步执行技能（在子线程的 event loop 里调用）。

        分两种情况：
        1. reference 类型：直接返回指令文本（如 history_reader）
        2. task 类型：构建 Sub-Crew 在沙箱执行

        参数：
            skill_name: 技能名
            task_context: 任务上下文（用户要做什么）

        返回：
            str: 执行结果

        异常处理：
            finally 块清理 MCP 连接（防止泄漏）
        """
        # history_reader 特殊处理（不需要沙箱，直接返回历史）
        if skill_name == "history_reader":
            return self._handle_history_reader(task_context)

        # 加载技能指令
        info = self._skill_registry[skill_name]
        instructions = self._get_skill_instructions(skill_name)

        # reference 类型：直接返回说明（不执行任何代码）
        if info["type"] == "reference":
            return f"<skill_instructions>\n{instructions}\n</skill_instructions>"

        # task 类型：构建 Sub-Crew 执行（详见第 08 篇）
        crew = build_skill_crew(
            skill_name=skill_name,
            skill_instructions=instructions,
            session_id=self._session_id,
            sandbox_mcp_url=self._sandbox_url,
        )

        # 准备输入参数
        inputs = {"task_context": task_context, "skill_name": skill_name}
        # 防止 SKILL.md 里有未识别的模板变量导致报错
        for m in _CREWAI_VAR_PATTERN.finditer(instructions):
            var = m.group(1)
            if var not in inputs:
                inputs[var] = var    # 用变量名当默认值

        try:
            # 执行 Sub-Crew
            result = await crew.akickoff(inputs=inputs)
            return str(result)
        finally:
            # 清理：注销 Sub-Crew 的工具 Hook
            hook = getattr(crew, "_subcrew_tool_hook", None)
            if hook is not None:
                try:
                    from crewai.hooks import unregister_before_tool_call_hook
                    unregister_before_tool_call_hook(hook)
                except (ValueError, AttributeError):
                    pass

            # 清理：关闭 MCP 连接（超时 10 秒）
            for agent in crew.agents:
                for mcp in getattr(agent, "mcps", []) or []:
                    try:
                        if hasattr(mcp, "stop") and callable(mcp.stop):
                            await asyncio.wait_for(mcp.stop(), timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "MCP graceful stop timed out after 10s for %s, "
                            "deferring to event loop cleanup",
                            skill_name,
                        )
                    except Exception as exc:
                        logger.warning("MCP cleanup error (non-fatal): %s", exc)
```

### 3.7 _run —— 同步入口（核心！）

这是整个系统最复杂的方法。**CrewAI 在主线程同步调用 `_run`，但 Sub-Crew 内部用 asyncio，不能直接在主线程跑。** 所以要把 Sub-Crew 塞到独立子线程执行，并通过 `copy_context()` 桥接 ContextVar。

```python
    def _run(self, skill_name: str, task_context: str = "") -> str:
        """★ 同步入口 —— ContextVar 跨线程传递的核心枢纽 ★

        什么时候触发？
        CrewAI 在主线程同步调用 BaseTool._run()。
        这是 CrewAI 调用工具的标准入口（_arun 是异步版本，但 CrewAI 内部会选一个）。

        为什么不直接用 _arun（异步版本）？
        因为 Sub-Crew 内部用 asyncio + 会调用阻塞 IO（沙箱 MCP），
        不能直接在主线程的 event loop 里跑（会和主 loop 冲突）。
        所以另起一个完全独立的子线程，子线程内自建 event loop。

        7 个步骤详解：
        ① 快照父线程的 Langfuse 父 span ID
        ② copy_context() 把所有 ContextVar 打成快照
        ③ 提交到线程池，用 ctx.run 桥接 ContextVar
        ④ 子线程内：ContextVar 已自动可见
        ⑤ 选择性重置 Langfuse ContextVar
        ⑥ 真正执行 Sub-Crew
        ⑦ 清理 + flush

        参数：
            skill_name: 技能名
            task_context: 任务上下文

        返回：
            str: 执行结果

        超时：
            300 秒（5 分钟）—— Sub-Crew 在沙箱里跑长任务的兜底
        """
        # 校验技能名
        if skill_name not in self._skill_registry and skill_name != "history_reader":
            available = ", ".join(sorted(self._skill_registry.keys()))
            return (
                f"错误：未找到 Skill '{skill_name}'。\n"
                f"可用 Skills: {available}"
            )

        import contextvars
        import concurrent.futures

        # ── 步骤①：快照父线程的 Langfuse 父 span ID ──
        # 必须在 copy_context() 之前取
        # 此时主线程栈顶是当前 skill_loader 自己的 span
        parent_span_id = _get_langfuse_parent_span_id()

        # ── 步骤②：copy_context() 把所有 ContextVar 当前值打成快照 ──
        # 注意：copy_context() 复制的是"键值对的浅拷贝"，对值是引用：
        #   - adapter 对象：引用共享（这正是我们想要的，sub-crew 用同一个 adapter）
        #   - trace_id 字符串：引用共享（不可变，安全）
        #   - _span_stack_var 元组：引用共享（不可变，安全）
        # 子线程后续通过 .set() 修改 ContextVar，是写入子线程私有的副本表
        ctx = contextvars.copy_context()

        def _run_with_cleanup():
            """在子线程里执行的闭包。

            被 ctx.run() 包裹后，所有 ContextVar 读写都作用在 ctx 这个副本上。
            """
            # 子线程独立的 event loop（不和主线程的 loop 冲突）
            loop = asyncio.new_event_loop()

            # ── 步骤⑤：选择性重置 Langfuse ContextVar ──
            # 此时已经在 ctx.run() 内部，写入只影响子线程副本
            _reset_langfuse_contextvars(parent_span_id)
            try:
                # ── 步骤⑥：真正执行 Sub-Crew ──
                # _execute_skill_async 内部调 build_skill_crew + crew.akickoff
                return loop.run_until_complete(
                    self._execute_skill_async(skill_name, task_context)
                )
            finally:
                # ── 步骤⑦：清理 + flush ──
                # 关闭子线程内未 close 的 span/gen，把 buffer 推送到 Langfuse
                _flush_langfuse_subcrew()

                # 取消子 loop 里残留的 task（避免 loop.close 警告）
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    try:
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=15.0,
                            )
                        )
                    except (asyncio.TimeoutError, Exception):
                        logger.warning("Event loop cleanup timed out, forcing close")
                loop.close()

        # ── 步骤③：把闭包提交到独立线程，用 ctx.run 桥接 ContextVar ──
        # ctx.run(fn) 的语义：在调用 fn 前激活 ctx 这个 ContextVar 表，
        # fn 内部所有 ContextVar 读写都作用在副本上，fn 返回后副本被丢弃
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ctx.run, _run_with_cleanup)
            # ── 步骤④：子线程内 ContextVar 自动可见 ──
            # （这是 ctx.run 的效果，不需要显式代码）
            # 5 分钟超时——Sub-Crew 在沙箱里跑长任务的兜底
            return future.result(timeout=300)
```

#### _run 的 7 步流程图解

```
主线程                                      子线程（ThreadPoolExecutor）
──────                                      ──────────────────────────

_run() 被调用
  │
  ├─ ① _get_langfuse_parent_span_id()
  │    取栈顶 span_id 存到局部变量
  │
  ├─ ② ctx = contextvars.copy_context()
  │    快照所有 ContextVar
  │
  ├─ ③ pool.submit(ctx.run, _run_with_cleanup)
  │    提交到线程池                         │
  │                                        ▼
  │                                  ④ ctx.run(_run_with_cleanup)
  │                                     子线程的 ContextVar = 父快照副本
  │                                     adapter / trace_id 自动可见
  │                                        │
  │                                        ▼
  │                                  ⑤ _reset_langfuse_contextvars(parent_span_id)
  │                                     - 保留 _trace_id_var（同一棵树）
  │                                     - _root_span_id_var ← parent_span_id
  │                                     - _gen_id_var / _span_stack_var ← 清零
  │                                        │
  │                                        ▼
  │                                  ⑥ loop.run_until_complete(
  │                                       self._execute_skill_async(...)
  │                                     )
  │                                     Sub-Crew 跑起来
  │                                        │
  │                                        ▼
  │                                  ⑦ finally:
  │                                     _flush_langfuse_subcrew()
  │                                     关闭残留 task
  │                                     loop.close()
  │                                        │
  ▼                                        ▼
  future.result(timeout=300)
  ← 返回结果
```

---

## 四、ContextVar 跨线程传递原理

### 4.1 什么是 ContextVar？

`ContextVar`（上下文变量）是 Python 3.7+ 提供的"线程安全的全局变量"。可以理解为**"每个线程私有的全局变量"**。

```python
import contextvars

# 创建一个 ContextVar（类似"线程局部变量"）
_current_adapter = contextvars.ContextVar("current_adapter", default=None)

# 设置值（返回 token，用于重置）
adapter = MyAdapter()
token = _current_adapter.set(adapter)

# 获取值（在同一线程内能拿到）
adapter = _current_adapter.get()    # → MyAdapter 实例

# 重置（恢复到 set 之前的状态）
_current_adapter.reset(token)
```

**为什么不用 `threading.local`（线程局部变量）？**

| 特性 | threading.local | ContextVar |
|------|----------------|-----------|
| 线程隔离 | 是 | 是 |
| 协程隔离 | 否 | 是 |
| 跨线程传递 | 困难 | 简单（copy_context） |
| asyncio 友好 | 否 | 是 |

XiaoPaw 用 asyncio，所以必须用 ContextVar。

### 4.2 为什么需要跨线程传递？

**场景**：Main Crew 在主线程运行，调用了 `skill_loader`。`skill_loader._run` 要在子线程执行 Sub-Crew。但 Sub-Crew 里的 Hook（如 `langfuse_trace`）需要访问主线程的 adapter 和 trace_id。

```
主线程                              子线程
──────                              ──────
MainCrew 执行
  │
  ├─ adapter = get_current_adapter()
  │  (ContextVar 里有值)
  │
  ├─ skill_loader._run() 被调用
  │
  ├─ ctx = copy_context()
  │  (快照所有 ContextVar)
  │
  └─ pool.submit(ctx.run, ...)
       │
       └─→ 子线程里 _current_adapter 自动可见
           SubCrew 的 Hook 调用 get_current_adapter()
           能拿到同一个 adapter 实例 ← 共享（故意）
```

如果不传，子线程里 `get_current_adapter()` 会返回 `None`，Sub-Crew 的所有 Hook 都失效。

### 4.3 copy_context 的语义

```python
# copy_context() 返回一个 Context 对象
ctx = contextvars.copy_context()

# ctx 包含当前所有 ContextVar 的快照
# 注意：值是引用共享的（浅拷贝）

# ctx.run(fn) 在 ctx 的上下文中执行 fn
ctx.run(some_function)
# some_function 内的 ContextVar 读写作用于 ctx
# 主线程的 ContextVar 不受影响（写时复制语义）

# 举例：
ctx = contextvars.copy_context()      # 快照
ctx.run(lambda: _current_adapter.set(None))  # 在 ctx 里改
# 主线程的 _current_adapter 仍然是原来的值！
```

### 4.4 父子线程视角对比图

```
主线程的 ContextVar                    子线程的 ContextVar（copy_context 后）
─────────────────                     ─────────────────────────

_current_adapter                       _current_adapter
  = <CrewObservabilityAdapter>          = <CrewObservabilityAdapter>  ← 共享（故意）
                                         （子线程改 ContextVar 不影响主线程，
                                           但 adapter 对象本身是同一个）

_trace_id_var                          _trace_id_var
  = "s-2976e0a09d01"                    = "s-2976e0a09d01"          ← 共享（同一棵树）

_root_span_id_var                      _root_span_id_var
  = "span-session-xxx"                  = "span-skill-baidu"        ← 重置为父 skill span
                                         （让子 trace 挂在父 skill 之下）

_span_stack_var                        _span_stack_var
  = (("span-skill-baidu",...),)         = ()                         ← 重置（子线程从空栈开始）

_gen_id_var                            _gen_id_var
  = "gen-llm-3"                         = ""                         ← 重置（没有未关闭的 gen）
```

**为什么这样设计？**

| ContextVar | 处理方式 | 原因 |
|-----------|---------|------|
| `_current_adapter` | 共享 | Sub-Crew 要用同一个 adapter（共用 _pending_deny） |
| `_trace_id_var` | 共享 | Sub-Crew 的 trace 必须挂在同一棵树上 |
| `_root_span_id_var` | 重置为父 skill span | 让子 trace 挂在父 skill 之下 |
| `_span_stack_var` | 清零 | 子线程的 push/pop 不应污染主线程栈 |
| `_gen_id_var` | 清零 | 子线程没有未关闭的 LLM generation |

### 4.5 步骤①和⑤的辅助函数

```python
def _get_langfuse_parent_span_id() -> str:
    """步骤①：在主线程里快照出当前应该作为 sub-crew 父节点的 span ID。

    必须在 copy_context() 之前调用——因为：
    - 此时主线程的 _span_stack_var 栈顶 = 当前 skill_loader 工具的 span
    - 我们想要把 sub-crew 的所有 observation 挂在这个 span 之下
    - 取出 span_id 存到普通局部变量，跨线程传递不依赖 ContextVar

    返回：
        str: 父 span ID（可能为空字符串）
    """
    try:
        from shared_hooks.langfuse_trace import _root_span_id_var, _span_stack_var

        stack = _span_stack_var.get(())
        if stack:
            # 栈顶元素结构：(span_id, tool_name, turn_number, tool_input)
            return stack[-1][0]
        # 栈空兜底：用 session 根 span 当父节点
        return _root_span_id_var.get("")
    except ImportError:
        return ""


def _reset_langfuse_contextvars(parent_span_id: str = "") -> None:
    """步骤⑤：子线程里对 Langfuse ContextVar 做"选择性重置"。

    在子线程开头调用（此时所有 ContextVar 已经是父线程快照的副本）。

    为什么不能直接共享父线程状态？
    copy_context() 复制的是"快照"，但 Langfuse 的几个 ContextVar 含义是
    "当前线程正在做什么"——继承父值会出问题：
        _gen_id_var = "gen-父线程-3"  ← 子线程不该认为自己有未关闭的 gen
        _span_stack_var = (父栈)      ← 子线程的 push/pop 会污染主线程视图

    为什么 _trace_id_var 不重置？
    它代表"这次对话属于哪棵 trace 树"——子 crew 的 observation 必须挂在同一棵树上，
    否则 Langfuse Session 视图里会拆成两条独立 trace。
    """
    try:
        from shared_hooks.langfuse_trace import (
            _closed_spans_var,
            _gen_count_var,
            _gen_id_var,
            _root_span_id_var,
            _span_stack_var,
            _tool_count_var,
        )

        # 把"子线程的 root span"改写成父 skill 的 span_id
        if parent_span_id:
            _root_span_id_var.set(parent_span_id)
        else:
            # 兜底：parent_span_id 取空时，从父快照的栈顶取
            stack = _span_stack_var.get(())
            if stack:
                _root_span_id_var.set(stack[-1][0])

        # 重置"瞬时状态"——子线程从干净的栈/计数开始
        _gen_id_var.set("")          # 没有未关闭的 gen
        _gen_count_var.set(0)        # generation 编号重新从 0 数
        _tool_count_var.set(0)       # tool span 编号重新从 0 数
        _span_stack_var.set(())      # 空栈
        _closed_spans_var.set({})    # 已关闭 span 索引清空
    except ImportError:
        pass


def _flush_langfuse_subcrew() -> None:
    """步骤⑦：子线程结束前清理 + flush。

    转调 langfuse_trace.subcrew_cleanup()，关闭子线程内未 close 的 span/gen
    并把 buffer 里累积的事件推送到 Langfuse。

    重要：subcrew_cleanup 不会重置 ContextVar——
    因为 ContextVar 是子线程副本，重置无意义；同时父线程从未让出执行权，
    它的 ContextVar 完全独立，不需要"恢复"。
    """
    try:
        from shared_hooks.langfuse_trace import subcrew_cleanup
        subcrew_cleanup()
    except ImportError:
        pass
```

---

## 五、技能列表一览

| 技能 | 类型 | 说明 |
|------|------|------|
| `baidu_search` | task | 百度搜索（支持时间过滤） |
| `web_browse` | task | 网页浏览、内容提取、截图 |
| `pdf` | task | PDF 读写 |
| `docx` | task | Word 文档处理 |
| `pptx` | task | PowerPoint 处理 |
| `xlsx` | task | Excel 处理 |
| `feishu_ops` | task | 飞书消息/文档/表格/日历 |
| `scheduler_mgr` | task | 定时任务管理 |
| `memory-save` | task | 保存记忆到文件 |
| `search_memory` | task | 向量搜索记忆 |
| `memory-governance` | task | 记忆治理 |
| `skill-creator` | task | 动态创建新技能 |
| `history_reader` | reference | 读取会话历史 |

**💡 实际场景**：用户问"帮我查一下 Python 3.12 的新特性"
1. Main Crew 看到技能清单，知道有 `baidu_search`
2. 调用 `skill_loader(skill_name="baidu_search", task_context="搜索 Python 3.12 新特性")`
3. SkillLoader 加载 `baidu_search/SKILL.md` 的详细指令
4. 构建 Sub-Crew，在沙箱里执行搜索脚本
5. 返回搜索结果给 Main Crew
6. Main Crew 整理后回复用户

---

## 六、设计优势与局限性

### 优势

1. **上下文精简**：LLM 只看到技能清单（650 字），不被实现细节干扰（9100 字）
2. **延迟加载**：技能详细说明在调用时才读取（lazy loading）
3. **跨线程安全**：ContextVar 快照保证追踪信息正确传递
4. **统一接口**：所有技能通过同一个 `skill_loader` 调用，LLM 不用记 13 个工具名
5. **可扩展**：新增技能只需加 SKILL.md + 改 load_skills.yaml，不用改代码

### 局限性

1. **子线程开销**：每次调用都要创建线程和事件循环（约 50-100ms 开销）
2. **MCP 连接延迟**：Sub-Crew 连接沙箱需要 1-2 秒
3. **超时风险**：沙箱执行长任务可能触发 5 分钟超时
4. **调试困难**：子线程出问题不易定位（需要 Langfuse 辅助）

---

## 七、❓ 常见问题

### Q1：为什么 `_run` 是同步方法，但里面又用 asyncio？

**A**：这是 CrewAI 的限制。CrewAI 在主线程同步调用 `BaseTool._run()`，但 Sub-Crew 内部需要 asyncio（要 await MCP 调用）。解决办法：
- 在 `_run` 里创建一个独立子线程
- 子线程里创建独立的 event loop
- 用 `loop.run_until_complete()` 跑异步代码
- 主线程通过 `future.result()` 等待结果

### Q2：为什么用 `ThreadPoolExecutor` 而不是 `asyncio.to_thread`？

**A**：因为到 `_run` 时，调用栈已经在 CrewAI 的 event loop 中。如果在已有 loop 里 `run_until_complete` 另一个协程会冲突（"asyncio.run() cannot be called from a running event loop"）。所以必须另起一个完全独立的子线程，子线程内自建 event loop，互不干扰。

### Q3：`copy_context()` 和 `threading.local` 有什么区别？

**A**：
- `threading.local`：每个线程独立的变量，**不能跨线程传递**
- `copy_context()`：复制当前线程的所有 ContextVar 快照，**可以传给子线程**
- ContextVar 还支持 asyncio 协程隔离（同一线程的不同协程有不同值）

### Q4：为什么要在子线程里"部分重置"ContextVar？

**A**：因为有些 ContextVar 是"应该共享的"（如 adapter、trace_id），有些是"当前线程瞬时状态"（如 span 栈、gen ID）。
- 共享的：让 Sub-Crew 的 Hook 能找到同一个 adapter，trace 挂在同一棵树
- 重置的：子线程的 span push/pop 不应污染主线程栈；子线程没有未关闭的 gen

### Q5：SKILL.md 里的 `{skill_base}` 是怎么被替换的？

**A**：在 `_get_skill_instructions` 方法里：
- 读 SKILL.md 原文
- `instructions.replace("{skill_base}", skill_base)` 替换占位符
- `skill_base` 根据是否有沙箱决定：有沙箱用 `/mnt/skills/技能名`，无沙箱用宿主机路径
- 还有 `{session_id}`、`{session_dir}` 等占位符

### Q6：技能调用失败了怎么办？

**A**：分情况：
- **技能不存在**：`_run` 开头校验，返回错误信息 `"错误：未找到 Skill 'xxx'"`
- **沙箱连接失败**：`build_skill_crew` 会抛 `ValueError`，被 CrewAI 当工具失败处理
- **执行超时**：`future.result(timeout=300)` 抛 `TimeoutError`，CrewAI 终止
- **MCP 清理超时**：记日志但不影响结果（non-fatal）

### Q7：`_instruction_cache` 会不会导致修改 SKILL.md 不生效？

**A**：会！`_instruction_cache` 是实例级别的，一个 `SkillLoaderTool` 实例的缓存只在该实例生命周期内有效。但每次创建 MainCrew 都会新建 `SkillLoaderTool`，所以下次对话会重新读 SKILL.md。如果热更新需要立即生效，重启服务即可。

### Q8：为什么 `history_reader` 是 reference 类型？

**A**：因为 `history_reader` 不需要执行代码，只需要返回历史消息。它直接读 `self._history_all`（MainCrew 传入的历史列表），不需要启动沙箱。如果设成 `task` 类型，会白白启动一次 Sub-Crew，浪费 1-2 秒。

---

## 八、🔧 调试技巧

### 8.1 查看构建的技能清单

```python
# 临时打印 SkillLoaderTool 的 description
from xiaopaw.tools.skill_loader import SkillLoaderTool

tool = SkillLoaderTool(session_id="test-session")
print(tool.description)
# 应该看到 <available_skills> XML 清单
```

### 8.2 查看某个技能的完整指令

```python
tool = SkillLoaderTool(session_id="test-session", sandbox_url="http://localhost:8030/mcp")
instructions = tool._get_skill_instructions("baidu_search")
print(instructions)
# 应该看到 SKILL.md 正文 + 替换后的路径 + sandbox_directive
```

### 8.3 检查技能注册表

```python
print(tool._skill_registry)
# {'baidu_search': {'type': 'task', 'path': 'baidu_search', 'dir': PosixPath(...)}, ...}
```

### 8.4 调试 ContextVar 传递

在子线程里加日志：

```python
def _run_with_cleanup():
    from xiaopaw.hook_framework.crew_adapter import get_current_adapter
    adapter = get_current_adapter()
    logger.debug(f"子线程 adapter: {adapter}")  # 应该不是 None
    # ... 后续代码
```

### 8.5 CrewAI 常见报错与解决

| 报错 | 原因 | 解决 |
|------|------|------|
| `Tool not found: skill_loader` | Main Crew 没挂载 SkillLoaderTool | 检查 `tools=[skill_tool, ...]` |
| `KeyError: 'baidu_search'` | 技能没在 load_skills.yaml 注册 | 检查 YAML 配置 |
| `FileNotFoundError: SKILL.md` | 技能目录没有 SKILL.md | 创建 SKILL.md 文件 |
| `TimeoutError` after 300s | Sub-Crew 执行超时 | 检查沙箱是否正常 / 脚本是否死循环 |
| `RuntimeError: asyncio.run() cannot be called` | 在已有 loop 里 run | 确保用 ThreadPoolExecutor 子线程 |
| `ContextVar 不可见` | 没用 copy_context | 确认 `pool.submit(ctx.run, fn)` |
| `invalid session_id` | session_id 含特殊字符 | 只用字母数字下划线横线 |

### 8.6 用 Langfuse 查看 trace 树

配置 Langfuse 后，可以看到完整的 trace 树：
- 父 span：`tool-skill_baidu_search`（主线程的 skill_loader 调用）
- 子 span：`gen-llm-1`（Sub-Crew 的第一次 LLM 调用）
- 子 span：`tool-sandbox_execute_command`（Sub-Crew 调沙箱工具）

如果子 span 没挂在父 span 下，说明 ContextVar 传递有问题。

---

## 九、验证你的理解

- [ ] 渐进式披露解决什么问题？相比把所有技能细节放进 prompt 有什么优势？
- [ ] `SkillLoaderTool._build_description` 方法做了什么？构建的 XML 长什么样？
- [ ] `reference` 类型和 `task` 类型的技能有什么区别？
- [ ] 为什么要用 `copy_context()` 跨线程传递 ContextVar？
- [ ] `_run` 方法里的 7 个步骤分别是什么？能画出来吗？
- [ ] 为什么用 `ThreadPoolExecutor` 而不是 `asyncio.to_thread`？
- [ ] 为什么要在子线程里"部分重置"ContextVar？哪些重置哪些共享？
- [ ] `{skill_base}` 占位符是怎么被替换的？

---

## 十、下一步

理解了 SkillLoader 后，下一篇我们会讲 Sub-Crew 是怎么在沙箱里执行技能的——包括 MCP 协议、Docker 沙箱的隔离原理、以及那个让无数人踩坑的 inode 问题。

> 下一篇：[08-第二层Agent-SubCrew与沙箱](./08-第二层Agent-SubCrew与沙箱.md)
