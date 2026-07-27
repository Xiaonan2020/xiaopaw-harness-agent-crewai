# XiaoPaw v2

基于 CrewAI 的飞书 AI 工作助手，支持多技能编排、三层记忆系统和安全加固。

## 概述

XiaoPaw 是一个运行在飞书里的 AI 工作助手。通过两层 Agent 架构理解意图、调用技能、在沙箱中执行代码，并将结果返回给用户。

**核心特性**：

- **两层 Agent 架构**：Main Crew 编排 + Sub-Crew 沙箱执行
- **三层记忆系统**：Bootstrap 角色记忆 + 文件记忆 + pgvector 向量搜索
- **安全加固**：沙箱隔离 + 权限控制 + 审计日志 + 成本围栏
- **全链路追踪**：Langfuse 集成，可视化完整调用链
- **渐进式能力披露**：SkillLoader 按需加载技能

## 架构

```
飞书消息 → FeishuListener(WebSocket) → Runner(队列) → Main Crew(编排)
                                                          │
                                                   SkillLoaderTool
                                                          │
                                                    Sub-Crew(沙箱执行)
                                                          │
                                                  AIO-Sandbox(Docker/MCP)
```

**主要组件**：

| 组件 | 说明 |
|------|------|
| `FeishuListener` | WebSocket 长连接，接收飞书消息事件 |
| `Runner` | 消息队列 + 并发调度 + 会话管理 |
| `Main Crew` | 意图识别 + 技能编排 + Hook 集成 |
| `Sub-Crew` | 技能执行（沙箱隔离） |
| `Hook Framework` | 观测层 + 安全层 + 可靠性策略 |

## 快速开始

### 前置条件

- Python 3.11+
- Docker（运行沙箱容器）
- 飞书开发者账号（可选，可用 TestAPI 本地调试）
- LLM API Key（支持 Qwen/OpenAI 等）

### 安装

```bash
git clone <repo>
cd xiaopaw-v2
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -e ".[full,dev]"
```

### 配置

```bash
# 复制配置模板
cp config.yaml.example config.yaml
cp .env.example .env
```

编辑 `.env` 填入 API Key：

```env
OPENAI_API_KEY=sk-xxx
BASE_URL=https://api.example.com/v1/
MODEL=gpt-4

FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

### 启动沙箱

```bash
docker compose -f sandbox-docker-compose.yaml up -d

# 验证
curl -s http://localhost:8030/
```

### 启动服务

```bash
# 开发模式（TestAPI）
export XIAOPAW_ENV=dev
python -m xiaopaw.main

# 生产模式（飞书）
python -m xiaopaw.main
```

### 测试消息

```bash
# TestAPI 方式（开发模式）
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-dev-token" \
  -d '{"routing_key": "p2p:ou_test", "text": "你好"}'
```

## 功能模块

### 技能系统（SkillLoader）

通过渐进式披露，Main Crew 只看到技能名称和描述，具体实现由 Sub-Crew 在沙箱中执行。

| 技能 | 类型 | 说明 |
|------|------|------|
| `baidu_search` | task | 百度搜索 |
| `web_browse` | task | 网页浏览、内容提取 |
| `pdf` / `docx` / `pptx` / `xlsx` | task | 文档读写 |
| `feishu_ops` | task | 飞书文档/表格/日历操作 |
| `scheduler_mgr` | task | 定时任务管理 |
| `memory-save` / `search_memory` | task | 三层记忆操作 |
| `skill-creator` | task | 动态创建新技能 |

### 记忆系统

- **Bootstrap 记忆**：角色定义（soul.md / user.md / agent.md）
- **文件记忆**：workspace 下的 `.md` 文件
- **向量记忆**：pgvector 语义搜索（可选）

### 安全加固

通过 Hook 框架实现：

- **观测层**：结构化日志 + Langfuse Trace
- **安全层**：沙箱隔离 + 权限网关 + 审计日志
- **可靠性**：成本围栏 + 循环检测 + 重试追踪

## 配置说明

主要配置项（`config.yaml`）：

```yaml
# LLM 配置
agent:
  model: "${MODEL}"
  max_iter: 50
  timeout_s: 300

# 沙箱配置
sandbox:
  url: "${SANDBOX_URL}"
  timeout_s: 120

# 记忆系统
memory:
  db_dsn: "${MEMORY_DB_DSN}"

# 可观测性
observability:
  enable_langfuse: "${TRACE_TO_LANGFUSE}"
  langfuse_host: "${LANGFUSE_BASE_URL}"
```

## 项目结构

```
xiaopaw-v2/
├── xiaopaw/                  # 主代码
│   ├── main.py              # 启动入口
│   ├── runner.py            # 消息调度
│   ├── agents/              # Agent 编排
│   ├── tools/               # SkillLoader 工具
│   ├── skills/              # 技能定义
│   ├── memory/              # 三层记忆
│   ├── hook_framework/      # Hook 框架
│   └── observability/       # 监控指标
│
├── shared_hooks/            # 加固策略
│   ├── hooks.yaml           # Hook 声明
│   ├── langfuse_trace.py    # 全链路追踪
│   ├── sandbox_guard.py     # 沙箱安全
│   └── permission_gate.py   # 权限控制
│
├── tests/                   # 测试
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
└── docs/                    # 文档
```

## 测试

```bash
# 单元测试
pytest tests/unit/ -v

# 集成测试
pytest tests/integration/ -v

# E2E 测试（需 LLM + Sandbox）
pytest tests/e2e/ -v
```

## 部署

### Docker Compose

```bash
# 沙箱
docker compose -f sandbox-docker-compose.yaml up -d

# pgvector（可选）
docker compose -f pgvector-docker-compose.yaml up -d

# Langfuse（可选）
git clone https://github.com/langfuse/langfuse.git
cd langfuse && docker compose up -d
```

### 生产环境

1. 配置飞书应用（App ID / Secret）
2. 设置环境变量
3. 启动沙箱和 pgvector
4. 运行 `python -m xiaopaw.main`

详见 [docs/08-deployment.md](docs/08-deployment.md)。

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 主语言（async/await） |
| CrewAI | >= 1.9.3 | Agent 编排 |
| lark-oapi | >= 1.3 | 飞书 SDK |
| AIO-Sandbox | latest | MCP 执行沙箱 |
| pgvector | pg16 | 向量搜索（可选） |
| Langfuse | >= 4.0 | 可观测性（可选） |

## 文档

- [架构设计](docs/01-architecture.md)
- [模块说明](docs/02-modules.md)
- [安全设计](docs/07-security.md)
- [部署指南](docs/08-deployment.md)
- [Hook 加固](docs/12-hook-hardening.md)

## 端口速查

| 端口 | 服务 |
|------|------|
| 8030 | AIO-Sandbox MCP |
| 8090 | Prometheus Metrics |
| 9090 | TestAPI（开发模式） |
| 5432 | PostgreSQL（可选） |
| 3000 | Langfuse（可选） |

## License

MIT