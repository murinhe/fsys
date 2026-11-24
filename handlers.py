"""Telegram bot handlers for single-message exploration, ingestion, and CRUD."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from telegram import CallbackQuery, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from database import (
    Database,
    DuplicateError,
    DuplicateFileError,
    FileRecord,
    FolderRecord,
    InvalidMoveError,
    NotFoundError,
)
from keyboards import (
    CODE_TO_TYPE,
    FILE_TYPE_ICONS,
    FILE_TYPE_LABELS,
    TYPE_TO_CODE,
    cancel_input_keyboard,
    explorer_keyboard,
    file_delete_confirm_keyboard,
    file_detail_keyboard,
    filtered_files_keyboard,
    folder_actions_keyboard,
    folder_delete_confirm_keyboard,
    move_picker_keyboard,
    tag_editor_keyboard,
    tag_filter_keyboard,
    type_filter_keyboard,
)

logger = logging.getLogger(__name__)

MAX_FOLDER_NAME_LEN = 64
MAX_FILE_NAME_LEN = 128
MAX_TAG_NAME_LEN = 32
_TAG_SPLIT_RE = re.compile(r"[\s,]+")


class InputAction(str, Enum):
    """Active text-input state stored in context.user_data['pending_input']."""

    CREATE_FOLDER = "create_folder"
    RENAME_FOLDER = "rename_folder"
    RENAME_FILE = "rename_file"
    EDIT_TAGS = "edit_tags"
    SEARCH = "search"


# ----------------------------------------------------------------------
# Context & Formatting Helpers
# ----------------------------------------------------------------------


def _get_config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in _get_config(context).allowed_user_ids


async def _reject_unauthorized(update: Update) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer(
            "⛔ You are not authorized to use this bot.", show_alert=True
        )
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            "⛔ Access denied. Your Telegram user ID is not whitelisted."
        )


def _format_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "Unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def _format_breadcrumb(trail: list[FolderRecord]) -> str:
    parts = ["Root", *(html.escape(folder.name) for folder in trail)]
    return "📁 <b>" + " / ".join(parts) + "</b>"


def _validate_folder_name(raw: str) -> str | None:
    name = " ".join(raw.strip().split())
    if not name:
        return None
    if "/" in name or "\\" in name:
        return None
    if len(name) > MAX_FOLDER_NAME_LEN:
        return None
    return name


def _validate_file_name(raw: str) -> str | None:
    name = " ".join(raw.strip().split())
    if not name or len(name) > MAX_FILE_NAME_LEN:
        return None
    return name


def _parse_tag_input(raw: str) -> tuple[list[str], list[str]]:
    """Parse user text like 'work, 2026, -draft' into (add_list, remove_list)."""
    add_list: list[str] = []
    remove_list: list[str] = []
    for token in _TAG_SPLIT_RE.split(raw.strip()):
        if not token:
            continue
        is_remove = token.startswith("-")
        clean = token[1:] if is_remove else token
        clean = clean.lstrip("#").strip()
        if not clean or len(clean) > MAX_TAG_NAME_LEN:
            continue
        if is_remove:
            remove_list.append(clean)
        else:
            add_list.append(clean)
    return add_list, remove_list


def _clear_pending_input(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_input", None)


def _set_pending_input(
    context: ContextTypes.DEFAULT_TYPE,
    action: InputAction,
    target_id: int | None = None,
) -> None:
    context.user_data["pending_input"] = {
        "action": action.value,
        "target_id": target_id,
    }


def _folder_id_from_token(token: str) -> int | None:
    val = int(token)
    return None if val <= 0 else val


async def _ensure_valid_current_folder(
    db: Database, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    folder_id: int | None = context.user_data.get("current_folder_id")
    if folder_id is not None:
        if await db.get_folder(user_id, folder_id) is None:
            context.user_data["current_folder_id"] = None
            return None
    return folder_id


# ----------------------------------------------------------------------
# Single-Message Dashboard Rendering
# ----------------------------------------------------------------------


async def _show_panel(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup: Any,
    query: CallbackQuery | None = None,
    force_new: bool = False,
) -> int:
    """Edit the existing dashboard message in-place, or create one if needed."""
    if query is not None and query.message is not None and not force_new:
        context.user_data["dashboard_msg_id"] = query.message.message_id
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return query.message.message_id
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return query.message.message_id
            logger.debug("Fallback to sending new dashboard message: %s", exc)

    dashboard_msg_id: int | None = (
        None if force_new else context.user_data.get("dashboard_msg_id")
    )
    if dashboard_msg_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=dashboard_msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return dashboard_msg_id
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                return dashboard_msg_id
            logger.debug("Stored dashboard message unavailable, sending fresh: %s", exc)

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    context.user_data["dashboard_msg_id"] = sent.message_id
    return sent.message_id


async def _render_explorer(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    folder_id: int | None,
    page: int = 0,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
    force_new: bool = False,
) -> None:
    db = _get_db(context)
    page_size = _get_config(context).page_size

    folder: FolderRecord | None = None
    if folder_id is not None:
        folder = await db.get_folder(user_id, folder_id)
        if folder is None:
            folder_id = None

    context.user_data["current_folder_id"] = folder_id
    context.user_data["view_mode"] = "folder"

    trail = await db.get_breadcrumb(user_id, folder_id)
    subfolders = await db.list_subfolders(user_id, folder_id)
    files = await db.list_files_in_folder(user_id, folder_id)

    markup, clamped_page, _ = explorer_keyboard(
        folder_id=folder_id,
        parent_id=folder.parent_id if folder else None,
        folders=subfolders,
        files=files,
        page=page,
        page_size=page_size,
    )
    context.user_data["view_page"] = clamped_page

    lines = [_format_breadcrumb(trail)]
    if status_notice:
        lines.append(f"\n{status_notice}")

    if not subfolders and not files:
        lines.append(
            "\n<i>This folder is empty. Send or forward any file here to save it.</i>"
        )
    else:
        lines.append(
            f"\n<i>{len(subfolders)} folder(s) · {len(files)} file(s)</i>"
        )

    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=markup,
        query=query,
        force_new=force_new,
    )


async def _render_search_results(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    search_query: str,
    page: int = 0,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
) -> None:
    db = _get_db(context)
    page_size = _get_config(context).page_size
    current_folder_id = await _ensure_valid_current_folder(db, user_id, context)

    context.user_data["view_mode"] = "search"
    context.user_data["search_query"] = search_query

    files = await db.search_files(user_id, search_query)
    markup, clamped_page, _ = filtered_files_keyboard(
        files=files,
        page=page,
        page_size=page_size,
        page_cb_prefix=("page",),
        current_folder_id=current_folder_id,
    )
    context.user_data["view_page"] = clamped_page

    lines = [f'🔍 <b>Search:</b> <code>{html.escape(search_query)}</code>']
    if status_notice:
        lines.append(f"\n{status_notice}")
    if not files:
        lines.append("\n<i>No matching files found.</i>")
    else:
        lines.append(f"\n<i>{len(files)} matching file(s)</i>")

    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=markup,
        query=query,
    )


async def _render_tag_results(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    tag_id: int,
    page: int = 0,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
) -> None:
    db = _get_db(context)
    page_size = _get_config(context).page_size
    current_folder_id = await _ensure_valid_current_folder(db, user_id, context)

    tag = await db.get_tag(user_id, tag_id)
    if tag is None:
        await _render_explorer(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            folder_id=current_folder_id,
            query=query,
            status_notice="⚠️ Tag not found.",
        )
        return

    context.user_data["view_mode"] = "tag"
    context.user_data["filter_tag_id"] = tag_id

    files = await db.list_files_by_tag(user_id, tag_id)
    markup, clamped_page, _ = filtered_files_keyboard(
        files=files,
        page=page,
        page_size=page_size,
        page_cb_prefix=("tag", tag_id),
        current_folder_id=current_folder_id,
    )
    context.user_data["view_page"] = clamped_page

    lines = [f"🏷️ <b>Tag:</b> #{html.escape(tag.name)}"]
    if status_notice:
        lines.append(f"\n{status_notice}")
    if not files:
        lines.append("\n<i>No files have this tag.</i>")
    else:
        lines.append(f"\n<i>{len(files)} file(s)</i>")

    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=markup,
        query=query,
    )


async def _render_type_results(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    file_type: str,
    page: int = 0,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
) -> None:
    db = _get_db(context)
    page_size = _get_config(context).page_size
    current_folder_id = await _ensure_valid_current_folder(db, user_id, context)

    context.user_data["view_mode"] = "type"
    context.user_data["filter_type"] = file_type

    files = await db.list_files_by_type(user_id, file_type)
    code = TYPE_TO_CODE[file_type]
    markup, clamped_page, _ = filtered_files_keyboard(
        files=files,
        page=page,
        page_size=page_size,
        page_cb_prefix=("typ", code),
        current_folder_id=current_folder_id,
    )
    context.user_data["view_page"] = clamped_page

    icon = FILE_TYPE_ICONS[file_type]
    label = FILE_TYPE_LABELS[file_type]
    lines = [f"{icon} <b>Type:</b> {label}"]
    if status_notice:
        lines.append(f"\n{status_notice}")
    if not files:
        lines.append(f"\n<i>No {label.lower()} saved yet.</i>")
    else:
        lines.append(f"\n<i>{len(files)} file(s)</i>")

    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=markup,
        query=query,
    )


async def _render_active_view(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
) -> None:
    """Restore whichever view (folder, search, tag, type) was last active."""
    view_mode = context.user_data.get("view_mode", "folder")
    page = int(context.user_data.get("view_page", 0))

    if view_mode == "search" and context.user_data.get("search_query"):
        await _render_search_results(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            search_query=context.user_data["search_query"],
            page=page,
            query=query,
            status_notice=status_notice,
        )
        return

    if view_mode == "tag" and context.user_data.get("filter_tag_id") is not None:
        await _render_tag_results(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            tag_id=int(context.user_data["filter_tag_id"]),
            page=page,
            query=query,
            status_notice=status_notice,
        )
        return

    if view_mode == "type" and context.user_data.get("filter_type"):
        await _render_type_results(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            file_type=context.user_data["filter_type"],
            page=page,
            query=query,
            status_notice=status_notice,
        )
        return

    db = _get_db(context)
    folder_id = await _ensure_valid_current_folder(db, user_id, context)
    await _render_explorer(
        context=context,
        user_id=user_id,
        chat_id=chat_id,
        folder_id=folder_id,
        page=page,
        query=query,
        status_notice=status_notice,
    )


async def _render_file_detail(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    file_row_id: int,
    query: CallbackQuery | None = None,
    status_notice: str | None = None,
) -> None:
    db = _get_db(context)
    file_rec = await db.get_file(user_id, file_row_id)
    if file_rec is None:
        await _render_active_view(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            query=query,
            status_notice="⚠️ File no longer exists.",
        )
        return

    trail = await db.get_breadcrumb(user_id, file_rec.folder_id)
    tags = await db.get_file_tags(user_id, file_rec.id)
    icon = FILE_TYPE_ICONS.get(file_rec.file_type, "📎")
    tag_str = (
        ", ".join(f"#{html.escape(t.name)}" for t in tags) if tags else "<i>None</i>"
    )
    mime_str = f" ({html.escape(file_rec.mime_type)})" if file_rec.mime_type else ""

    lines = [
        f"{icon} <b>{html.escape(file_rec.display_name)}</b>",
    ]
    if status_notice:
        lines.append(f"\n{status_notice}")

    lines.extend(
        [
            "",
            f"<b>Type:</b> {file_rec.file_type}{mime_str}",
            f"<b>Size:</b> {_format_size(file_rec.file_size)}",
            f"<b>Folder:</b> {_format_breadcrumb(trail)}",
            f"<b>Tags:</b> {tag_str}",
            f"<b>Saved:</b> <code>{html.escape(file_rec.created_at)}</code>",
        ]
    )

    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=file_detail_keyboard(file_rec.id, file_rec.folder_id),
        query=query,
    )


async def _render_tag_editor(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    file_row_id: int,
    page: int = 0,
    query: CallbackQuery | None = None,
) -> None:
    db = _get_db(context)
    file_rec = await db.get_file(user_id, file_row_id)
    if file_rec is None:
        await _render_active_view(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            query=query,
            status_notice="⚠️ File no longer exists.",
        )
        return

    all_tags = await db.list_tags(user_id, only_used=False)
    attached = await db.get_file_tags(user_id, file_row_id)
    attached_ids = {t.id for t in attached}

    markup, _, _ = tag_editor_keyboard(
        file_row_id=file_row_id,
        all_tags=all_tags,
        attached_tag_ids=attached_ids,
        page=page,
        page_size=_get_config(context).page_size,
    )

    lines = [
        f"🏷️ <b>Edit Tags</b> — {html.escape(file_rec.display_name)}",
        "",
        "Tap a tag below to toggle it, or choose <b>Add / Remove by Text</b> to create new tags.",
    ]
    await _show_panel(
        context=context,
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=markup,
        query=query,
    )


async def _render_move_picker(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    mode: str,
    target_id: int,
    browse_folder_id: int | None,
    page: int = 0,
    query: CallbackQuery | None = None,
) -> None:
    db = _get_db(context)
    page_size = _get_config(context).page_size

    excluded_ids: set[int] = set()
    if mode == "file":
        target_file = await db.get_file(user_id, target_id)
        if target_file is None:
            await _render_active_view(
                context=context,
                user_id=user_id,
                chat_id=chat_id,
                query=query,
                status_notice="⚠️ File not found.",
            )
            return
        title = f"📁 <b>Move File:</b> {html.escape(target_file.display_name)}"
        can_confirm = browse_folder_id != target_file.folder_id
    else:
        target_folder = await db.get_folder(user_id, target_id)
        if target_folder is None:
            await _render_active_view(
                context=context,
                user_id=user_id,
                chat_id=chat_id,
                query=query,
                status_notice="⚠️ Folder not found.",
            )
            return
        excluded_ids = await db.get_folder_descendant_ids(user_id, target_id)
        if browse_folder_id in excluded_ids:
            browse_folder_id = target_folder.parent_id
        title = f"📁 <b>Move Folder:</b> {html.escape(target_folder.name)}"
        can_confirm = (
            browse_folder_id != target_folder.parent_id
            and browse_folder_id not in excluded_ids
        )

    browse_folder: FolderRecord | None = None
    if browse_folder_id is not None:
        browse_folder = await db.get_folder(user_id, browse_folder_id)
        if browse_folder is None:
            browse_folder_id = None

    trail = await db.get_breadcrumb(user_id, browse_folder_id)
    subfolders = await db.list_subfolders(user_id, browse_folder_id)

    markup, _, _ = move_picker_keyboard(
        mode=mode,
        target_id=target_id,
        browse_folder_id=browse_folder_id,
        browse_parent_id=browse_folder.parent_id if browse_folder else None,
        subfolders=subfolders,
        page=page,
        page_size=page_size,
        excluded_ids=excluded_ids,
        can_confirm=can_confirm,
    )

    hint = (
        "Browse to a destination folder and tap <b>Move Here</b>."
        if can_confirm
        else "<i>Already in this folder. Choose another destination folder.</i>"
    )
    text = f"{title}\n\nDestination: {_format_breadcrumb(trail)}\n\n{hint}"
    await _show_panel(
        context=context,
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        query=query,
    )


# ----------------------------------------------------------------------
# Send File Dispatcher (Native Telegram file_id, Zero Local Storage)
# ----------------------------------------------------------------------


async def _send_file_by_type(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_rec: FileRecord,
) -> None:
    """Re-send media directly from Telegram servers using stored file_id."""
    caption = html.escape(file_rec.display_name)
    bot = context.bot

    if file_rec.file_type == "document":
        await bot.send_document(
            chat_id=chat_id,
            document=file_rec.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    elif file_rec.file_type == "photo":
        await bot.send_photo(
            chat_id=chat_id,
            photo=file_rec.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    elif file_rec.file_type == "video":
        await bot.send_video(
            chat_id=chat_id,
            video=file_rec.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    elif file_rec.file_type == "audio":
        await bot.send_audio(
            chat_id=chat_id,
            audio=file_rec.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    elif file_rec.file_type == "voice":
        await bot.send_voice(
            chat_id=chat_id,
            voice=file_rec.file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    else:
        raise ValueError(f"Unsupported file_type: {file_rec.file_type}")


# ----------------------------------------------------------------------
# Media Extraction for File Ingestion
# ----------------------------------------------------------------------


def _extract_media_payload(message: Message) -> dict[str, Any] | None:
    """Extract Telegram file_id metadata without downloading any binary content."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    caption_line = (
        message.caption.strip().splitlines()[0][:MAX_FILE_NAME_LEN]
        if message.caption and message.caption.strip()
        else None
    )

    if message.document:
        doc = message.document
        return {
            "file_id": doc.file_id,
            "file_unique_id": doc.file_unique_id,
            "display_name": doc.file_name or caption_line or f"document_{stamp}",
            "file_type": "document",
            "mime_type": doc.mime_type,
            "file_size": doc.file_size,
        }

    if message.photo:
        # Always pick the highest-resolution variant
        best = message.photo[-1]
        return {
            "file_id": best.file_id,
            "file_unique_id": best.file_unique_id,
            "display_name": caption_line or f"photo_{stamp}.jpg",
            "file_type": "photo",
            "mime_type": "image/jpeg",
            "file_size": best.file_size,
        }

    if message.video:
        vid = message.video
        return {
            "file_id": vid.file_id,
            "file_unique_id": vid.file_unique_id,
            "display_name": vid.file_name or caption_line or f"video_{stamp}.mp4",
            "file_type": "video",
            "mime_type": vid.mime_type,
            "file_size": vid.file_size,
        }

    if message.audio:
        aud = message.audio
        if aud.file_name:
            name = aud.file_name
        elif aud.performer and aud.title:
            name = f"{aud.performer} - {aud.title}"
        elif aud.title:
            name = aud.title
        else:
            name = caption_line or f"audio_{stamp}.mp3"
        return {
            "file_id": aud.file_id,
            "file_unique_id": aud.file_unique_id,
            "display_name": name,
            "file_type": "audio",
            "mime_type": aud.mime_type,
            "file_size": aud.file_size,
        }

    if message.voice:
        voice = message.voice
        return {
            "file_id": voice.file_id,
            "file_unique_id": voice.file_unique_id,
            "display_name": caption_line or f"voice_{stamp}.ogg",
            "file_type": "voice",
            "mime_type": voice.mime_type or "audio/ogg",
            "file_size": voice.file_size,
        }

    return None


