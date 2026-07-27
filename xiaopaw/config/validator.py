"""Pydantic configuration schemas with startup validation."""

from __future__ import annotations

import os
# ↑ os 模块用于访问环境变量
import re
# ↑ re 模块用于正则表达式匹配
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
# ↑ python-dotenv 用于从 .env 文件加载环境变量到 os.environ
from pydantic import BaseModel, Field

from xiaopaw.config.flags import FeatureFlags


_ENV_PATTERN = re.compile(r"\$\{(\w+)}")
# ↑ 编译正则：匹配 ${字母数字下划线} 模式
# ↑ \w+ 表示一个或多个字母数字下划线
# ↑ 括号 () 是捕获组，提取变量名


def expand_env_vars(value: str) -> str:
    """把 ${ENV_VAR} 替换为环境变量的值。

    参数:
        value: 可能包含 ${ENV_VAR} 的字符串

    返回:
        替换后的字符串

    异常:
        RuntimeError: 如果字符串中引用的环境变量未设置
    """

    def replacer(match: re.Match) -> str:
        # ↑ 内部函数，处理每个匹配
        env_name = match.group(1)
        # ↑ match.group(1) 取捕获组内容（变量名）
        if env_name not in os.environ:
            # ↑ 检查该环境变量是否已设置
            raise RuntimeError(
                f"配置文件中引用了未设置的环境变量: ${{{env_name}}}"
            )
            # ↑ 未设置时直接报错，提示用户需要设置哪个变量
        return os.environ[env_name]
        # ↑ os.environ[env_name] 读取环境变量的值

    return _ENV_PATTERN.sub(replacer, value)
    # ↑ sub() 用 replacer 函数替换所有匹配


def _expand_env_in_data(data: Any) -> Any:
    """递归地把字典 / 列表中所有字符串里的 ${ENV_VAR} 替换为环境变量值。

    参数:
        data: 从 YAML 解析出来的任意数据，可能是字典、列表、字符串或其他类型

    返回:
        替换后的同类数据结构
    """
    if isinstance(data, dict):
        # ↑ 如果是字典，对每个键值对递归处理
        return {key: _expand_env_in_data(value) for key, value in data.items()}
    if isinstance(data, list):
        # ↑ 如果是列表，对每个元素递归处理
        return [_expand_env_in_data(item) for item in data]
    if isinstance(data, str):
        # ↑ 如果是字符串，替换其中的环境变量占位符
        return expand_env_vars(data)
    # ↑ 其他类型（数字、布尔值、None 等）直接返回，不处理
    return data


class FeishuConfig(BaseModel):
    app_id: str = Field(min_length=8)
    app_secret: str = Field(min_length=8)
    allowed_chats: list[str] = Field(default_factory=list)

class BaiduConfig(BaseModel):
    # enabled: bool = False
    api_key: str = Field(min_length=8)


class AgentConfig(BaseModel):
    model: str = "qwen3-max"
    max_iter: int = Field(default=50, ge=1, le=200)
    max_input_tokens: int = Field(default=30000, ge=1000, le=128000)
    sub_agent_model: str = "qwen3-max"
    sub_agent_max_iter: int = Field(default=20, ge=1, le=100)
    timeout_s: int = Field(default=300, ge=30, le=3600)
    llm_timeout_s: int = Field(default=120, ge=10, le=600)


class SandboxConfig(BaseModel):
    url: str = "http://localhost:8030/mcp"
    timeout_s: int = Field(default=120, ge=10, le=600)


class MemoryConfig(BaseModel):
    db_dsn: str = ""
    hard_limit_lines: int = 250
    max_save_length: int = 2000
    compress_threshold: float = 0.45
    context_window_tokens: int = 32000
    fresh_keep_turns: int = 10


class SessionConfig(BaseModel):
    max_active_sessions: int = Field(default=1000, ge=1)
    max_history_turns: int = Field(default=20, ge=1)


class RunnerConfig(BaseModel):
    max_queue_size: int = Field(default=10, ge=1, le=100)
    idle_timeout_s: float = Field(default=300.0, ge=10)


class SenderConfig(BaseModel):
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_backoff: list[float] = Field(default_factory=lambda: [1.0, 2.0, 4.0])
    max_concurrent: int = Field(default=5, ge=1, le=20)


class DebugConfig(BaseModel):
    enable_test_api: bool = False
    test_api_host: str = "127.0.0.1"
    test_api_port: int = Field(default=9090, ge=1024, le=65535)
    test_api_token: str = ""


class ObservabilityConfig(BaseModel):
    metrics_host: str = "0.0.0.0"
    metrics_port: int = Field(default=8090, ge=1024, le=65535)
    log_json: bool = True
    langfuse_host: str = "http://localhost:3000"
    # ↑ Langfuse 服务地址，对应环境变量 LANGFUSE_BASE_URL 或 XIAOPAW_LANGFUSE_BASE_URL
    langfuse_public_key: str = ""
    # ↑ Langfuse 公钥，通常从环境变量读取
    langfuse_secret_key: str = ""
    # ↑ Langfuse 密钥，通常从环境变量读取
    enable_langfuse: bool = True
    # ↑ 是否启用 Langfuse 追踪，对应环境变量 TRACE_TO_LANGFUSE


class RateLimitConfig(BaseModel):
    per_user_per_minute: int = Field(default=20, ge=1)


class ReplayCacheConfig(BaseModel):
    maxsize: int = Field(default=10000, ge=100)
    ttl_sec: float = Field(default=300.0, ge=10)


class CronConfig(BaseModel):
    enabled: bool = True
    check_interval_s: float = Field(default=30.0, ge=5)
    filelock_timeout_s: float = Field(default=10.0, ge=1)
    max_dlq_retries: int = Field(default=3, ge=0)


class CleanupConfig(BaseModel):
    enabled: bool = True
    session_ttl_days: int = Field(default=180, ge=1)
    trace_ttl_days: int = Field(default=30, ge=1)
    raw_ttl_days: int = Field(default=30, ge=1)
    run_hour_utc: int = Field(default=3, ge=0, le=23)


class AppConfig(BaseModel):
    workspace: str = "data/workspace"
    data_dir: str = "data"
    feishu: FeishuConfig = Field(default_factory=lambda: FeishuConfig(app_id="placeholder", app_secret="placeholder"))
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    sender: SenderConfig = Field(default_factory=SenderConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    replay_cache: ReplayCacheConfig = Field(default_factory=ReplayCacheConfig)
    cron: CronConfig = Field(default_factory=CronConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    baidu: BaiduConfig = Field(default_factory=BaiduConfig) 


def load_config(path: Path) -> AppConfig:
    """Load and validate configuration from YAML file."""
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    env_file = path.parent / ".env"
    # ↑ 假设 .env 文件和 config.yaml 在同一目录下
    if env_file.exists():
        # ↑ 如果存在 .env 文件，就把它加载到 os.environ
        load_dotenv(dotenv_path=env_file, override=True)
        # ↑ override=True 表示覆盖已经存在的环境变量（命令行 export 的优先）
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # ↑ 先读取 YAML 文件内容并解析成 Python 字典
    raw = _expand_env_in_data(raw)
    # ↑ 再递归替换字典中所有字符串里的 ${ENV_VAR} 环境变量占位符
    return AppConfig(**raw)
    # ↑ 最后用 Pydantic 模型校验并生成配置对象
