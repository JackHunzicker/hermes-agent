"""Telegram ForceReply input with exact prompt correlation and no chat-wide capture."""

import asyncio
import re

from gateway.native_reply_input import NativeReplySubmission, ReplyInput, prompt_token, text
from gateway.platforms.base import MessageType, SendResult

_REFERENCE = re.compile(r"\[reply:([0-9a-f]{16})\]$")


async def send_reply_input(adapter, event, title, on_reply):
    from telegram import ForceReply, ReplyParameters
    from .adapter import normalize_telegram_chat_id

    request = ReplyInput(adapter, on_reply)
    source = event.source
    bot_id = getattr(adapter._bot, "id", None)
    if not bot_id or not event.message_id or source.chat_type == "channel":
        return None, SendResult(success=False, error=text("unavailable"))
    request.chat_id, request.bot_id = str(source.chat_id), str(bot_id)
    await asyncio.to_thread(request.register)
    metadata = adapter.gateway_runner._thread_metadata_for_source(source, event.message_id)
    try:
        # Explicit reply is functional input targeting, independent of cosmetic reply_to_mode.
        msg = await adapter._bot.send_message(
            chat_id=normalize_telegram_chat_id(source.chat_id),
            text=f"{title}\n\n{text('prompt')}\n\n[reply:{request.token}]",
            parse_mode=None,
            reply_parameters=ReplyParameters(message_id=int(event.message_id), allow_sending_without_reply=False),
            reply_markup=ForceReply(selective=True, input_field_placeholder=text("placeholder")[:64]),
            **adapter._thread_kwargs_for_send(
                source.chat_id, source.thread_id, metadata,
                reply_to_message_id=int(event.message_id),
            ),
        )
        await asyncio.to_thread(request.bind_prompt, msg.message_id)
        return request, SendResult(success=True, message_id=request.prompt_id)
    except Exception:
        # A timeout may have delivered the prompt. Its reference remains recognizable, but
        # without the returned message ID no reply can be submitted or enter ordinary chat.
        await asyncio.to_thread(request.cancel)
        return None, SendResult(success=False, error=text("unavailable"))


async def dispatch_reply_input(adapter, message, update_id):
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return False
    marker = _REFERENCE.search(str(getattr(reply, "text", None) or ""))
    author = getattr(reply, "from_user", None)
    own_bot_reply = (
        getattr(author, "id", None) == getattr(adapter._bot, "id", None)
        and getattr(author, "is_bot", None) is True
    )
    token = marker[1] if marker else None
    if own_bot_reply:
        try:
            token = await asyncio.to_thread(
                prompt_token, adapter, adapter._bot.id, message.chat.id, reply.message_id,
            ) or token
        except Exception:
            # If prompt receipts cannot be read, do not risk turning a Group Send into a Bot turn.
            token = token or "unavailable"
    if token is None:
        return False
    request = getattr(adapter, "_native_reply_inputs", {}).get(token)
    # A copied marker from another author is not a native prompt. A known live
    # prompt copied to another message is recognized only to refuse it.
    if not own_bot_reply and request is None:
        return False
    kind = MessageType.TEXT if getattr(message, "text", None) else adapter._media_message_type(message)
    event = adapter._build_message_event(message, kind, update_id=update_id)
    event.text = message.text or ""
    # Real commands retain their ordinary control semantics, including emergency stops.
    command = event.get_command()
    if command:
        from hermes_cli.commands import resolve_command
        if resolve_command(command) is not None:
            return False
    if not adapter._should_process_message(message):
        return True
    if getattr(message, "caption", None) or event.message_type != MessageType.TEXT:
        event.source.message_had_attachments = True
    valid = own_bot_reply and (request is None or request.bot_id == str(adapter._bot.id))
    event._native_reply_submission = NativeReplySubmission(adapter, token, valid)
    # Bypass BOTH normal session guards via the existing profile-scoped handler. Do not
    # acquire/release an agent turn or send this through client-split text batching.
    await adapter._dispatch_inline_reply(event)
    return True
