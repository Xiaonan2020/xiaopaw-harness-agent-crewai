# XiaoPaw v2 — 飞书 AI 工作助手

> 从第 17 课骨架出发，经历四代演进：能力(L17) → 记忆(L22) → 协作(L29) → 加固(L30-33)。

XiaoPaw 是一个运行在飞书里的 AI 工作助手。你在飞书里发消息给它，它通过两层 Agent 架构理解意图、调用技能、在沙箱里执行代码，然后把结果返回给你。

```
飞书消息 → FeishuListener(WebSocket) → Runner(队列) → Main Crew(编排)
                                                          ↓
                                                    SkillLoaderTool
                                                          ↓
                                                    Sub-Crew(沙箱执行)
                                                          ↓
                                                    AIO-Sandbox(Docker/MCP)
```

> **本仓与课程的对应关系**：第 33 课《项目实战 5：系统加固》把 30/31/32 课在独立 demo（`crewai_mas_demo/m5l30~32`）里跑通的三层加固，整建制装到 XiaoPaw 上。**业务代码 0 行修改**，靠 `shared_hooks/hooks.yaml` 一份声明把 9 个策略接线启动。本仓即第 33 课交付的"穿上装甲"的 XiaoPaw。

---

## 目录

1. [快速开始](#快速开始)（5 分钟跑通本地对话）
2. [使用 TestAPI 调试（不需要飞书）](#使用-testapi-调试不需要飞书)
3. [飞书应用配置](#飞书应用配置)
4. [Langfuse 可观测（推荐）](#langfuse-可观测推荐)
5. [代码框架说明](#代码框架说明)（每个目录的职责）
6. [项目学习指南](#项目学习指南)（按 17→22→29→30→31→32→33 顺序读代码）
7. [测试](#测试) · [端口速查](#端口速查) · [技术栈](#技术栈) · [数据本地化披露](#数据本地化披露)

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker（运行沙箱容器）
- 飞书开发者账号（也可以用 TestAPI 本地调试，不强依赖飞书）
- 阿里云 DashScope API Key（Qwen3-max）

### Step 1：克隆 & 安装依赖

```bash
git clone <repo> xiaopaw-v2
cd xiaopaw-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[full,dev]"
```

### Step 2：准备配置文件

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，需要填写的最少配置：

```yaml
feishu:
  app_id: "cli_xxx"              # 飞书应用 App ID
  app_secret: "xxx"              # 飞书应用 App Secret

agent:
  model: "qwen3-max"             # 主 LLM（默认即可）

sandbox:
  url: "http://localhost:8030/mcp"  # 沙箱地址（与 compose 端口一致）
```

> 也支持通过环境变量覆盖：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`。config.yaml 中使用 `"${ENV_VAR}"` 语法引用环境变量。

### Step 3：设置 LLM API Key

```bash
export QWEN_API_KEY="sk-xxx"   # 阿里云 DashScope API Key
```

### Step 4：启动沙箱

沙箱是 Agent 执行代码的隔离环境（Docker 容器），通过 MCP 协议通信。

```bash
docker compose -f sandbox-docker-compose.yaml up -d
```

验证沙箱启动：

```bash
curl -s http://localhost:8030/ | head -3   # 返回 JSON 即正常（/healthz 不存在属正常现象）
```

**重要：workspace 目录权限**

沙箱内的 MCP 服务以 `gem` 用户运行，需要能直接写入 `/workspace/`（即宿主机的 `./data/workspace`）。
XiaoPaw 在首次启动时会自动设置权限，但如果你手动创建或恢复了 workspace 文件，需确保权限正确：

```bash
chmod -R 777 data/workspace/
chmod 666 data/workspace/*.md
```

**如果权限不对会发生什么**：memory-save Skill 无法直接写入 workspace，沙箱会尝试绕路（`sudo cp`），被 sandbox_guard 拦截后反复重试，导致每条回复耗时数分钟。

> 沙箱将 `./xiaopaw/skills` 挂载到容器 `/mnt/skills`，将 `./data/workspace` 挂载到 `/workspace`。这两个目录必须存在。

**⚠️ 不要 `rm -rf data/workspace/`（典型坑）**

清空 workspace 时，**只删 contents、保留目录本身**：

```bash
# ✅ 正确：只清内容，目录 inode 不变
rm -rf data/workspace/* data/workspace/.[!.]*

# ❌ 错误：会破坏 docker bind mount
rm -rf data/workspace && mkdir data/workspace
```

原理：Docker bind mount 绑定的是 host 目录的 **inode**。删掉目录再 mkdir 会创建新 inode，但容器里的 `/workspace` 仍指向旧（已删除）inode —— host 写文件容器看不到，反之亦然。症状：MCP 工具调用挂死、memory-save 写入丢失、e2e 测试卡在 "MCP Connection Started" 不动。

如果已经 `rm -rf` 了，用以下命令修复：

```bash
docker compose -f sandbox-docker-compose.yaml restart

# 验证 host / container inode 一致
stat -c "host=%i" data/workspace
docker exec xiaopaw-v2-aio-sandbox-1 stat -c "container=%i" /workspace
# 两个数字必须相同，否则 mount 还是坏的
```

### Step 5：启动 pgvector（可选，记忆搜索需要）

```
docker compose -f pgvector-docker-compose.yaml up -d
```

如果只是体验基本对话，可以跳过这步。需要第 22 课的"三层记忆"中向量搜索功能时再启动。

```bash
# 方式 1：使用已有的 PostgreSQL（需安装 pgvector 扩展）
export MEMORY_DB_DSN="postgresql://user:pass@localhost:5432/xiaopaw"
psql "$MEMORY_DB_DSN" -f schema.sql

# 方式 2：用 Docker 快速启动
docker run -d --name pgvector \
  -e POSTGRES_USER=xiaopaw -e POSTGRES_PASSWORD=xiaopaw -e POSTGRES_DB=xiaopaw \
  -p 5432:5432 pgvector/pgvector:pg16

sleep 3
export MEMORY_DB_DSN="postgresql://xiaopaw:xiaopaw@localhost:5432/xiaopaw"
psql "$MEMORY_DB_DSN" -f schema.sql
```

然后在 `config.yaml` 中填入：

```yaml
memory:
  db_dsn: "postgresql://xiaopaw:xiaopaw@localhost:5432/xiaopaw"
```

### Step 6：启动 XiaoPaw

```bash
# 开发模式（启动 TestAPI + CaptureSender 模式，不依赖真实飞书）
export XIAOPAW_ENV=dev
python -m xiaopaw.main
```

启动成功后会看到：

```
feishu websocket listener started
test api started on 127.0.0.1:9090
metrics server started on 0.0.0.0:8090
```

### Step 7：验证

```bash
# Metrics 端点
curl http://127.0.0.1:8090/metrics

# 通过 TestAPI 发测试消息（开发模式）
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XIAOPAW_TESTAPI_TOKEN" \
  -d '{"routing_key": "p2p:ou_test", "text": "你好"}'
```

---

## 使用 TestAPI 调试（不需要飞书）

开发模式下（`XIAOPAW_ENV=dev`），XiaoPaw 会启动一个本地 TestAPI（默认 9090 端口），你可以直接发 HTTP 请求测试，不需要配置飞书应用。

```bash
# config.yaml 中启用：
# debug:
#   enable_test_api: true
#   test_api_port: 9090
#   test_api_token: "your-dev-token"

# 发送消息
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Authorization: Bearer your-dev-token" \
  -d '{"routing_key": "p2p:ou_test", "text": "帮我搜索一下 Python 3.13 有什么新特性"}'

# 查看 Agent 回复
curl http://127.0.0.1:9090/api/test/replies?routing_key=p2p:ou_test
```

---

## 飞书应用配置

要在飞书中使用 XiaoPaw，需要在飞书开发者后台创建一个机器人应用。

1. **创建应用**：[飞书开放平台](https://open.feishu.cn/) → 开发者后台 → 创建企业自建应用，复制 **App ID** 和 **App Secret**。
2. **添加机器人能力**：应用能力 → 添加机器人，填写名称和描述。
3. **配置权限**：

   | 权限 | 用途 |
   |------|------|
   | `im:message:receive_v1` | 接收消息事件 |
   | `im:message` / `im:message:send_as_bot` | 发送消息 |
   | `im:resource` | 获取消息中的图片/文件 |
   | `contact:user.base:readonly` | 获取用户基本信息 |

   如果使用飞书操作技能（`feishu_ops`），还需要 `docs:doc` / `sheets:spreadsheet` / `calendar:calendar` / `bitable:app`。

4. **启用 WebSocket 模式**：事件与回调 → 选择"使用长连接接收事件"，无需公网回调地址。
5. **发布应用**：版本管理与发布 → 创建版本 → 发布（企业内部应用需管理员审批）。
6. **使用**：飞书中搜索机器人名称发私聊；或拉入群聊后 @ 机器人。

---

## Langfuse 可观测（推荐）

本地docker拉取git，到langfuse目录中启动docker

```
git clone https://github.com/langfuse/langfuse.git
cd langfuse
# 启动所有容器（Web + Worker + Postgres + Redis + ClickHouse + MinIO）
docker compose up -d
```



XiaoPaw 集成了 Langfuse 全链路追踪，可以可视化每次对话的完整调用链（LLM 调用、工具执行、Sub-Crew 流程）。**第 33 课课文里的"Trace 树"截图就是从这里看到的。**

```bash
# Langfuse Cloud 或自托管实例：
export XIAOPAW_LANGFUSE_PUBLIC_KEY="pk-lf-..."
export XIAOPAW_LANGFUSE_SECRET_KEY="sk-lf-..."
export TRACE_TO_LANGFUSE=true
# export XIAOPAW_LANGFUSE_BASE_URL="https://your-langfuse.example.com"  # 默认 http://localhost:3000
```

> `XIAOPAW_` 前缀的环境变量优先于通用的 `LANGFUSE_PUBLIC_KEY`。当机器上有多个服务共享 Langfuse 时，用前缀可以避免 trace 写入错误的 project。

**自托管 Langfuse**：

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse && docker compose up -d
# 访问 http://localhost:3000，创建 project，获取 API Key
```

---

## 代码框架说明

```
xiaopaw-v2/
├── config.yaml.example           # 配置模板
├── sandbox-docker-compose.yaml   # 沙箱 Docker Compose（端口 8030）
├── schema.sql                    # pgvector 表结构（记忆搜索）
├── DESIGN.md                     # 设计总纲
│
├── xiaopaw/                      # 主代码（业务层）
│   ├── main.py                   #   启动入口：装配 listener / runner / 监控端点
│   ├── runner.py                 #   消息队列 + Agent 调度（33课在这里改了 +4 行接线）
│   ├── agents/                   #   Main Crew + Sub-Crew 两层 Agent 编排
│   │   ├── main_crew.py          #     主 Crew（17/22/29 课）
│   │   └── skill_crew.py         #     Sub-Crew（29 课"零编排协作"）
│   ├── tools/                    #   SkillLoaderTool（17课渐进式能力披露）
│   ├── skills/                   #   13 个技能（baidu_search/web_browse/pdf/docx/...）
│   ├── hook_framework/           #   ★ 30 课 Hook 骨架（事件分发 + YAML 加载）
│   │   ├── registry.py           #     EventType + HookContext + HookRegistry + GuardrailDeny
│   │   ├── crew_adapter.py       #     CrewAI 回调 → 5+2 事件映射 + pending_deny
│   │   └── loader.py             #     hooks + strategies + deps 三段式 YAML 加载
│   ├── memory/                   #   ★ 22 课三层记忆（Bootstrap + 文件 + pgvector）
│   ├── session/                  #   会话管理（routing_key → session 状态）
│   ├── feishu/                   #   飞书 SDK（WebSocket 监听 + 消息发送）
│   ├── llm/                      #   LLM 接入（Qwen3-max via DashScope）
│   ├── config/                   #   配置校验（Pydantic）
│   └── observability/            #   指标 / 日志 / trace
│
├── shared_hooks/                 # ★ 加固层（30-33课产出，9 个策略，1337 行，业务 0 行修改）
│   ├── hooks.yaml                #   33 课新写：两段式声明（72 行，本仓的"装甲接线图"）
│   ├── structured_log.py         #   30 课：JSON 事件日志（82 行）
│   ├── langfuse_trace.py         #   30 课：Langfuse 全链路（779 行，含 Trace 树五大机制）
│   ├── audit_logger.py           #   32 课：JSONL 审计日志（63 行，被 sandbox/permission deps 共享）
│   ├── sandbox_guard.py          #   32 课：路径穿越/Shell 注入/Prompt 注入消毒（107 行）
│   ├── permission_gate.py        #   32 课：工具权限三级控制 deny/warn/allow（75 行）
│   ├── cost_guard.py             #   31 课：$1 成本围栏（69 行）
│   ├── loop_detector.py          #   31 课：循环检测阈值 3（50 行）
│   └── retry_tracker.py          #   31 课：重试追踪最多 5 次（40 行）
│
├── workspace-init/               # 新用户 workspace 模板（22课记忆四件套：soul/user/agent/memory）
├── tests/                        # 单元 + 集成 + E2E 测试（293 用例）
│   ├── unit/                     #   188 单测（shared_hooks 106 + hook_framework 64 + v3_fixes 18）
│   ├── integration/              #   40 集成（hook_chain / security_chain / deny_flow）
│   └── e2e/                      #   65 个 E2E 用例，15 场景 + 2 persona
│
└── docs/                         # 设计文档（18 篇 + SSOT 清单）
    ├── 01-architecture.md  02-modules.md  07-security.md  08-deployment.md
    ├── 12-hook-hardening.md      #   30-33 课 Hook 加固设计总纲（v3.1-rc1）
    ├── 13-test-design-hook-hardening.md
    ├── 14-e2e-test-design.md
    ├── 15-e2e-fix-structured-log-and-timing.md
    ├── langfuse-trace-fix-design.md
    └── ssot/                     #   权威清单（锁/任务/端口/feature flags/威胁）
```

### 技能列表（SkillLoaderTool 渐进式披露）

Main Crew 不知道具体技能实现，只看到技能名称和描述，需要时调用 `skill_loader` 触发 Sub-Crew 在沙箱中执行。

| 技能 | 类型 | 说明 |
|------|------|------|
| `baidu_search` | task | 百度搜索（支持时间过滤） |
| `web_browse` | task | 网页浏览、内容提取、截图 |
| `pdf` / `docx` / `pptx` / `xlsx` | task | 文档读写 |
| `feishu_ops` | task | 飞书消息/文档/表格/日历/多维表格 |
| `scheduler_mgr` | task | 定时任务管理（cron） |
| `memory-save` / `search_memory` / `memory-governance` | task | 三层记忆操作 |
| `skill-creator` | task | 动态创建新技能 |
| `history_reader` | reference | 读取完整会话历史（分页） |

---

## 项目学习指南

本节按课程教学顺序帮你阅读代码，从 17 课的能力骨架一路读到 33 课的"装甲接线"。**每一步都标注了对应课文章节、要看的文件、和验证方式。**

### 整体架构一览

```
                飞书消息 / TestAPI 请求
                          │
                ┌─────────┴─────────┐
                │  FeishuListener   │ ← xiaopaw/feishu/
                └─────────┬─────────┘
                          │
                ┌─────────▼─────────┐
                │     Runner        │ ← xiaopaw/runner.py（33课在这里 +4 行接线）
                │ (消息队列 + 调度)  │
                └─────────┬─────────┘
                          │
                ┌─────────▼─────────┐
                │    Main Crew      │ ← xiaopaw/agents/main_crew.py（17课）
                │  + 三层记忆        │ ← xiaopaw/memory/（22课）
                └─────┬─────────┬───┘
                      │         │
            SkillLoaderTool   step_callback / task_callback
                      │         │
        ┌─────────────▼─┐    ┌──▼─────────────────────────┐
        │  Sub-Crew     │    │  CrewObservabilityAdapter   │
        │ (沙箱执行)     │    │  ↓ 翻译为 5+2 事件          │
        │  29课零编排    │    │  HookRegistry              │ ← 30课 Hook 骨架
        └───────┬───────┘    │  ↓ dispatch / dispatch_gate │
                │            └──┬──────────────────────────┘
                │               │
        ┌───────▼───────┐    ┌──▼──────────────────────────┐
        │  AIO-Sandbox  │    │  hooks.yaml（33课声明）      │
        │  (MCP/Docker) │    │  ├ 观测层：log + langfuse    │ 30课
        │  端口 8030    │    │  ├ 安全层：sandbox+permission│ 32课
        └───────────────┘    │  │           +audit          │
                             │  └ 可靠性：cost+loop+retry   │ 31课
                             └─────────────────────────────┘
```

### 学习路线（建议按顺序）

---

#### 第一站｜L17 能力骨架：SkillLoader 是如何"渐进式披露"的

**对应课文**：第 17 课《项目实战 1：能力骨架》

**阅读文件**：

| 文件 | 看什么 |
|------|--------|
| `xiaopaw/runner.py` | 消息进入后如何排队、如何把每条消息交给一个 Crew 实例 |
| `xiaopaw/agents/main_crew.py` | Main Crew 的 Agent / Task 定义；它只看到 `skill_loader` 这一个工具 |
| `xiaopaw/tools/skill_loader.py` | 调用时传 `skill_name`，工具内部触发对应技能的 Sub-Crew |
| `xiaopaw/skills/baidu_search/SKILL.md` | 一个 task 型技能的标准结构（YAML 元信息 + 任务说明） |

**理解要点**：Main Crew 的 prompt 里**没有具体技能的实现细节**，只有一份"技能清单 + 描述"。这是为了控制上下文长度和让 LLM 选择更确定。

**验证**：
```bash
pytest tests/unit/agents/test_main_crew.py -v
```

---

#### 第二站｜L22 三层记忆：Bootstrap + 文件 + pgvector

**对应课文**：第 22 课《项目实战 2：长期记忆》 + `DESIGN.md` 的"记忆"章节

**阅读文件**：

| 文件 | 看什么 |
|------|--------|
| `xiaopaw/memory/__init__.py` | 三层记忆的入口：bootstrap / file / vector |
| `xiaopaw/memory/bootstrap.py` | session 启动时把 `soul.md / user.md / agent.md / memory.md` 注入 prompt |
| `xiaopaw/memory/file_memory.py` | 文件级记忆（workspace 下的 `*.md`） |
| `xiaopaw/memory/vector_memory.py` | pgvector 嵌入与检索（需 `MEMORY_DB_DSN`） |
| `workspace-init/` | 新用户 workspace 模板：四件套结构 |
| `schema.sql` | pgvector 表结构 |

**理解要点**：

- **Bootstrap 是同步加载的"角色记忆"**——总是出现在每次 prompt 头部
- **文件记忆是 LLM 显式工具调用**（`memory-save`）触发的
- **向量搜索是 `search_memory` 技能**——延迟检索，按相似度返回片段

**验证**：
```bash
pytest tests/unit/memory/ -v
# 集成测试需要 pgvector：
pytest tests/integration/memory/ -v -m pgvector_required
```

---

#### 第三站｜L29 零编排协作：Sub-Crew 如何接续上下文

**对应课文**：第 29 课《零编排架构》

**阅读文件**：

| 文件 | 看什么 |
|------|--------|
| `xiaopaw/agents/skill_crew.py` | Sub-Crew 的构建：从 SKILL.md 提取 Agent / Task 定义 |
| `xiaopaw/tools/skill_loader.py` | 父 Crew 怎么把 routing_key / session_id / parent_span 透传给 Sub-Crew |
| `docs/01-architecture.md` 第 4 节 | 四接缝（输入 / 工具 / 输出 / 记忆）和六阶段端到端流程 |

**理解要点**：Sub-Crew **运行在 ThreadPoolExecutor 的子线程**。这个细节在 33 课"Trace 树机制二"里至关重要：靠 `copy_context()` 把 ContextVar 拷过去，trace 父子关系才能自动建立。

**验证**：
```bash
pytest tests/unit/agents/test_skill_crew.py -v
pytest tests/e2e/test_e2e_05_search.py -v   # 调用 baidu_search → Sub-Crew 沙箱执行的端到端
```

---

#### 第四站｜L30 Hook 骨架：HookRegistry 的两套分发机制

**对应课文**：第 33 课"三、核心架构：HookRegistry 的两套机制"（设计源自第 30 课）

**阅读文件**：`xiaopaw/hook_framework/registry.py`

| 重点区域 | 看什么 |
|---------|--------|
| `EventType` 枚举 | 5+2 事件：BEFORE_TURN / BEFORE_LLM / BEFORE_TOOL_CALL / AFTER_TOOL_CALL / AFTER_TURN + TASK_COMPLETE / SESSION_END |
| `HookContext` 数据类 | `frozen=True` + `tool_input` 转 `MappingProxyType`——**Handler 只读不可改** |
| `dispatch()` 方法 | "报警器模式"：所有异常被吞，观测层用它（崩了不影响业务） |
| `dispatch_gate()` 方法 | "保险丝模式"：只有 `GuardrailDeny` 能穿透，首次 deny 立即中止链路 |
| `fail_closed` 参数 | 安全 handler 自己崩溃时也算 deny（"安全组件坏了 = 默认拒绝"） |

**配套文件**：

- `xiaopaw/hook_framework/crew_adapter.py`：把 CrewAI 的 `@before_tool_use` / `step_callback` / `task_callback` 翻译成 5+2 事件，并实现 **`pending_deny`** 模式（CrewAI 会吞 `BEFORE_TOOL_CALL` 抛出的异常，需要在 `step_callback` 安全出口重抛）
- `xiaopaw/hook_framework/loader.py`：YAML 三段式（hooks / strategies / deps）加载

**验证**：
```bash
pytest tests/unit/hook_framework/ -v   # 64 个单元测试
```

---

#### 第五站｜L30 观测层：Langfuse Trace 树的五个机制

**对应课文**：第 33 课"五、Trace 树：从事件到完整树形结构"

**阅读文件**：`shared_hooks/langfuse_trace.py`（779 行，是整个仓库最复杂的单文件，但也是含金量最高的）

按课文五个机制读：

| 课文机制 | 代码区域 | 看什么 |
|---------|---------|--------|
| **机制一**：多轮对话留在同一棵树 | `_get_trace_id()` | `trace_id = session_id`，利用 Langfuse 的 upsert 语义 |
| **机制二**：Sub-crew 自动挂到父 trace | `subcrew_setup()` / `subcrew_cleanup()` | `copy_context()` 复制 ContextVar 到子线程；不在子线程里重置变量 |
| **机制三**：Span 栈维护嵌套关系 | `_span_stack_var` 相关函数 | 用不可变元组模拟栈（方便 ContextVar 传播）；LIFO 匹配嵌套 |
| **机制四**：Generation 先写后更新 | `before_llm_handler()` | 跨事件关闭：下一次 BEFORE_LLM 或 AFTER_TURN 才补完上一个 generation |
| **机制五**：强制 flush 保证可见性 | `after_turn_handler()` 末尾 `_flush_batch()` | 必须在 `sender.send(reply)` 之前 |

**理解要点**：这五个机制都是被坑出来的——`docs/langfuse-trace-fix-design.md` 记录了 P1-P8 八个具体问题与修复设计。读完代码后回看那篇设计文档，能看到每个机制都对应一个真实生产事故。

**验证**：
```bash
pytest tests/unit/shared_hooks/test_langfuse_trace.py -v
# 真实 Langfuse 验证：
export TRACE_TO_LANGFUSE=true
pytest tests/e2e/test_e2e_03_trace_completeness.py -v
```

---

#### 第六站｜L31 可靠性策略：cost / loop / retry

**对应课文**：第 31 课《项目实战 3：dispatch_gate + pending_deny + 双路径 + 三策略》

**阅读文件**：

| 文件 | 看什么 | 挂载事件 |
|------|--------|---------|
| `shared_hooks/cost_guard.py` | 实时累计 token 成本，超 $1 抛 GuardrailDeny | BEFORE_TOOL_CALL（拦截）+ AFTER_TURN（算账） |
| `shared_hooks/loop_detector.py` | MD5 哈希去重连续 3 次相同则 deny | AFTER_TOOL_CALL + AFTER_TURN |
| `shared_hooks/retry_tracker.py` | 纯观测，只打 WARNING 不 deny | AFTER_TOOL_CALL |

**验证**：
```bash
pytest tests/unit/shared_hooks/test_cost_guard.py tests/unit/shared_hooks/test_loop_detector.py -v
```

---

#### 第七站｜L32 安全层：sandbox / permission / audit + deps 共享

**对应课文**：第 32 课《项目实战 4：三层安全——沙箱 + 权限网关 + 身份认证》

**阅读文件**：

| 文件 | 看什么 |
|------|--------|
| `shared_hooks/sandbox_guard.py` | 4 组正则（路径穿越 / 危险命令 / Shell 注入 / Prompt 注入）+ NFKC 归一化 + 迭代 URL 解码 |
| `shared_hooks/permission_gate.py` | YAML 工具权限矩阵 deny/warn/allow，按 routing_key 判调用方 |
| `shared_hooks/audit_logger.py` | append-only JSONL 审计；SESSION_END 写本次会话安全摘要 |

**`deps` 注入机制**：sandbox_guard 和 permission_gate 共享同一个 audit_logger 实例。详见 `xiaopaw/hook_framework/loader.py` 的 strategies 段处理逻辑。

**验证**：
```bash
pytest tests/unit/shared_hooks/test_sandbox_guard.py \
       tests/unit/shared_hooks/test_permission_gate.py \
       tests/unit/shared_hooks/test_audit_logger.py -v
```

---

#### 第八站｜L33 接线启动：读懂 hooks.yaml 的三条执行顺序约束 ★★★

**对应课文**：第 33 课全篇——这是本仓库的"封顶仪式"

**阅读文件**：`shared_hooks/hooks.yaml`（72 行，是全仓库信息密度最高的文件）

打开 `hooks.yaml`，对照课文第四节"声明顺序就是执行顺序"，逐条核对：

**约束一：观测段必须整体先于策略段**

```yaml
hooks:                          # ← 上半段
  BEFORE_TOOL_CALL:
    - structured_log...         # 1
    - langfuse_trace...         # 2

strategies:                     # ← 下半段
  - sandbox_guard               # 3 (即使这里 deny，1+2 已执行)
  - permission_gate             # 4
```

强制在 `xiaopaw/hook_framework/loader.py` 的 `load_from_directory()`：先 `_load_hooks_section()` 后 `_load_strategies_section()`。

**约束二：audit_logger 必须在 strategies 段中排第一**

```yaml
strategies:
  - name: audit_logger          # ← 必须最先（被后面的 deps 引用）
  - name: sandbox_guard
    deps: { audit: audit_logger }
  - name: permission_gate
    deps: { audit: audit_logger }
```

不按这个顺序的后果（课文 4.2）：`SandboxGuard.__init__(audit=None)`，运行时 `AttributeError`，而 `fail_closed=True` 会把所有请求都拒掉——系统完全瘫痪。

**约束三：cost_guard 必须先于 loop_detector（AFTER_TURN）**

```yaml
strategies:
  - name: cost_guard            # AFTER_TURN 算账
    hooks: { AFTER_TURN: after_turn_handler }
  - name: loop_detector         # AFTER_TURN 检测（可能 deny）
    hooks: { AFTER_TURN: after_turn_handler }
```

不按这个顺序的后果：循环场景是高消耗场景，但 loop_detector 先 deny 会让 cost_guard 永远算不到账，预算严重偏低。

**33 课的接线点**：

| 文件 | 改动 | 看什么 |
|------|------|--------|
| `xiaopaw/runner.py` | +4 行 | 创建 adapter → pre-flight 检查 → catch GuardrailDeny → cleanup 触发 SESSION_END |
| `xiaopaw/agents/main_crew.py` | +2 处 | 把 adapter 传给 CrewAI 的 `step_callback` 和 `task_callback` |
| `shared_hooks/hooks.yaml` | 新建 | 把 9 个 handler 声明进来 |

**总计：新增 699 行，改动 6 行，业务代码 0 行修改。**

**验证**：
```bash
# 单元 + 集成
pytest tests/unit/shared_hooks/ tests/integration/ -v
# 完整 E2E（需 LLM + Sandbox + Langfuse）
export QWEN_API_KEY=xxx TRACE_TO_LANGFUSE=true
pytest tests/e2e/ -v
```

---

### 学习检查清单

完成八站后，你应该能回答：

- [ ] `dispatch()` 和 `dispatch_gate()` 的本质区别是什么？为什么观测层用前者、策略层用后者？
- [ ] `HookContext` 为什么要 `frozen=True`，`tool_input` 为什么要 `MappingProxyType`？
- [ ] `pending_deny` 解决了 CrewAI 的什么问题？
- [ ] `trace_id = session_id` 的设计带来了什么便利？局限性是什么？
- [ ] Sub-crew 在子线程运行时，trace 父子关系是怎么自动建立的？（提示：`copy_context()`）
- [ ] **如果把 `audit_logger` 移到 `sandbox_guard` 后面，系统会发生什么？为什么 HookLoader 的 WARNING 不足以提醒你？**
- [ ] 为什么 `cost_guard` 必须在 `loop_detector` 之前？
- [ ] 为什么 `_flush_batch()` 必须在 `sender.send()` 之前？
- [ ] **L33 改了业务代码 0 行，靠什么实现的？请用一句话向同事解释。**

---

## 测试

```bash
# 全量单元测试（188 个）
pytest tests/unit/ -v

# 加固层（shared_hooks 106 + hook_framework 64）
pytest tests/unit/shared_hooks/ tests/unit/hook_framework/ -v

# 集成测试（40 个，需 Langfuse 实例）
pytest tests/integration/ -v

# E2E（15 场景 65 用例，需 LLM + Sandbox + Langfuse）
export QWEN_API_KEY=xxx TRACE_TO_LANGFUSE=true
pytest tests/e2e/ -v

# 代码质量
ruff check .
```

测试标记：

| 标记 | 含义 |
|------|------|
| `llm_dependent` | 需要真实 LLM API |
| `sandbox` / `sandbox_required` | 需要运行中的沙箱 |
| `pgvector_required` | 需要 pgvector 数据库 |
| `security` | 安全相关测试 |
| `e2e` | 端到端测试 |

---

## 常见坑 FAQ（最容易踩）

> 这几个坑都跟"看似成功，实际坏掉"或"5分钟挂死无报错"有关。先记住症状，遇到时直接对号入座。

### 1. memory-save 说"已记住"，但 `/new` 后召回失败

**症状**：保存阶段看到 `好的，已记住...`，但下次会话问"我是做什么的"，agent 回"不清楚"。

**根因**：`data/workspace/*.md` 文件 perms 漂移成 `644`（root 只读）。沙箱以 `gem`(UID 1000) 运行，写 `/workspace/user.md` 收 `Permission denied`，**但 LLM "创意"地 cp 出去再写到 sub-dir，最后返回成功** —— Bootstrap 只读 `/workspace/user.md`，看不到。

**自查**：
```bash
ls -la data/workspace/*.md       # 应该是 -rw-rw-rw- (666)
chmod 666 data/workspace/*.md    # 修复
```

启动 XiaoPaw 时 `xiaopaw/main.py` 会自动重置 perms，重启 XiaoPaw 通常就能自愈。memory-save SKILL.md 已加"严禁绕道"规则，新版本 LLM 遇到 `Permission denied` 会显式返回 `errcode 1003`，不再静默成功。

### 2. 测试卡在 `MCP Connection Started` 不动 5 分钟

**症状**：sub-crew 启动后只打印 `MCP Connection Started` 然后无任何输出，5 分钟后 `concurrent.futures.TimeoutError` 或测试 SocketTimeout。

**根因**（任一）：
- **Transport 不匹配**：`MCPServerSSE` 配 `/mcp`（HTTP）端点。沙箱 `/mcp` 是 Streamable HTTP，必须用 `MCPServerHTTP`。
- **URL 空串**：`MCPServerHTTP(url="")` → httpx 抛 `UnsupportedProtocol`，被 anyio TaskGroup 吞，asyncgen 关不掉，请求永远不返回。

**自查**：
```bash
# 直接测沙箱 MCP（应返回 JSON 而非 404）
curl -s -X POST http://localhost:8030/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream, application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}'
```

`xiaopaw/agents/skill_crew.py::build_skill_crew` 已加 URL 校验，配错时直接 `ValueError` 而非挂 5min。

### 3. `rm -rf data/workspace` 之后 MCP 挂死、沙箱 host 互不可见

**症状**：删了 workspace 目录再 mkdir，跑测试发现 sandbox 写的文件 host 看不到，或反之；MCP 工具调用挂死。

**根因**：Docker bind mount 绑的是 host 目录的 inode。`rm -rf <dir> && mkdir <dir>` 创建的是新 inode，容器仍指向旧（已删）inode。

**自查 + 修复**：
```bash
stat -c "%i" data/workspace                                    # host inode
docker exec xiaopaw-v2-aio-sandbox-1 stat -c "%i" /workspace   # container inode — 必须相同
docker compose -f sandbox-docker-compose.yaml restart           # 不一致就重启重建 mount
```

**正确清空姿势**：`rm -rf data/workspace/*`（清 contents，保留目录本身）。

### 4. 怎么读 Langfuse trace 时不要被假成功骗

学员看 trace 树时，**根 span output ≠ 内部全部成功**。Trace 显示 "好的已记住" 不代表真的写到 user.md。

**正确读法**：
- 检查 root span `name`、`source: xiaopaw-v2`、tree 结构
- 检查每个 tool span 的 `level`（DEFAULT vs WARNING）和 `statusMessage`
- 检查最里层 file_operations 的 output JSON 里 `success` 字段
- 跨 session 的语义检查（"我能召回吗？"）才是真正的端到端断言

---

## 端口速查

| 端口 | 服务 | 说明 |
|------|------|------|
| 8030 | AIO-Sandbox MCP | 沙箱 MCP 端点（`sandbox-docker-compose.yaml`） |
| 8090 | Prometheus Metrics | 指标端点（`/metrics`） |
| 9090 | TestAPI | 开发调试 HTTP API（仅 dev 模式） |
| 5432 | PostgreSQL | pgvector 数据库（可选） |
| 3000 | Langfuse | 可观测 UI（可选，自托管时） |

---

## 课程导航（速查表）

| 课程 | 主题 | 关注代码 | 关键设计文档 |
|------|------|---------|------------|
| 第 17 课 | 能力骨架 | `xiaopaw/runner.py` + `xiaopaw/agents/` + `xiaopaw/tools/skill_loader.py` | — |
| 第 22 课 | 三层记忆 | `xiaopaw/memory/` + `workspace-init/` | `DESIGN.md` |
| 第 29 课 | 零编排协作 | `xiaopaw/agents/skill_crew.py` | `docs/01-architecture.md` 第 4 节 |
| 第 30 课 | Hook 骨架 + 可观测 | `xiaopaw/hook_framework/` + `shared_hooks/structured_log.py` + `shared_hooks/langfuse_trace.py` | `docs/12-hook-hardening.md` |
| 第 31 课 | 可靠性策略 | `shared_hooks/{cost_guard,loop_detector,retry_tracker}.py` | `docs/12-hook-hardening.md` |
| 第 32 课 | 安全策略 | `shared_hooks/{sandbox_guard,permission_gate,audit_logger}.py` | `docs/07-security.md` |
| **第 33 课** | **系统加固接线** | **`shared_hooks/hooks.yaml` + `xiaopaw/runner.py`（+4 行）** | **`docs/12-hook-hardening.md` v3.1-rc1** |

完整设计文档列表：

| 文档 | 内容 |
|------|------|
| [DESIGN.md](DESIGN.md) | 设计总纲 |
| [docs/01-architecture.md](docs/01-architecture.md) | 架构总览、数据流、信任边界 |
| [docs/02-modules.md](docs/02-modules.md) | 模块职责和接口 |
| [docs/07-security.md](docs/07-security.md) | 安全威胁模型 |
| [docs/08-deployment.md](docs/08-deployment.md) | 部署指南 |
| [docs/12-hook-hardening.md](docs/12-hook-hardening.md) | Hook 框架 + 加固策略设计（v3.1-rc1） |
| [docs/13-test-design-hook-hardening.md](docs/13-test-design-hook-hardening.md) | 加固层测试设计（136 用例规格） |
| [docs/14-e2e-test-design.md](docs/14-e2e-test-design.md) | E2E 测试设计（15 场景 + 覆盖矩阵） |
| [docs/15-e2e-fix-structured-log-and-timing.md](docs/15-e2e-fix-structured-log-and-timing.md) | E2E 修复记录 |
| [docs/langfuse-trace-fix-design.md](docs/langfuse-trace-fix-design.md) | Langfuse trace 质量修复（8 个问题 P1-P8） |
| [docs/ssot/](docs/ssot/) | 权威清单（锁/任务/端口/feature flags/威胁） |

---

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 主语言（async/await） |
| CrewAI | >= 1.9.3 | Agent 编排 |
| lark-oapi | >= 1.3 | 飞书 SDK（WebSocket 长连接） |
| Qwen3-max | — | 主 LLM（阿里云 DashScope） |
| AIO-Sandbox | latest | MCP 执行沙盒（Docker 容器） |
| pgvector | pg16 | 记忆搜索（PostgreSQL 扩展，可选） |
| Langfuse | >= 4.0 | 可观测性（trace/generation/span，可选） |

---

## 数据本地化披露

XiaoPaw 在处理消息时，会将对话内容发送到以下外部服务：

- **阿里云 DashScope**（Qwen API）：对话内容 + embedding
- **Langfuse**（可观测）：trace 元数据（不含原始对话，可选）
- **百度千帆**（搜索技能）：搜索查询（可选）
- **飞书开放平台**：消息收发

企业部署前建议评估数据出境、商业机密保护、和数据主体权利合规要求。

---

## License
    
