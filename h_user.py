"""Пользовательские хендлеры: настройки, игноры, фильтры, ЛС, репорты, эхо."""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    LinkPreviewOptions,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
    ReplyParameters,
)

import config
import crypto
import delivery
import keyboards
import logs
import moderation
import textutil
import timeutil
from db import db
from texts import LANGS, RESERVED_NAMES, t

router = Router()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
NAME_RE = re.compile(r"^[^\n\r\t]{1,64}$")

# token -> (текст, entities, текстовое ли сообщение) для подтверждения правки
PENDING_EDITS: dict[str, tuple[str, list, bool]] = {}


# --------------------------------------------------------------------------- #
#                              вспомогательное                                 #
# --------------------------------------------------------------------------- #

async def sync_username(message: Message, user: dict[str, Any]) -> None:
    """Шифруем юзернейм при входе и заново — только если он реально сменился."""
    if not user["keep_username"]:
        return
    current = message.from_user.username
    digest = crypto.fingerprint(current) if current else None
    if digest == user["username_hash"]:
        return

    blob = crypto.encrypt(current or f"id{user['id']}")
    await db.update(
        user["id"],
        username_enc=blob,
        username_hash=digest,
        name_enc=crypto.encrypt(message.from_user.full_name),
    )
    key = "username_changed" if user["username_hash"] else "username_encrypted"
    user["username_enc"], user["username_hash"] = blob, digest
    await message.answer(t(user["lang"], key, blob=blob))


def echo_markup(message: Message, user: dict[str, Any], with_delete: Optional[str] = None):
    return keyboards.echo_kb(
        user,
        display_name=message.from_user.full_name,
        username=message.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
        with_delete=with_delete,
    )


async def profile_markup(user: dict[str, Any]):
    return keyboards.profile_kb(
        user,
        ignores=await db.ignore_count(user["id"]),
        pm_blocks=await db.pm_block_count(user["id"]),
    )


async def refresh_kb(call: CallbackQuery, user: dict[str, Any]) -> None:
    rows = len(call.message.reply_markup.inline_keyboard) if call.message.reply_markup else 0
    markup = (
        keyboards.tag_kb(user, moderation.is_staff(user))
        if rows <= 4
        else await profile_markup(user)
    )
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


async def autodelete_later(bot: Bot, ref: int, delay: int) -> None:
    await asyncio.sleep(delay)
    await moderation.delete_message(bot, ref)


def is_stub(record: Optional[dict]) -> bool:
    return bool(record and (record["stub"] or record["deleted"]))


async def author_of_reply(message: Message) -> tuple[Optional[dict], Optional[dict]]:
    """По реплаю возвращает (запись сообщения, автора)."""
    if not message.reply_to_message:
        return None, None
    record = await db.message_by_copy(message.chat.id, message.reply_to_message.message_id)
    if record:
        return record, await db.get_user(record["author_id"])
    peer = await db.get_pm(message.chat.id, message.reply_to_message.message_id)
    if peer:
        return None, await db.get_user(peer)
    return None, None


# --------------------------------------------------------------------------- #
#                                регистрация                                   #
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, user: dict[str, Any]):
    if not user["active"]:
        await db.update(user["id"], active=1)
    if not user["registered"]:
        await message.answer(
            t(user["lang"], "greet", name=html.escape(message.from_user.full_name)),
            reply_markup=keyboards.confirm_start_kb(),
        )
        return
    await message.answer(t(user["lang"], "start_help"))


@router.callback_query(F.data == "reg:ok")
async def cb_register(call: CallbackQuery, user: dict[str, Any]):
    if not user["registered"]:
        await db.update(user["id"], registered=1, active=1)
        await logs.event("START", f"Новый пользователь {logs.who(user)}")
        if user["keep_username"]:
            current = call.from_user.username
            blob = crypto.encrypt(current or f"id{user['id']}")
            await db.update(
                call.from_user.id,
                username_enc=blob,
                username_hash=crypto.fingerprint(current) if current else None,
                name_enc=crypto.encrypt(call.from_user.full_name),
            )
            await call.message.answer(t(user["lang"], "username_encrypted", blob=blob))
    await call.message.edit_text(t(user["lang"], "registered"))
    if db.get_bool("greet_registered"):
        await call.message.answer(t(user["lang"], "start_help"))
    await call.answer()