# ----------------------------------------------------------------------
# Command Handlers
# ----------------------------------------------------------------------


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return
    assert update.effective_user is not None and update.effective_chat is not None

    _clear_pending_input(context)
    is_start = bool(
        update.message and update.message.text and update.message.text.startswith("/start")
    )
    folder_id = None if is_start else context.user_data.get("current_folder_id")

    await _render_explorer(
        context=context,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        folder_id=folder_id,
        page=0,
        force_new=True,
    )


async def new_folder_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return
    assert update.effective_user is not None and update.effective_chat is not None

    db = _get_db(context)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    folder_id = await _ensure_valid_current_folder(db, user_id, context)

    if context.args:
        raw_name = " ".join(context.args)
        name = _validate_folder_name(raw_name)
        if not name:
            await update.effective_message.reply_text(
                "⚠️ Invalid folder name. Avoid slashes and keep it under 64 characters."
            )
            return
        try:
            created = await db.create_folder(user_id, name, folder_id)
        except DuplicateError as exc:
            await update.effective_message.reply_text(f"⚠️ {exc}")
            return
        await _render_explorer(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            folder_id=folder_id,
            status_notice=f"✅ Created folder <b>{html.escape(created.name)}</b>.",
        )
        return

    trail = await db.get_breadcrumb(user_id, folder_id)
    _set_pending_input(context, InputAction.CREATE_FOLDER, folder_id)
    await _show_panel(
        context=context,
        chat_id=chat_id,
        text=(
            f"➕ <b>New Folder</b> in {_format_breadcrumb(trail)}\n\n"
            "Send the folder name (or /cancel to abort)."
        ),
        reply_markup=cancel_input_keyboard(),
    )


