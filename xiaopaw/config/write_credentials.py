import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def write_feishu_credentials(data_dir: Path, app_id: str, app_secret: str) -> None:
    """将飞书凭证写入沙盒 .config/feishu.json（凭证不经过 LLM）。

    文件权限设为 0o600（仅属主可读），目录权限设为 0o700，
    防止同宿主机其他用户或容器内非 root 进程读取凭证。
    """
    import json

    config_dir = data_dir / "workspace" / ".config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)  # 目录：仅属主可进入

    creds = {"app_id": app_id, "app_secret": app_secret}
    creds_file = config_dir / "feishu.json"

    # 原子写入：先写临时文件再 rename
    tmp_file = creds_file.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.chmod(0o600)  # 临时文件：仅属主可读写
    os.replace(tmp_file, creds_file)
    # rename 后再次设置权限（不同 OS 行为不同）
    creds_file.chmod(0o600)
    logger.info("cleanup: feishu credentials written to %s (mode 0600)", creds_file)

def write_baidu_credentials(data_dir: Path, api_key: str) -> None:
    """将百度千帆 API Key 写入沙盒 .config/baidu.json（凭证不经过 LLM）。

    文件权限设为 0o600（仅属主可读），与 feishu.json 保持一致。
    若 api_key 为空则跳过（百度搜索 Skill 不可用，但不阻断主流程）。
    """
    import json

    if not api_key:
        logger.info("cleanup: BAIDU_API_KEY 未配置，跳过写入 baidu.json")
        return

    config_dir = data_dir / "workspace" / ".config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)

    creds = {"api_key": api_key}
    creds_file = config_dir / "baidu.json"

    tmp_file = creds_file.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.chmod(0o600)
    os.replace(tmp_file, creds_file)
    creds_file.chmod(0o600)
    logger.info("cleanup: baidu credentials written to %s (mode 0600)", creds_file)