# --------------------------------------------------------------------------- #
#                              простые команды                                 #
# --------------------------------------------------------------------------- #

@router.message(Command("help"))
async def cmd_help(message: Message, user: dict[str, Any]):
    text = t(user["lang"], "help")
    if moderation.is_staff(user):
        text += t(user["lang"], "help_admin")
    if moderation.is_owner(user):
        text += t(user["lang"], "help_owner")
    await message.answer(text)


@router.message(Command("rules"))
async def cmd_rules(message: Message, user: dict[str, Any]):
    custom = db.get("rules_text")
    await message.answer(
        custom or t(user["lang"], "rules_default", limit=db.get_int("warn_limit") or 3)
    )


@router.message(Command("support"))
async def cmd_support(message: Message, user: dict[str, Any]):
    await message.answer(t(user["lang"], "support", owner=config.OWNER_USERNAME))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, user: dict[str, Any]):
    await message.answer(
        t(user["lang"], "privacy", hours=db.get_int("stub_hours") or 30)
    )


@router.message(Command("users"))
async def cmd_users(message: Message, user: dict[str, Any]):
    stats = await db.stats()
    today, mine = await db.daily_counts(user["id"])
    await message.answer(
        t(
            user["lang"], "users",
            active=stats["active"], afk=stats["afk"], today=today, mine=mine,
        )
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message, user: dict[str, Any], bot: Bot):
    started = time.perf_counter()
    await bot.get_me()
    await message.answer(
        t(user["lang"], "ping", ms=int((time.perf_counter() - started) * 1000))
    )


@router.message(Command("config"))
async def cmd_config(message: Message, user: dict[str, Any]):
    started = db.get_int("started_at") or int(time.time())
    await message.answer(
        t(
            user["lang"], "config",
            uptime=timeutil.uptime_line(started),
            send_delay=db.get("send_delay"),
            slowmode=db.get("slowmode"),
            parallel=db.get("parallel_limit"),
            text=keyboards.flag(db.get_bool("echo_text")),
            doc=keyboards.flag(db.get_bool("echo_doc")),
            voice=keyboards.flag(db.get_bool("echo_voice")),
            sticker=keyboards.flag(db.get_bool("echo_sticker")),
            video=keyboards.flag(db.get_bool("echo_video")),
            photo=keyboards.flag(db.get_bool("echo_photo")),
            poll=keyboards.flag(db.get_bool("echo_poll")),
        )
    )