async def search_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return
    assert update.effective_user is not None and update.effective_chat is not None

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    _clear_pending_input(context)

    if context.args:
        query_text = " ".join(context.args).strip()
        await _render_search_results(
            context=context,
            user_id=user_id,
            chat_id=chat_id,
            search_query=query_text,
            page=0,
        )
        return

    _set_pending_input(context, InputAction.SEARCH)
    await _show_panel(
        context=context,
        chat_id=chat_id,
        text=(
            "🔍 <b>Search Files</b>\n\n"
            "Send a filename or tag keyword to search (or /cancel to abort)."
        ),
        reply_markup=cancel_input_keyboard(),
    )


async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return
    assert update.effective_user is not None and update.effective_chat is not None

    had_pending = context.user_data.pop("pending_input", None) is not None
    notice = "❎ Cancelled." if had_pending else "Nothing to cancel."
    await _render_active_view(
        context=context,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        status_notice=notice,
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return
    assert update.effective_message is not None

    await update.effective_message.reply_text(
        "<b>Telegram File Manager</b>\n\n"
        "• Send or forward any <b>document, photo, video, audio, or voice</b> "
        "to save its Telegram <code>file_id</code> into your currently open folder.\n"
        "• Zero files are downloaded or stored on disk.\n\n"
        "<b>Commands:</b>\n"
        "/start — Open Root explorer\n"
        "/files — Re-open explorer in current folder\n"
        "/newfolder [name] — Create a folder here\n"
        "/search [query] — Search files by name or tag\n"
        "/cancel — Cancel the current text input flow",
        parse_mode=ParseMode.HTML,
    )


# ----------------------------------------------------------------------
# Media Ingestion & Text Input Handlers
# ----------------------------------------------------------------------


async def media_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return

    payload = _extract_media_payload(message)
    if payload is None:
        return

    db = _get_db(context)
    folder_id = await _ensure_valid_current_folder(db, user.id, context)
    trail = await db.get_breadcrumb(user.id, folder_id)
    folder_label = _format_breadcrumb(trail)

    try:
        saved = await db.add_file(
            user_id=user.id,
            folder_id=folder_id,
            file_id=payload["file_id"],
            file_unique_id=payload["file_unique_id"],
            display_name=payload["display_name"],
            file_type=payload["file_type"],
            mime_type=payload["mime_type"],
            file_size=payload["file_size"],
        )
    except DuplicateFileError as exc:
        await message.reply_text(
            f"⚠️ Duplicate skipped: <b>{html.escape(exc.existing_file.display_name)}</b> "
            f"is already in {folder_label}.",
            parse_mode=ParseMode.HTML,
        )
        return

    icon = FILE_TYPE_ICONS.get(saved.file_type, "📎")
    status_line = f"✅ Saved {icon} <b>{html.escape(saved.display_name)}</b>"
    await message.reply_text(
        f"{status_line} → {folder_label}",
        parse_mode=ParseMode.HTML,
    )

    # Refresh the single-message explorer in-place if the user is viewing this folder
    if (
        context.user_data.get("dashboard_msg_id") is not None
        and context.user_data.get("pending_input") is None
        and context.user_data.get("view_mode", "folder") == "folder"
    ):
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=folder_id,
            page=int(context.user_data.get("view_page", 0)),
            status_notice=status_line,
        )


