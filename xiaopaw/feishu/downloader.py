"""File and image download from Feishu to local session directory."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from lark_oapi.ws import Client

from xiaopaw.models import Attachment


logger = logging.getLogger(__name__)


class FeishuDownloader:
    def __init__(self, client: Client, data_dir: Path) -> None:
        self._client = client
        self._data_dir = data_dir

    async def download_attachment(
            self,
            msg_id: str,
            attachment: Attachment,
            session_id: str,
        ) -> Path | None:
            """下载单个附件到本地。

            Returns:
                下载成功时返回本地绝对路径，失败返回 None。
            """
            dest_dir = (
                self._data_dir / "workspace" / "sessions" / session_id / "uploads"
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            # dest_path = dest_dir / attachment.file_name

            try:
                from lark_oapi.api.im.v1 import GetMessageResourceRequest
                req = (
                    GetMessageResourceRequest.builder()
                    .message_id(msg_id)
                    .file_key(attachment.file_key)
                    .type(attachment.msg_type)
                    .build()
                )

                resp = await self._client.im.v1.message_resource.aget(req)
                if not resp.success():
                    logger.error(
                        "附件下载失败 msg_id=%s file_key=%s code=%s msg=%s",
                        msg_id,
                        attachment.file_key,
                        resp.code,
                        resp.msg,
                    )
                    return None
                
                file_name = resp.file_name or attachment.file_name
                dest_path = dest_dir / file_name
                logger.info(
                    f"========resp.file_name========={resp.file_name}========attachment.file_name======={attachment.file_name}======================"
                )
                dest_path.write_bytes(resp.file.read())
                logger.info(
                    "附件下载完成 file_key=%s -> %s (%d bytes)",
                    attachment.file_key,
                    dest_path,
                    dest_path.stat().st_size,
                )
                return dest_path

            except Exception:
                logger.exception(
                    "下载附件异常 msg_id=%s file_key=%s", msg_id, attachment.file_key
                )
                return None


def _build_attachment_message(sandbox_path: str, original_text: str) -> str:
    """构造附件下载成功后传给 Agent 的模板消息"""
    msg = (
        f"用户发来了文件，已自动保存至沙盒路径：\n`{sandbox_path}`\n"
        "请根据文件内容和用户意图完成相应处理。"
    )
    if original_text.strip():
        msg += f"\n用户备注：{original_text}"
    return msg