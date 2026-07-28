"""Движок эхо-рассылки: форматирование, теги-кнопки, игноры, фильтры, альбомы."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    MessageEntity,
    ReplyParameters,
)

import crypto
import textutil
from db import db
from texts import t

log = logging.getLogger("delivery")

REASON_BLOCKED = "Error_code 403"
REASON_IGNORED = "Ignored"
REASON_FILTERED = "Filtered"
REASON_ERROR = "Error"


# --------------------------------------------------------------------------- #
#                          определение типа сообщения                          #
# --------------------------------------------------------------------------- #

def message_kind(src: Message) -> str:
    if src.text is not None:
        return "text"
    if src.photo:
        return "photo"
    if src.video or src.animation or src.video_note:
        return "video"
    if src.voice:
        return "voice"
    if src.audio or src.document:
        return "doc"
    if src.sticker:
        return "sticker"
    if src.poll:
        return "poll"
    return "other"


def kind_allowed(kind: str) -> bool:
    key = f"echo_{kind}"
    if key not in db.all_settings():
        return True  # гео, контакты, кубики и прочую экзотику не ограничиваем
    return db.get_bool(key)


def media_meta_line(src: Message) -> str:
    """Метаданные только для гифок: '#id | mime | имя файла | размер'.

    Строка уходит моноширинным блоком, поэтому в Telegram копируется по клику.
    """
    gif = src.animation
    if gif is None:
        return ""

    parts: list[str] = [f"#{gif.file_unique_id}"]
    if gif.mime_type:
        parts.append(gif.mime_type)
    if gif.file_name:
        parts.append(gif.file_name)
    size = gif.file_size
    if size:
        parts.append(
            f"{size / 1048576:.2f} MB" if size >= 1048576 else f"{size / 1024:.2f} KB"
        )
    return " | ".join(parts)


# --------------------------------------------------------------------------- #
#                                   фильтры                                    #
# --------------------------------------------------------------------------- #

def filter_hit(
    rules: Sequence[tuple[str, str]], text_lower: str, media: set[str]
) -> bool:
    for kind, value in rules:
        if kind == "media":
            if value in media:
                return True
        elif kind == "regex":
            try:
                if re.search(value, text_lower, re.IGNORECASE):
                    return True
            except re.error:
                continue
        elif value.lower() in text_lower:
            return True
    return False


# --------------------------------------------------------------------------- #
#                              отправка одной копии                            #
# --------------------------------------------------------------------------- #

async def send_payload(
    bot: Bot,
    chat_id: int,
    src: Message,
    text: str,
    entities: list[MessageEntity],
    *,
    protect: bool,
    spoiler: bool,
    reply_to: Optional[int],
    markup: Optional[InlineKeyboardMarkup],
) -> int:
    reply = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to
        else None
    )
    base = dict(protect_content=protect, reply_parameters=reply, reply_markup=markup)
    caption = text or None
    cap_ents = entities or None

    if src.text is not None:
        sent = await bot.send_message(chat_id, text, entities=entities, **base)
    elif src.photo:
        sent = await bot.send_photo(
            chat_id, src.photo[-1].file_id, caption=caption,
            caption_entities=cap_ents, has_spoiler=spoiler, **base,
        )
    elif src.video:
        sent = await bot.send_video(
            chat_id, src.video.file_id, caption=caption,
            caption_entities=cap_ents, has_spoiler=spoiler, **base,
        )
    elif src.animation:
        sent = await bot.send_animation(
            chat_id, src.animation.file_id, caption=caption,
            caption_entities=cap_ents, has_spoiler=spoiler, **base,
        )
    elif src.audio:
        sent = await bot.send_audio(
            chat_id, src.audio.file_id, caption=caption, caption_entities=cap_ents, **base
        )
    elif src.voice:
        sent = await bot.send_voice(
            chat_id, src.voice.file_id, caption=caption, caption_entities=cap_ents, **base
        )
    elif src.document:
        sent = await bot.send_document(
            chat_id, src.document.file_id, caption=caption, caption_entities=cap_ents, **base
        )
    elif src.video_note:
        sent = await bot.send_video_note(chat_id, src.video_note.file_id, **base)
    elif src.sticker:
        sent = await bot.send_sticker(chat_id, src.sticker.file_id, **base)
    elif src.dice:
        sent = await bot.send_dice(chat_id, emoji=src.dice.emoji, **base)
    elif src.location:
        sent = await bot.send_location(
            chat_id, src.location.latitude, src.location.longitude, **base
        )
    elif src.contact:
        sent = await bot.send_contact(
            chat_id, src.contact.phone_number, src.contact.first_name,
            last_name=src.contact.last_name, **base,
        )
    else:
        sent = await bot.copy_message(
            chat_id, src.chat.id, src.message_id,
            protect_content=protect, reply_parameters=reply, reply_markup=markup,
        )
    return sent.message_id


def build_album(
    sources: list[Message], text: str, entities: list[MessageEntity], spoiler: bool
) -> list[Any]:
    items: list[Any] = []
    for index, src in enumerate(sources):
        caption = (text or None) if index == 0 else None
        cap_ents = entities if (index == 0 and entities) else None
        # спойлер: либо настройка, либо автор сам скрыл это медиа при отправке
        blur = spoiler or bool(getattr(src, "has_media_spoiler", False))
        if src.photo:
            items.append(
                InputMediaPhoto(
                    media=src.photo[-1].file_id, caption=caption,
                    caption_entities=cap_ents, has_spoiler=blur,
                )
            )
        elif src.video:
            items.append(
                InputMediaVideo(
                    media=src.video.file_id, caption=caption,
                    caption_entities=cap_ents, has_spoiler=blur,
                )
            )
        elif src.audio:
            items.append(
                InputMediaAudio(
                    media=src.audio.file_id, caption=caption, caption_entities=cap_ents
                )
            )
        elif src.document:
            items.append(
                InputMediaDocument(
                    media=src.document.file_id, caption=caption, caption_entities=cap_ents
                )
            )
    return items


# --------------------------------------------------------------------------- #
#                                  рассылка                                    #
# --------------------------------------------------------------------------- #


def prepare(
    author: dict[str, Any], sources: list[Message]
) -> tuple[str, list[MessageEntity], str, list[MessageEntity]]:
    """Готовит текст к отправке: стиль автора + строка метаданных гифки."""
    primary = sources[0]
    raw_text, raw_entities = textutil.source_text(primary)

    # спецсимволы и «модернизация» меняют сами символы, поэтому entities обнуляем
    styled = textutil.apply_style(
        raw_text, bool(author["specials"]), bool(author["modernize"])
    )
    if styled != raw_text:
        raw_text, raw_entities = styled, []

    suffix: str = ""
    suffix_entities: list[MessageEntity] = []
    if author["media_meta"] and len(sources) == 1:
        meta = media_meta_line(primary)
        if meta:
            suffix = f"\n\n{meta}"
            # моноширинный блок = копирование по клику
            suffix_entities = [
                MessageEntity(type="code", offset=2, length=textutil.u16len(meta))
            ]
    return raw_text, raw_entities, suffix, suffix_entities


async def deliver_self(
    bot: Bot,
    author: dict[str, Any],
    sources: list[Message],
    ref: int,
    reply_ref: Optional[int],
    markup: Optional[InlineKeyboardMarkup],
) -> list[int]:
    """Присылает автору его же сообщение в том виде, в каком его увидят все."""
    primary = sources[0]
    raw_text, raw_entities, suffix, suffix_entities = prepare(author, sources)
    text, entities = textutil.compose(
        raw_text, raw_entities, suffix=suffix, suffix_entities=suffix_entities
    )
    spoiler = bool(
        author["spoiler_out"] or getattr(primary, "has_media_spoiler", False)
    )
    reply_to = await db.copy_in_chat(reply_ref, author["id"]) if reply_ref else None

    try:
        if len(sources) > 1:
            sent = await bot.send_media_group(
                author["id"],
                build_album(sources, text, entities, spoiler),
                protect_content=bool(author["protect"]),
                reply_parameters=(
                    ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                    if reply_to else None
                ),
            )
            ids = [m.message_id for m in sent]
        else:
            ids = [
                await send_payload(
                    bot, author["id"], primary, text, entities,
                    protect=bool(author["protect"]), spoiler=spoiler,
                    reply_to=reply_to, markup=markup,
                )
            ]
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("self copy -> %s: %s", author["id"], exc)
        return []

    await db.add_copies(ref, [(author["id"], mid) for mid in ids])
    return ids


@dataclass
class Result:
    ok: int = 0
    total: int = 0
    elapsed: float = 0.0
    reasons: Counter = field(default_factory=Counter)
    floods: list[int] = field(default_factory=list)
    pairs: list[tuple[int, int]] = field(default_factory=list)


async def broadcast(
    bot: Bot,
    author: dict[str, Any],
    sources: list[Message],
    ref: int,
    reply_ref: Optional[int],
    markup: Optional[InlineKeyboardMarkup],
) -> Result:
    recipients = await db.recipients(exclude=author["id"])
    result = Result(total=len(recipients))
    if not recipients:
        return result

    primary = sources[0]
    raw_text, raw_entities, suffix, suffix_entities = prepare(author, sources)

    # кого игнорируют, у кого фильтры, кого упомянули
    ignoring = await db.ignoring_author(author["id"])
    all_filters = await db.all_filters()
    text_lower = (raw_text or "").lower()
    media = set()
    for src in sources:
        media.update(textutil.media_ids(src))

    mentioned: set[int] = set()
    names = textutil.mentions_in(raw_text, raw_entities)
    if names:
        mentioned = await db.ids_by_username_hashes(
            {crypto.fingerprint(name) for name in names}
        )

    parent = await db.message_by_ref(reply_ref) if reply_ref else None
    protect = bool(author["protect"])
    delay = db.get_float("send_delay")
    semaphore = asyncio.Semaphore(max(1, db.get_int("parallel_limit")))
    started = time.perf_counter()

    async def worker(target: dict[str, Any]) -> None:
        if target["id"] in ignoring:
            result.reasons[REASON_IGNORED] += 1
            return
        rules = all_filters.get(target["id"])
        if rules and filter_hit(rules, text_lower, media):
            result.reasons[REASON_FILTERED] += 1
            return

        marks: list[str] = []
        if parent and parent["author_id"] == target["id"]:
            marks.append(t(target["lang"], "reply_mark"))
        if target["id"] in mentioned:
            marks.append(t(target["lang"], "mention_mark"))
        prefix = " ".join(marks) + "\n" if marks else ""

        text, entities = textutil.compose(
            raw_text, raw_entities, prefix=prefix,
            suffix=suffix, suffix_entities=suffix_entities,
        )
        spoiler = bool(
            author["spoiler_out"]
            or target["spoiler_in"]
            or getattr(primary, "has_media_spoiler", False)
        )

        reply_to = None
        if parent and target["autoreply"]:
            reply_to = await db.copy_in_chat(reply_ref, target["id"])

        async with semaphore:
            if delay:
                await asyncio.sleep(delay)
            for attempt in (1, 2):
                try:
                    if len(sources) > 1:
                        sent = await bot.send_media_group(
                            target["id"],
                            build_album(sources, text, entities, spoiler),
                            protect_content=protect,
                            reply_parameters=(
                                ReplyParameters(
                                    message_id=reply_to, allow_sending_without_reply=True
                                )
                                if reply_to else None
                            ),
                        )
                        ids = [m.message_id for m in sent]
                    else:
                        ids = [
                            await send_payload(
                                bot, target["id"], primary, text, entities,
                                protect=protect, spoiler=spoiler,
                                reply_to=reply_to, markup=markup,
                            )
                        ]
                    result.ok += 1
                    result.pairs.extend((target["id"], mid) for mid in ids)
                    return
                except TelegramRetryAfter as exc:
                    result.floods.append(int(exc.retry_after))
                    if attempt == 2:
                        result.reasons[REASON_ERROR] += 1
                        return
                    await asyncio.sleep(exc.retry_after)
                except TelegramForbiddenError:
                    # заблокировал бота — остальным всё равно доставляем
                    result.reasons[REASON_BLOCKED] += 1
                    await db.update(target["id"], active=0)
                    return
                except TelegramBadRequest as exc:
                    log.warning("send -> %s: %s", target["id"], exc)
                    result.reasons[REASON_ERROR] += 1
                    return
                except Exception as exc:  # noqa: BLE001
                    log.exception("send -> %s: %s", target["id"], exc)
                    result.reasons[REASON_ERROR] += 1
                    return

    await asyncio.gather(*(worker(person) for person in recipients))
    result.elapsed = time.perf_counter() - started

    if result.pairs:
        await db.add_copies(ref, result.pairs)
    await db.mark_message(ref, recipients=result.ok)
    return result


def report_text(lang: str, result: Result) -> str:
    import timeutil

    elapsed = timeutil.human_ms(result.elapsed)
    failed = result.total - result.ok
    if failed <= 0:
        lines = [t(lang, "sent_ok", elapsed=elapsed, count=result.ok)]
    else:
        lines = [t(lang, "sent_partial", elapsed=elapsed, ok=result.ok, total=result.total)]
        for reason, count in result.reasons.most_common():
            lines.append(t(lang, "sent_failed_head", n=count, reason=reason))
    if result.floods:
        uniq = sorted(set(result.floods), reverse=True)[:5]
        lines.append(
            t(lang, "sent_flood", n=len(result.floods), waits=", ".join(map(str, uniq)))
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#                        редактирование и удаление копий                       #
# --------------------------------------------------------------------------- #

async def propagate_edit(
    bot: Bot,
    ref: int,
    author: dict[str, Any],
    raw_text: str,
    raw_entities: Optional[Sequence[MessageEntity]],
    is_text: bool,
    markup: Optional[InlineKeyboardMarkup] = None,
    self_markup: Optional[InlineKeyboardMarkup] = None,
) -> int:
    styled = textutil.apply_style(
        raw_text, bool(author["specials"]), bool(author["modernize"])
    )
    if styled != raw_text:
        raw_text, raw_entities = styled, []

    mark = "\n\n" + t(author["lang"], "edited_mark")
    text, entities = textutil.compose(raw_text, raw_entities, suffix=mark)

    async def edit_one(chat_id: int, msg_id: int, kb) -> None:
        if is_text:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id,
                entities=entities, reply_markup=kb,
            )
        else:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=text or None, caption_entities=entities or None,
                reply_markup=kb,
            )

    changed = 0
    for chat_id, msg_id in await db.copies(ref):
        kb = self_markup if (self_markup and chat_id == author["id"]) else markup
        try:
            await edit_one(chat_id, msg_id, kb)
            changed += 1
        except TelegramBadRequest:
            try:  # у сообщений из альбома кнопок быть не может
                await edit_one(chat_id, msg_id, None)
                changed += 1
            except (TelegramBadRequest, TelegramForbiddenError):
                continue
        except TelegramForbiddenError:
            continue
    await db.mark_message(ref, edited=1)
    return changed


async def propagate_delete(bot: Bot, ref: int) -> int:
    removed = 0
    for chat_id, msg_id in await db.copies(ref):
        try:
            await bot.delete_message(chat_id, msg_id)
            removed += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
    await db.drop_copies(ref)
    await db.mark_message(ref, deleted=1)
    return removed


# --------------------------------------------------------------------------- #
#                              буфер медиагрупп                                #
# --------------------------------------------------------------------------- #

class AlbumBuffer:
    """Собирает сообщения одного альбома, чтобы разослать их одной пачкой."""

    def __init__(self) -> None:
        self._groups: dict[str, list[Message]] = defaultdict(list)
        self._owners: set[str] = set()

    async def collect(self, message: Message) -> Optional[list[Message]]:
        group_id = message.media_group_id
        if not group_id:
            return [message]

        self._groups[group_id].append(message)
        if group_id in self._owners:
            return None  # этот альбом уже собирает другой апдейт

        self._owners.add(group_id)
        await asyncio.sleep(db.get_float("media_group_wait") or 1.3)
        self._owners.discard(group_id)
        batch = self._groups.pop(group_id, [])
        batch.sort(key=lambda m: m.message_id)
        return batch[:10] or None


albums = AlbumBuffer()