async def text_input_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or not message.text or user is None or chat is None:
        return

    pending = context.user_data.get("pending_input")
    if not pending:
        await message.reply_text(
            "Use /files to open the explorer, or send/forward any media to save it here."
        )
        return

    action = InputAction(pending["action"])
    target_id: int | None = pending.get("target_id")
    raw_text = message.text.strip()
    db = _get_db(context)

    if action == InputAction.CREATE_FOLDER:
        name = _validate_folder_name(raw_text)
        if not name:
            await message.reply_text(
                "⚠️ Folder name must be 1–64 characters and cannot contain '/' or '\\'. "
                "Try again or send /cancel."
            )
            return
        try:
            created = await db.create_folder(user.id, name, target_id)
        except DuplicateError as exc:
            await message.reply_text(f"⚠️ {exc} Try another name or send /cancel.")
            return
        except NotFoundError:
            _clear_pending_input(context)
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                status_notice="⚠️ Parent folder no longer exists; returned to Root.",
            )
            return

        _clear_pending_input(context)
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=target_id,
            page=0,
            status_notice=f"✅ Created folder <b>{html.escape(created.name)}</b>.",
        )
        return

    if action == InputAction.RENAME_FOLDER:
        assert target_id is not None
        name = _validate_folder_name(raw_text)
        if not name:
            await message.reply_text(
                "⚠️ Folder name must be 1–64 characters and cannot contain '/' or '\\'. "
                "Try again or send /cancel."
            )
            return
        try:
            renamed = await db.rename_folder(user.id, target_id, name)
        except DuplicateError as exc:
            await message.reply_text(f"⚠️ {exc} Try another name or send /cancel.")
            return
        except NotFoundError:
            _clear_pending_input(context)
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                status_notice="⚠️ Folder no longer exists.",
            )
            return

        _clear_pending_input(context)
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=renamed.id,
            page=0,
            status_notice=f"✅ Renamed folder to <b>{html.escape(renamed.name)}</b>.",
        )
        return

    if action == InputAction.RENAME_FILE:
        assert target_id is not None
        name = _validate_file_name(raw_text)
        if not name:
            await message.reply_text(
                "⚠️ File name must be 1–128 characters. Try again or send /cancel."
            )
            return
        try:
            renamed = await db.rename_file(user.id, target_id, name)
        except NotFoundError:
            _clear_pending_input(context)
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                status_notice="⚠️ File no longer exists.",
            )
            return

        _clear_pending_input(context)
        await _render_file_detail(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=renamed.id,
            status_notice=f"✅ Renamed to <b>{html.escape(renamed.display_name)}</b>.",
        )
        return

    if action == InputAction.EDIT_TAGS:
        assert target_id is not None
        add_names, remove_names = _parse_tag_input(raw_text)
        if not add_names and not remove_names:
            await message.reply_text(
                "⚠️ Provide at least one valid tag (1–32 chars), e.g. "
                "<code>work, 2026, -draft</code>, or send /cancel.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            added, removed = await db.apply_tag_changes(
                user.id, target_id, add_names, remove_names
            )
        except NotFoundError:
            _clear_pending_input(context)
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                status_notice="⚠️ File no longer exists.",
            )
            return

        _clear_pending_input(context)
        parts: list[str] = []
        if added:
            parts.append("Added: " + ", ".join(f"#{html.escape(t)}" for t in added))
        if removed:
            parts.append("Removed: " + ", ".join(f"#{html.escape(t)}" for t in removed))
        notice = "✅ " + " · ".join(parts) if parts else "ℹ️ No tag changes made."

        await _render_file_detail(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=target_id,
            status_notice=notice,
        )
        return

    if action == InputAction.SEARCH:
        _clear_pending_input(context)
        await _render_search_results(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            search_query=raw_text,
            page=0,
        )