@router.message(Command("lang"))
async def cmd_lang(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg in LANGS:
        await db.update(user["id"], lang=arg)
        await message.answer(t(arg, "lang_set", name=LANGS[arg]))
        return
    await message.answer(t(user["lang"], "lang_head"), reply_markup=keyboards.lang_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery, user: dict[str, Any]):
    code = call.data.split(":", 1)[1]
    if code in LANGS:
        await db.update(user["id"], lang=code)
        await call.message.edit_text(t(code, "lang_set", name=LANGS[code]))
    await call.answer()


# --------------------------------------------------------------------------- #
#                            профиль и настройки                               #
# --------------------------------------------------------------------------- #

@router.message(Command("profile"))
async def cmd_profile(message: Message, user: dict[str, Any]):
    lang = user["lang"]
    if moderation.is_owner(user):
        role_line = t(lang, "role_owner", prefix=moderation.prefix_of(user))
    elif user["role"] == "admin":
        role_line = t(
            lang, "role_admin",
            prefix=moderation.prefix_of(user),
            rights=await moderation.rights_summary(user),
        )
    else:
        role_line = t(lang, "role_user")

    await message.answer(
        t(
            lang, "profile",
            name=html.escape(message.from_user.full_name),
            id=user["id"],
            clock=timeutil.fmt_clock(),
            joined=timeutil.fmt_dt_long(user["joined_at"]),
            joined_ago=timeutil.ago(user["joined_at"]),
            username=user["username_enc"] or "не сохранён",
            last=timeutil.fmt_dt_short(user["last_msg_at"]) if user["last_msg_at"] else "—",
            last_ago=timeutil.ago(user["last_msg_at"]) if user["last_msg_at"] else "—",
            msgs=user["msgs"],
            role_line=role_line,
            warns=user["warns"],
            warn_limit=db.get_int("warn_limit") or 3,
            streak=user["streak"],
        ),
        reply_markup=await profile_markup(user),
    )


@router.message(Command("tag"))
async def cmd_tag(message: Message, user: dict[str, Any]):
    await message.answer(
        t(user["lang"], "tag_head"),
        reply_markup=keyboards.tag_kb(user, moderation.is_staff(user)),
    )


@router.message(Command("name"))
async def cmd_name(message: Message, user: dict[str, Any], command: CommandObject):
    limit = db.get_int("name_max") or 17
    arg = (command.args or "").strip()
    if not arg:
        await db.update(user["id"], custom_name=None)
        await message.answer(
            t(
                user["lang"], "name_head",
                name=html.escape(message.from_user.full_name), max=limit,
            )
        )
        return
    if len(arg) > limit or not NAME_RE.match(arg):
        await message.answer(t(user["lang"], "name_bad", max=limit))
        return
    if textutil.strip_specials(arg).strip().lower() in RESERVED_NAMES:
        await message.answer(t(user["lang"], "name_reserved"))
        return
    await db.update(user["id"], custom_name=arg)
    await message.answer(t(user["lang"], "name_set", name=html.escape(arg)))


@router.message(Command("protect"))
async def cmd_protect(message: Message, user: dict[str, Any]):
    value = await db.toggle(user["id"], "protect")
    await message.answer(t(user["lang"], "protect_on" if value else "protect_off"))


@router.message(Command("reaction"))
async def cmd_reaction(message: Message, user: dict[str, Any]):
    value = await db.toggle(user["id"], "reaction")
    await message.answer(t(user["lang"], "reaction_toggled", v=keyboards.flag(value)))


@router.message(Command("autodel"))
async def cmd_autodel(message: Message, user: dict[str, Any]):
    await message.answer(
        t(user["lang"], "autodel_head"), reply_markup=keyboards.autodel_kb()
    )


@router.callback_query(F.data.startswith("ad:"))
async def cb_autodel(call: CallbackQuery, user: dict[str, Any]):
    seconds = int(call.data.split(":", 1)[1])
    await db.update(user["id"], autodel=seconds)
    await call.message.edit_text(
        t(user["lang"], "autodel_set", value=timeutil.human_delta(seconds))
        if seconds else t(user["lang"], "autodel_off")
    )
    await call.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_toggle(call: CallbackQuery, user: dict[str, Any]):
    field = call.data.split(":", 1)[1]
    if field not in keyboards.ALL_TOGGLES:
        await call.answer()
        return
    user[field] = await db.toggle(user["id"], field)
    await refresh_kb(call, user)
    await call.answer(keyboards.flag(user[field]))


@router.callback_query(F.data == "open:autodel")
async def cb_open_autodel(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(
        t(user["lang"], "autodel_head"), reply_markup=keyboards.autodel_kb()
    )
    await call.answer()


@router.callback_query(F.data == "open:name")
async def cb_open_name(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(
        t(
            user["lang"], "name_head",
            name=html.escape(call.from_user.full_name),
            max=db.get_int("name_max") or 17,
        )
    )
    await call.answer()


@router.callback_query(F.data == "open:ignore")
async def cb_open_ignore(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(await ignore_list_text(user))
    await call.answer()


@router.callback_query(F.data == "open:filter")
async def cb_open_filter(call: CallbackQuery, user: dict[str, Any]):
    await call.message.answer(await filter_list_text(user))
    await call.answer()


@router.callback_query(F.data == "open:pmblock")
async def cb_open_pmblock(call: CallbackQuery, user: dict[str, Any]):
    peers = await db.pm_blocklist(user["id"])
    await call.message.answer(
        await pm_block_list_text(user),
        reply_markup=keyboards.pm_clear_kb() if peers else None,
    )
    await call.answer()


@router.callback_query(F.data == "pmclear")
async def cb_pm_clear(call: CallbackQuery, user: dict[str, Any]):
    for peer_id in await db.pm_blocklist(user["id"]):
        await db.pm_unblock(user["id"], peer_id)
    await call.message.edit_text(t(user["lang"], "pm_block_cleared"))
    await call.answer()


@router.callback_query(F.data == "close")
async def cb_close(call: CallbackQuery):
    try:
        await call.message.delete()
    except TelegramBadRequest:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# --------------------------------------------------------------------------- #
#                              архив и выход                                   #
# --------------------------------------------------------------------------- #

@router.message(Command("afk"))
async def cmd_afk(message: Message, user: dict[str, Any]):
    await db.update(user["id"], afk=1, afk_auto=0)
    await message.answer(t(user["lang"], "afk_on"))


@router.message(Command("unafk"))
async def cmd_unafk(message: Message, user: dict[str, Any]):
    await db.update(user["id"], afk=0, afk_auto=0)
    await message.answer(t(user["lang"], "afk_off"))


@router.message(Command("leave"))
async def cmd_leave(message: Message, user: dict[str, Any], command: CommandObject):
    if (command.args or "").strip() != str(user["id"]):
        await message.answer(
            t(user["lang"], "leave_confirm", id=user["id"]),
            reply_markup=keyboards.close_kb(),
        )
        return
    await db.update(user["id"], active=0, registered=0, afk=0)
    await message.answer(t(user["lang"], "leave_done"))
    await logs.event("LEAVE", logs.who(user))


# --------------------------------------------------------------------------- #
#                                  игноры                                      #
# --------------------------------------------------------------------------- #

async def ignore_list_text(user: dict[str, Any]) -> str:
    items = await db.ignore_list(user["id"])
    if not items:
        return t(user["lang"], "ignore_empty")
    listing = "\n".join(
        f"#{index} — ID {row['peer_id']}"
        for index, row in enumerate(items, 1)
    )
    return t(user["lang"], "ignore_list", n=len(items), items=listing)


@router.message(Command("ignore"))
async def cmd_ignore(message: Message, user: dict[str, Any]):
    if not message.reply_to_message:
        await message.answer(t(user["lang"], "ignore_head"))
        return
    record, target = await author_of_reply(message)
    if is_stub(record):
        await message.answer(t(user["lang"], "msg_stub"))
        return
    if not target:
        await message.answer(t(user["lang"], "target_not_found"))
        return
    if target["id"] == user["id"]:
        await message.answer(t(user["lang"], "ignore_self"))
        return
    if await db.is_ignored(user["id"], target["id"]):
        await message.answer(t(user["lang"], "ignore_exists"))
        return
    await db.ignore_add(user["id"], target["id"])
    await message.answer(
        t(user["lang"], "ignore_added", n=await db.ignore_count(user["id"]))
    )


@router.message(Command("unignore"))
async def cmd_unignore(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()
    if arg.lower() == "all":
        removed = await db.ignore_clear(user["id"])
        await message.answer(t(user["lang"], "ignore_cleared", n=removed))
        return

    target_id: Optional[int] = None
    if arg.startswith("#") and arg[1:].isdigit():
        items = await db.ignore_list(user["id"])
        index = int(arg[1:]) - 1
        if 0 <= index < len(items):
            target_id = items[index]["peer_id"]
    elif message.reply_to_message:
        _, target = await author_of_reply(message)
        target_id = target["id"] if target else None
    elif arg:
        target = await db.resolve(arg)
        target_id = target["id"] if target else None

    if target_id is None:
        await message.answer(t(user["lang"], "unignore_head"))
        return
    await db.ignore_remove(user["id"], target_id)
    await message.answer(
        t(user["lang"], "ignore_removed", n=await db.ignore_count(user["id"]))
    )


async def pm_block_list_text(user: dict[str, Any]) -> str:
    peers = await db.pm_blocklist(user["id"])
    if not peers:
        return t(user["lang"], "pm_block_empty")
    rows = []
    for index, peer_id in enumerate(peers, 1):
        peer = await db.get_user(peer_id)
        rows.append(f"#{index} — ID {peer_id}")
    return t(user["lang"], "pm_block_list", n=len(peers), items="\n".join(rows))


@router.message(Command("ignorelist", ignore_case=True))
async def cmd_ignorelist(message: Message, user: dict[str, Any]):
    await message.answer(await ignore_list_text(user))


# --------------------------------------------------------------------------- #
#                                  фильтры                                     #
# --------------------------------------------------------------------------- #

async def filter_list_text(user: dict[str, Any]) -> str:
    items = await db.filter_list(user["id"])
    if not items:
        return t(user["lang"], "filter_empty")
    listing = "\n".join(
        f"#{index} [{row['kind']}] {html.escape(row['value'])}"
        for index, row in enumerate(items, 1)
    )
    return t(user["lang"], "filter_list", n=len(items), items=listing)


@router.message(Command("filter"))
async def cmd_filter(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()

    if message.reply_to_message and not arg:
        record = await db.message_by_copy(
            message.chat.id, message.reply_to_message.message_id
        )
        if is_stub(record):
            await message.answer(t(user["lang"], "msg_stub"))
            return
        ids = textutil.media_ids(message.reply_to_message)
        if not ids:
            await message.answer(t(user["lang"], "filter_need_media"))
            return
        added = 0
        for uid in ids:
            added += int(await db.filter_add(user["id"], "media", uid))
        if not added:
            await message.answer(t(user["lang"], "filter_exists"))
            return
        await message.answer(
            t(
                user["lang"], "filter_added",
                kind="media", value=ids[0], n=await db.filter_count(user["id"]),
            )
        )
        return

    if not arg:
        await message.answer(t(user["lang"], "filter_head"))
        return

    kind, value = "word", arg
    if len(arg) > 2 and arg.startswith("/") and arg.endswith("/"):
        kind, value = "regex", arg[1:-1]
        try:
            re.compile(value)
        except re.error:
            await message.answer(t(user["lang"], "filter_bad_regex"))
            return

    if not await db.filter_add(user["id"], kind, value):
        await message.answer(t(user["lang"], "filter_exists"))
        return
    await message.answer(
        t(
            user["lang"], "filter_added",
            kind=kind, value=html.escape(value), n=await db.filter_count(user["id"]),
        )
    )


@router.message(Command("unfilter"))
async def cmd_unfilter(message: Message, user: dict[str, Any], command: CommandObject):
    arg = (command.args or "").strip()

    if arg.lower() == "all":
        await message.answer(
            t(user["lang"], "filter_cleared", n=await db.filter_clear(user["id"]))
        )
        return

    if message.reply_to_message and not arg:
        removed = 0
        for uid in textutil.media_ids(message.reply_to_message):
            removed += await db.filter_remove(user["id"], uid)
        if not removed:
            await message.answer(t(user["lang"], "filter_not_found"))
            return
        await message.answer(
            t(
                user["lang"], "filter_removed",
                value="media", n=await db.filter_count(user["id"]),
            )
        )
        return

    if not arg:
        await message.answer(t(user["lang"], "unfilter_head"))
        return

    value = arg[1:-1] if len(arg) > 2 and arg.startswith("/") and arg.endswith("/") else arg
    if not await db.filter_remove(user["id"], value):
        await message.answer(t(user["lang"], "filter_not_found"))
        return
    await message.answer(
        t(
            user["lang"], "filter_removed",
            value=html.escape(value), n=await db.filter_count(user["id"]),
        )
    )


@router.message(Command("filterlist", ignore_case=True))
async def cmd_filterlist(message: Message, user: dict[str, Any]):
    await message.answer(await filter_list_text(user))


# --------------------------------------------------------------------------- #
#                             личные сообщения                                 #
# --------------------------------------------------------------------------- #

@router.message(Command("pm"))
async def cmd_pm(message: Message, user: dict[str, Any], command: CommandObject, bot: Bot):
    lang = user["lang"]
    if not message.reply_to_message or not (command.args or "").strip():
        await message.answer(t(lang, "pm_head") + "\n\n" + t(lang, "pm_usage"))
        return
    if moderation.muted(user):
        await message.answer(moderation.mute_message(user))
        return

    record, peer = await author_of_reply(message)
    if record and (record["stub"] or record["deleted"]):
        await message.answer(t(lang, "msg_stub"))
        return
    if not peer:
        await message.answer(t(lang, "msg_stub"))
        return
    if peer["id"] == user["id"]:
        await message.answer(t(lang, "report_self"))
        return
    if not peer["active"]:
        await message.answer(t(lang, "pm_fail"))
        return
    if not peer["pm_open"]:
        await message.answer(t(lang, "pm_closed"))
        return
    if await db.pm_blocked(peer["id"], user["id"]):
        await message.answer(t(lang, "pm_blocked"))
        return

    # ЛС всегда без тега — полностью анонимно
    try:
        sent = await bot.send_message(
            peer["id"],
            t(peer["lang"], "pm_in", text=html.escape(command.args.strip())),
            reply_markup=keyboards.pm_kb(user["id"]),
            link_preview_options=NO_PREVIEW,
            protect_content=bool(user["protect"]),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await db.update(peer["id"], active=0)
        await message.answer(t(lang, "pm_fail"))
        return

    await db.add_pm(peer["id"], sent.message_id, user["id"])
    await message.answer(t(lang, "pm_sent"))


@router.callback_query(F.data.startswith("pmb:"))
async def cb_pm_block(call: CallbackQuery, user: dict[str, Any]):
    await db.pm_block(user["id"], int(call.data.split(":", 1)[1]))
    await call.answer(t(user["lang"], "pm_block_done"), show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


# --------------------------------------------------------------------------- #
#                                  репорты                                     #
# --------------------------------------------------------------------------- #

@router.message(Command("report"))
async def cmd_report(message: Message, user: dict[str, Any], command: CommandObject, bot: Bot):
    lang = user["lang"]
    if moderation.muted(user):
        await message.answer(t(lang, "report_muted"))
        return
    if not message.reply_to_message:
        await message.answer(t(lang, "report_usage"))
        return

    left = user["last_report"] + db.get_int("report_cooldown") - int(time.time())
    if left > 0:
        await message.answer(t(lang, "report_cd", left=timeutil.human_delta(left)))
        return

    record, target = await author_of_reply(message)
    if not record or record["stub"] or record["deleted"]:
        await message.answer(t(lang, "msg_stub"))
        return
    if record["author_id"] == user["id"]:
        await message.answer(t(lang, "report_self"))
        return

    reason = (command.args or "").strip() or "—"
    await db.add_report(user["id"], record["author_id"], record["ref"], reason)
    await db.update(user["id"], last_report=int(time.time()))

    header = t(
        lang, "report_admin",
        target=str(target["id"]) if target else "?",
        target_id=record["author_id"],
        author=str(user["id"]),
        author_id=user["id"],
        reason=html.escape(reason),
    )
    # репорт уходит только админам и овнеру, в общий чат ничего не летит
    for member in await db.staff():
        try:
            await bot.send_message(member["id"], header, link_preview_options=NO_PREVIEW)
            copy_id = await db.copy_in_chat(record["ref"], member["id"])
            if copy_id:
                await bot.copy_message(member["id"], member["id"], copy_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            continue

    await message.answer(t(lang, "report_sent"))
    await logs.event("REPORT", header)


# --------------------------------------------------------------------------- #
#                      удаление и редактирование своего эхо                    #
# --------------------------------------------------------------------------- #

async def do_delete(call: CallbackQuery, user: dict[str, Any], bot: Bot, token: str):
    record = await db.message_by_token(token)
    if not record or record["author_id"] != user["id"]:
        await call.answer(t(user["lang"], "not_your_msg"), show_alert=True)
        return
    if record["deleted"]:
        await call.answer(t(user["lang"], "msg_stub"), show_alert=True)
        return
    removed = await moderation.delete_message(bot, record["ref"])
    try:
        await call.message.edit_text(t(user["lang"], "deleted_ok", count=removed))
    except TelegramBadRequest:
        await call.message.answer(t(user["lang"], "deleted_ok", count=removed))
    await call.answer()


@router.callback_query(F.data.startswith("d:"))
async def cb_delete(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    token = call.data.split(":", 1)[1]
    if not user["del_warning"]:
        await do_delete(call, user, bot, token)
        return
    await call.message.edit_reply_markup(reply_markup=keyboards.delete_confirm_kb(token))
    await call.answer(t(user["lang"], "delete_confirm"))


@router.callback_query(F.data.startswith("dy:"))
async def cb_delete_yes(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    await do_delete(call, user, bot, call.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("dn:"))
async def cb_delete_no(call: CallbackQuery):
    await call.message.edit_reply_markup(
        reply_markup=keyboards.sent_kb(call.data.split(":", 1)[1])
    )
    await call.answer()


@router.edited_message(F.chat.type == "private")
async def on_edited(message: Message, user: dict[str, Any], bot: Bot):
    record = await db.message_by_author_msg(user["id"], message.message_id)
    if not record or record["deleted"]:
        return
    if record["stub"]:
        await message.answer(t(user["lang"], "msg_stub"))
        return

    raw_text, raw_entities = textutil.source_text(message)
    is_text = message.text is not None

    if user["autoedit"]:
        changed = await delivery.propagate_edit(
            bot, record["ref"], user, raw_text, raw_entities, is_text,
            echo_markup(message, user),
            echo_markup(message, user, with_delete=record["token"]),
        )
        await message.answer(t(user["lang"], "edited_ok", count=changed))
        return

    PENDING_EDITS[record["token"]] = (raw_text, raw_entities, is_text)
    await message.answer(
        t(user["lang"], "edit_confirm"),
        reply_markup=keyboards.edit_confirm_kb(record["token"]),
    )


@router.callback_query(F.data.startswith("ey:"))
async def cb_edit_confirm(call: CallbackQuery, user: dict[str, Any], bot: Bot):
    token = call.data.split(":", 1)[1]
    record = await db.message_by_token(token)
    payload = PENDING_EDITS.pop(token, None)
    if not record or record["author_id"] != user["id"] or not payload:
        await call.answer(t(user["lang"], "not_your_msg"), show_alert=True)
        return
    raw_text, raw_entities, is_text = payload
    markup = keyboards.echo_kb(
        user,
        display_name=call.from_user.full_name,
        username=call.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
    )
    self_markup = keyboards.echo_kb(
        user,
        display_name=call.from_user.full_name,
        username=call.from_user.username,
        is_owner=moderation.is_owner(user),
        is_admin=user["role"] == "admin",
        prefix=user["prefix"],
        with_delete=token,
    )
    changed = await delivery.propagate_edit(
        bot, record["ref"], user, raw_text, raw_entities, is_text, markup, self_markup
    )
    await call.message.edit_text(t(user["lang"], "edited_ok", count=changed))
    await call.answer()


# --------------------------------------------------------------------------- #
#                                   реакции                                    #
# --------------------------------------------------------------------------- #

@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, bot: Bot):
    """
    Реакцию видят все: бот повторяет её на оригинале автора и на копиях
    у всех получателей. Новая реакция заменяет старую.
    """
    record = await db.message_by_copy(event.chat.id, event.message_id)
    if not record or record["deleted"]:
        return
    author = await db.get_user(record["author_id"])
    if not author or not author["reaction"] or not author["active"]:
        return

    reaction = list(event.new_reaction or [])

    # оригинал автора + все копии, кроме той, где реакцию уже поставили руками
    targets = [(author["id"], record["author_msg_id"])]
    targets += [
        (chat_id, msg_id)
        for chat_id, msg_id in await db.copies(record["ref"])
        if not (chat_id == event.chat.id and msg_id == event.message_id)
    ]

    semaphore = asyncio.Semaphore(max(1, db.get_int("parallel_limit")))

    async def mirror(chat_id: int, msg_id: int) -> None:
        async with semaphore:
            try:
                await bot.set_message_reaction(
                    chat_id=chat_id, message_id=msg_id, reaction=reaction
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await bot.set_message_reaction(
                        chat_id=chat_id, message_id=msg_id, reaction=reaction
                    )
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

    await asyncio.gather(*(mirror(chat_id, msg_id) for chat_id, msg_id in targets))


# --------------------------------------------------------------------------- #
#                                    ЭХО                                       #
# --------------------------------------------------------------------------- #

def slowmode_applies(user: dict[str, Any]) -> bool:
    if moderation.is_owner(user):
        return db.get_bool("slowmode_owner")
    if user["role"] == "admin":
        return db.get_bool("slowmode_staff")
    return True


@router.message(F.chat.type == "private")
async def echo(message: Message, user: dict[str, Any], bot: Bot):
    lang = user["lang"]

    if message.text and message.text.startswith("/"):
        await message.answer(t(lang, "unknown_cmd"))
        return

    if not user["registered"]:
        await message.answer(
            t(lang, "greet", name=html.escape(message.from_user.full_name)),
            reply_markup=keyboards.confirm_start_kb(),
        )
        return

    if moderation.muted(user):
        await message.answer(moderation.mute_message(user))
        return

    if user["afk"]:
        note = t(lang, "afk_restored")
        if user["afk_auto"]:
            note += t(lang, "tag_disabled_safety")
        await db.update(user["id"], afk=0, afk_auto=0)
        await message.answer(note)
        return

    if not user["active"]:
        await db.update(user["id"], active=1)

    slowmode = db.get_int("slowmode")
    left = user["last_msg_at"] + slowmode - int(time.time())
    if slowmode and left > 0 and slowmode_applies(user):
        await message.answer(t(lang, "slowmode", left=left))
        return

    kind = delivery.message_kind(message)
    if not delivery.kind_allowed(kind):
        await message.answer(t(lang, "type_disabled"))
        return

    batch = await delivery.albums.collect(message)
    if batch is None:
        return  # остальные части альбома отправит первый апдейт

    if db.get_bool("no_duplicates"):
        digest = textutil.content_hash(batch)
        if digest == user["last_hash"]:
            await message.answer(t(lang, "duplicate"))
            return
    else:
        digest = user["last_hash"]

    await sync_username(message, user)

    reply_ref: Optional[int] = None
    if message.reply_to_message:
        parent = await db.find_message(message.chat.id, message.reply_to_message.message_id)
        if parent:
            if parent["stub"] or parent["deleted"]:
                await message.answer(t(lang, "msg_stub"))
                return
            reply_ref = parent["ref"]

    record = await db.add_message(user["id"], batch[0].message_id, reply_ref)
    result = await delivery.broadcast(
        bot, user, batch, record["ref"], reply_ref, echo_markup(message, user)
    )

    # своя копия: видишь ровно то, что увидели остальные, и кнопку удаления под ней
    self_ids = await delivery.deliver_self(
        bot, user, batch, record["ref"], reply_ref,
        echo_markup(message, user, with_delete=record["token"]),
    )
    own_button = bool(self_ids) and len(batch) == 1

    await db.update(
        user["id"],
        msgs=user["msgs"] + 1,
        last_msg_at=int(time.time()),
        last_hash=digest,
    )
    await db.bump_daily(user["id"])

    if result.total == 0:
        await message.answer(
            t(lang, "nobody"),
            reply_markup=None if own_button else keyboards.sent_kb(record["token"]),
        )
        return

    await message.answer(
        delivery.report_text(lang, result),
        reply_markup=None if own_button else keyboards.sent_kb(record["token"]),
        link_preview_options=NO_PREVIEW,
    )

    if user["autodel"]:
        asyncio.create_task(autodelete_later(bot, record["ref"], user["autodel"]))
