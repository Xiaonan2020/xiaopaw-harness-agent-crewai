"""FeishuListener: WebSocket-based event listener."""

from __future__ import annotations

import asyncio
import logging
import json
from collections.abc import Awaitable, Callable
from typing import Any

from xiaopaw.feishu.session_key import resolve_routing_key
from xiaopaw.models import Attachment, InboundMessage
from xiaopaw.observability.metrics import record_inbound_message
from xiaopaw.observability.security import RateLimiter, ReplayCache
from xiaopaw.observability.trace import new_trace_id

logger = logging.getLogger(__name__)

OnMessage = Callable[[InboundMessage], Awaitable[None]]


class FeishuListener:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: OnMessage,
        replay_cache: ReplayCache,
        rate_limiter: RateLimiter,
        on_bot_added: Callable[[Any], Awaitable[None]] | None = None,
        allowed_chats: list[str] | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._replay_cache = replay_cache
        self._rate_limiter = rate_limiter
        self._on_bot_added = on_bot_added
        self._allowed_chats = set(allowed_chats) if allowed_chats else None
        self._ws_client = None
        self._main_loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        import threading

        import lark_oapi.ws.client as ws_mod
        from lark_oapi.ws.client import Client as WsClient

        self._main_loop = asyncio.get_running_loop()
        event_handler = self._build_event_handler()
        self._ws_client = WsClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
        )

        def _run_ws() -> None:
            ws_mod.loop = asyncio.new_event_loop()
            self._ws_client.start()

        t = threading.Thread(target=_run_ws, daemon=True)
        t.start()
        logger.info("feishu websocket listener started (thread=%s)", t.name)

    async def stop(self) -> None:
        if self._ws_client:
            logger.info("feishu websocket listener stopped")

    def _build_event_handler(self):
        import lark_oapi as lark

        handler = lark.EventDispatcherHandler.builder("", "")

        def _on_p2p_receive(data) -> None:
            asyncio.run_coroutine_threadsafe(
                self._handle_message_event(data), self._main_loop
            )

        handler.register_p2_im_message_receive_v1(_on_p2p_receive)
        return handler.build()

    async def _handle_message_event(self, data) -> None:
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            event_id = getattr(data.header, "event_id", "") if data.header else ""
            if event_id and await self._replay_cache.seen(event_id):
                logger.debug("replay blocked: %s", event_id)
                return

            open_id = sender.sender_id.open_id if sender.sender_id else ""
            if not self._rate_limiter.allow(open_id):
                logger.warning("rate limited: %s", open_id)
                return

            chat_id = msg.chat_id or ""
            chat_type = msg.chat_type or "p2p"
            thread_id = getattr(msg, "thread_id", "") or ""

            if self._allowed_chats and chat_type != "p2p":
                if chat_id not in self._allowed_chats:
                    logger.debug("chat not in allowed list: %s", chat_id)
                    return

            routing_key = resolve_routing_key(chat_type, chat_id, open_id, thread_id)

            content_raw = msg.content or "{}"
            import json
            content_dict = json.loads(content_raw)
            # text = content_dict.get("text", "")
            text = self._extract_content(msg.message_type, content_dict)

            attachment = None
            if msg.message_type == "image":
                image_key = content_dict.get("image_key", "")
                if image_key:
                    attachment = Attachment(
                        msg_type="image", file_key=image_key, file_name=f"image_{image_key}.png"
                    )
            elif msg.message_type == "file":
                file_key = content_dict.get("file_key", "")
                file_name = content_dict.get("file_name", "")
                if file_key:
                    attachment = Attachment(
                        msg_type="file", file_key=file_key, file_name=file_name
                    )

            inbound = InboundMessage(
                routing_key=routing_key,
                content=text,
                msg_id=msg.message_id or "",
                root_id=msg.root_id or "",
                sender_id=open_id,
                ts=int(msg.create_time or "0"),
                attachment=attachment,
                trace_id=new_trace_id(),
            )

            record_inbound_message("feishu", routing_key,
            has_attachment=attachment is not None)

            await self._on_message(inbound)

        except Exception:
            logger.exception("failed to handle feishu message event")

    @staticmethod
    def _extract_post_text(content_dict: dict) -> str:
        """从 post 消息的 content dict 中提取纯文本。

        飞书 post 消息结构::

            {
              "zh_cn": {
                "title": "标题（可选）",
                "content": [
                  [{"tag": "text", "text": "第一段"}, {"tag": "a", ...}],
                  [{"tag": "text", "text": "第二段"}]
                ]
              }
            }

        提取逻辑：
        - 优先取 zh_cn，不存在时取根对象
        - 提取所有 tag == "text" 的 text 字段
        - title 非空时拼接在最前面，与 content 间用换行分隔
        - 返回 .strip() 后的结果
        """
        try:
            node = content_dict.get("zh_cn") or content_dict
            title = node.get("title") or "" if isinstance(node, dict) else ""
            raw_content = node.get("content") if isinstance(node, dict) else None

            if not isinstance(raw_content, list):
                return ""

            paragraph_texts: list[str] = []
            for paragraph in raw_content:
                if not isinstance(paragraph, list):
                    continue
                words = [
                    elem.get("text", "")
                    for elem in paragraph
                    if isinstance(elem, dict) and elem.get("tag") == "text"
                ]
                paragraph_texts.append(" ".join(words))

            body = " ".join(paragraph_texts)

            if title:
                return f"{title}\n{body}".strip()
            return body.strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _extract_content(msg_type: str, content_dict: dict) -> str:
        """根据消息类型从 content dict 中提取纯文本内容."""
        if not content_dict:
            return ""

        if msg_type == "text":
            return content_dict.get("text", "")

        if msg_type == "post":
            return FeishuListener._extract_post_text(content_dict)

        # 其它类型先不做细分，统一交给上游决定如何处理
        return ""