# ----------------------------------------------------------------------
# Callback Query Router
# ----------------------------------------------------------------------


async def callback_router(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _reject_unauthorized(update)
        return

    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or not query.data or user is None or chat is None:
        return

    parts = query.data.split(":")
    cmd = parts[0]
    db = _get_db(context)

    if cmd == "noop":
        await query.answer()
        return

    # Any button interaction cancels stale text-input flows unless entering a new one
    _clear_pending_input(context)

    # --- Navigation & Explorer ---
    if cmd == "home":
        await query.answer()
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=None,
            page=0,
            query=query,
        )
        return

    if cmd == "dir" and len(parts) == 3:
        await query.answer()
        folder_id = _folder_id_from_token(parts[1])
        page = int(parts[2])
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=folder_id,
            page=page,
            query=query,
        )
        return

    if cmd == "back":
        await query.answer()
        await _render_active_view(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            query=query,
        )
        return

    if cmd == "cxl":
        await query.answer("Cancelled")
        await _render_active_view(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            query=query,
        )
        return

    if cmd == "page" and len(parts) == 2:
        await query.answer()
        search_q = context.user_data.get("search_query")
        if search_q:
            await _render_search_results(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                search_query=search_q,
                page=int(parts[1]),
                query=query,
            )
        else:
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                query=query,
            )
        return

    # --- Search & Filters ---
    if cmd == "srch":
        await query.answer()
        _set_pending_input(context, InputAction.SEARCH)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                "🔍 <b>Search Files</b>\n\n"
                "Send a filename or tag keyword to search (or /cancel to abort)."
            ),
            reply_markup=cancel_input_keyboard(),
            query=query,
        )
        return

    if cmd == "ftag" and len(parts) == 2:
        await query.answer()
        page = int(parts[1])
        current_folder_id = await _ensure_valid_current_folder(db, user.id, context)
        tags = await db.list_tags(user.id, only_used=True)
        markup, _, _ = tag_filter_keyboard(
            tags=tags,
            page=page,
            page_size=_get_config(context).page_size,
            current_folder_id=current_folder_id,
        )
        text = (
            "🏷️ <b>Filter by Tag</b>\n\nChoose a tag:"
            if tags
            else "🏷️ <b>Filter by Tag</b>\n\n<i>No tagged files yet. Open any file and tap Edit Tags to add tags.</i>"
        )
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=text,
            reply_markup=markup,
            query=query,
        )
        return

    if cmd == "tag" and len(parts) == 3:
        await query.answer()
        await _render_tag_results(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            tag_id=int(parts[1]),
            page=int(parts[2]),
            query=query,
        )
        return

    if cmd == "ftyp":
        await query.answer()
        current_folder_id = await _ensure_valid_current_folder(db, user.id, context)
        counts = await db.count_files_by_type(user.id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text="📂 <b>Filter by Type</b>\n\nChoose a media type:",
            reply_markup=type_filter_keyboard(
                type_counts=counts, current_folder_id=current_folder_id
            ),
            query=query,
        )
        return

    if cmd == "typ" and len(parts) == 3:
        await query.answer()
        ftype = CODE_TO_TYPE.get(parts[1])
        if ftype is None:
            return
        await _render_type_results(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_type=ftype,
            page=int(parts[2]),
            query=query,
        )
        return

    # --- File Detail & File Management ---
    if cmd == "f" and len(parts) == 2:
        await query.answer()
        await _render_file_detail(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=int(parts[1]),
            query=query,
        )
        return

    if cmd == "fs" and len(parts) == 2:
        file_rec = await db.get_file(user.id, int(parts[1]))
        if file_rec is None:
            await query.answer("File not found.", show_alert=True)
            await _render_active_view(
                context=context, user_id=user.id, chat_id=chat.id, query=query
            )
            return
        await query.answer("📤 Sending file...")
        try:
            await _send_file_by_type(context, chat.id, file_rec)
        except BadRequest as exc:
            logger.warning("Failed to send file_id=%s: %s", file_rec.file_id, exc)
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Telegram could not deliver this file_id. It may be invalid or expired.",
            )
        return

    if cmd == "fr" and len(parts) == 2:
        await query.answer()
        file_rec = await db.get_file(user.id, int(parts[1]))
        if file_rec is None:
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                query=query,
                status_notice="⚠️ File not found.",
            )
            return
        _set_pending_input(context, InputAction.RENAME_FILE, file_rec.id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"✏️ <b>Rename File</b>\n\n"
                f"Current name: <code>{html.escape(file_rec.display_name)}</code>\n\n"
                "Send the new display name (or /cancel to abort)."
            ),
            reply_markup=cancel_input_keyboard(),
            query=query,
        )
        return

    if cmd == "fm" and len(parts) == 4:
        await query.answer()
        await _render_move_picker(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            mode="file",
            target_id=int(parts[1]),
            browse_folder_id=_folder_id_from_token(parts[2]),
            page=int(parts[3]),
            query=query,
        )
        return

    if cmd == "fmc" and len(parts) == 3:
        file_row_id = int(parts[1])
        dest_folder_id = _folder_id_from_token(parts[2])
        try:
            moved = await db.move_file(user.id, file_row_id, dest_folder_id)
        except DuplicateFileError as exc:
            await query.answer(
                f"Destination already contains '{exc.existing_file.display_name}'.",
                show_alert=True,
            )
            return
        except NotFoundError as exc:
            await query.answer(str(exc), show_alert=True)
            await _render_active_view(
                context=context, user_id=user.id, chat_id=chat.id, query=query
            )
            return

        await query.answer("✅ File moved")
        await _render_file_detail(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=moved.id,
            query=query,
            status_notice="✅ File moved.",
        )
        return

    if cmd == "ft" and len(parts) == 3:
        await query.answer()
        await _render_tag_editor(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=int(parts[1]),
            page=int(parts[2]),
            query=query,
        )
        return

    if cmd == "ftt" and len(parts) == 4:
        file_row_id = int(parts[1])
        tag_id = int(parts[2])
        page = int(parts[3])
        try:
            attached = await db.toggle_file_tag(user.id, file_row_id, tag_id)
        except NotFoundError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer("Tag added" if attached else "Tag removed")
        await _render_tag_editor(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            file_row_id=file_row_id,
            page=page,
            query=query,
        )
        return

    if cmd == "fta" and len(parts) == 2:
        await query.answer()
        file_rec = await db.get_file(user.id, int(parts[1]))
        if file_rec is None:
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                query=query,
                status_notice="⚠️ File not found.",
            )
            return
        existing_tags = await db.get_file_tags(user.id, file_rec.id)
        tags_preview = (
            ", ".join(f"#{html.escape(t.name)}" for t in existing_tags)
            if existing_tags
            else "<i>None</i>"
        )
        _set_pending_input(context, InputAction.EDIT_TAGS, file_rec.id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"🏷️ <b>Edit Tags</b> — {html.escape(file_rec.display_name)}\n\n"
                f"Current tags: {tags_preview}\n\n"
                "Send tags separated by spaces or commas.\n"
                "Prefix a tag with <code>-</code> to remove it.\n"
                "Example: <code>finance, 2026, -draft</code>\n\n"
                "Send /cancel to abort."
            ),
            reply_markup=cancel_input_keyboard(),
            query=query,
        )
        return

    if cmd == "fd" and len(parts) == 2:
        await query.answer()
        file_rec = await db.get_file(user.id, int(parts[1]))
        if file_rec is None:
            await _render_active_view(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                query=query,
                status_notice="⚠️ File not found.",
            )
            return
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"🗑️ <b>Delete File?</b>\n\n"
                f"Remove <b>{html.escape(file_rec.display_name)}</b> from your index?\n"
                "<i>(Original Telegram chat messages are untouched.)</i>"
            ),
            reply_markup=file_delete_confirm_keyboard(file_rec.id),
            query=query,
        )
        return

    if cmd == "fdc" and len(parts) == 2:
        try:
            deleted = await db.delete_file(user.id, int(parts[1]))
        except NotFoundError:
            await query.answer("Already deleted.")
            await _render_active_view(
                context=context, user_id=user.id, chat_id=chat.id, query=query
            )
            return
        await query.answer("🗑️ Deleted")
        await _render_active_view(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            query=query,
            status_notice=f"🗑️ Deleted <b>{html.escape(deleted.display_name)}</b>.",
        )
        return

    # --- Folder Actions ---
    if cmd == "dn" and len(parts) == 2:
        await query.answer()
        parent_id = _folder_id_from_token(parts[1])
        trail = await db.get_breadcrumb(user.id, parent_id)
        _set_pending_input(context, InputAction.CREATE_FOLDER, parent_id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"➕ <b>Create New Folder</b> in {_format_breadcrumb(trail)}\n\n"
                "Send the new folder name (or /cancel to abort)."
            ),
            reply_markup=cancel_input_keyboard(),
            query=query,
        )
        return

    if cmd == "dm" and len(parts) == 2:
        await query.answer()
        folder = await db.get_folder(user.id, int(parts[1]))
        if folder is None:
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                query=query,
                status_notice="⚠️ Folder not found.",
            )
            return
        trail = await db.get_breadcrumb(user.id, folder.id)
        counts = await db.get_folder_counts(user.id, folder.id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"⚙️ <b>Folder Actions</b>\n\n"
                f"{_format_breadcrumb(trail)}\n"
                f"<i>Contains {counts.subfolders} nested subfolder(s) · {counts.files} total file(s)</i>"
            ),
            reply_markup=folder_actions_keyboard(folder),
            query=query,
        )
        return

    if cmd == "dr" and len(parts) == 2:
        await query.answer()
        folder = await db.get_folder(user.id, int(parts[1]))
        if folder is None:
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                query=query,
                status_notice="⚠️ Folder not found.",
            )
            return
        _set_pending_input(context, InputAction.RENAME_FOLDER, folder.id)
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"✏️ <b>Rename Folder</b>\n\n"
                f"Current name: <code>{html.escape(folder.name)}</code>\n\n"
                "Send the new folder name (or /cancel to abort)."
            ),
            reply_markup=cancel_input_keyboard(),
            query=query,
        )
        return

    if cmd == "dmv" and len(parts) == 4:
        await query.answer()
        await _render_move_picker(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            mode="folder",
            target_id=int(parts[1]),
            browse_folder_id=_folder_id_from_token(parts[2]),
            page=int(parts[3]),
            query=query,
        )
        return

    if cmd == "dmc" and len(parts) == 3:
        folder_id = int(parts[1])
        dest_parent_id = _folder_id_from_token(parts[2])
        try:
            moved = await db.move_folder(user.id, folder_id, dest_parent_id)
        except (DuplicateError, InvalidMoveError, NotFoundError) as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer("✅ Folder moved")
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=moved.id,
            page=0,
            query=query,
            status_notice=f"✅ Moved folder <b>{html.escape(moved.name)}</b>.",
        )
        return

    if cmd == "dd" and len(parts) == 2:
        folder_id = int(parts[1])
        folder = await db.get_folder(user.id, folder_id)
        if folder is None:
            await query.answer("Folder not found.", show_alert=True)
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                query=query,
            )
            return

        counts = await db.get_folder_counts(user.id, folder_id)
        if counts.is_empty:
            deleted = await db.delete_folder(user.id, folder_id)
            await query.answer("🗑️ Empty folder deleted")
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=deleted.parent_id,
                page=0,
                query=query,
                status_notice=f"🗑️ Deleted empty folder <b>{html.escape(deleted.name)}</b>.",
            )
            return

        await query.answer()
        await _show_panel(
            context=context,
            chat_id=chat.id,
            text=(
                f"⚠️ <b>Delete Non-Empty Folder?</b>\n\n"
                f"Folder <b>{html.escape(folder.name)}</b> contains:\n"
                f"• <b>{counts.subfolders}</b> subfolder(s)\n"
                f"• <b>{counts.files}</b> file(s)\n\n"
                "Deleting it will remove all nested folders and indexed file entries."
            ),
            reply_markup=folder_delete_confirm_keyboard(folder.id),
            query=query,
        )
        return

    if cmd == "ddc" and len(parts) == 2:
        try:
            deleted = await db.delete_folder(user.id, int(parts[1]))
        except NotFoundError:
            await query.answer("Already deleted.")
            await _render_explorer(
                context=context,
                user_id=user.id,
                chat_id=chat.id,
                folder_id=None,
                query=query,
            )
            return
        await query.answer("🗑️ Folder deleted")
        await _render_explorer(
            context=context,
            user_id=user.id,
            chat_id=chat.id,
            folder_id=deleted.parent_id,
            page=0,
            query=query,
            status_notice=f"🗑️ Deleted folder <b>{html.escape(deleted.name)}</b> and its contents.",
        )
        return

    await query.answer("Unknown action.")


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.exception(
        "Unhandled exception while processing update %r",
        update,
        exc_info=context.error,
    )


def register_handlers(app: Application) -> None:
    """Attach all command, media, text, callback, and error handlers."""
    app.add_handler(CommandHandler(["start", "files"], start_command))
    app.add_handler(CommandHandler("newfolder", new_folder_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(callback_router))

    media_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
    )
    app.add_handler(MessageHandler(media_filter, media_message_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler)
    )
    app.add_error_handler(error_handler)
