"""Inline keyboard builders enforcing Telegram's 64-byte callback_data limit."""

from __future__ import annotations

import math
from typing import Sequence, TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import FileRecord, FolderRecord, TagRecord, VALID_FILE_TYPES

MAX_CALLBACK_BYTES = 64

FILE_TYPE_ICONS: dict[str, str] = {
    "document": "📄",
    "photo": "🖼️",
    "video": "🎬",
    "audio": "🎵",
    "voice": "🎤",
}

FILE_TYPE_LABELS: dict[str, str] = {
    "document": "Documents",
    "photo": "Photos",
    "video": "Videos",
    "audio": "Audio",
    "voice": "Voice",
}

TYPE_TO_CODE: dict[str, str] = {
    "document": "d",
    "photo": "p",
    "video": "v",
    "audio": "a",
    "voice": "o",
}

CODE_TO_TYPE: dict[str, str] = {code: ftype for ftype, code in TYPE_TO_CODE.items()}

T = TypeVar("T")


def cb(*parts: object) -> str:
    """Build a compact ID-based callback token verified to fit in <= 64 UTF-8 bytes."""
    token = ":".join(str(part) for part in parts)
    if len(token.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(
            f"Callback data exceeds Telegram's {MAX_CALLBACK_BYTES}-byte limit: {token!r}"
        )
    return token


def truncate(text: str, max_len: int = 38) -> str:
    """Truncate a button label cleanly while preserving readability."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 1]}…"


def paginate(
    items: Sequence[T], page: int, page_size: int
) -> tuple[list[T], int, int]:
    """Slice a sequence for the requested 0-based page, clamping bounds safely."""
    total_pages = max(1, math.ceil(len(items) / page_size))
    clamped = max(0, min(page, total_pages - 1))
    start = clamped * page_size
    end = start + page_size
    return list(items[start:end]), clamped, total_pages


def _pagination_row(
    page: int,
    total_pages: int,
    prev_cb: str,
    next_cb: str,
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=prev_cb))
    row.append(
        InlineKeyboardButton(f"· {page + 1}/{total_pages} ·", callback_data=cb("noop"))
    )
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=next_cb))
    return row


def explorer_keyboard(
    *,
    folder_id: int | None,
    parent_id: int | None,
    folders: Sequence[FolderRecord],
    files: Sequence[FileRecord],
    page: int,
    page_size: int,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build the main single-message explorer keyboard with pagination and actions."""
    combined: list[tuple[str, FolderRecord | FileRecord]] = [
        ("folder", f) for f in folders
    ] + [("file", f) for f in files]

    page_items, clamped_page, total_pages = paginate(combined, page, page_size)
    rows: list[list[InlineKeyboardButton]] = []

    for kind, item in page_items:
        if kind == "folder":
            assert isinstance(item, FolderRecord)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📁 {truncate(item.name)}",
                        callback_data=cb("dir", item.id, 0),
                    )
                ]
            )
        else:
            assert isinstance(item, FileRecord)
            icon = FILE_TYPE_ICONS.get(item.file_type, "📎")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{icon} {truncate(item.display_name)}",
                        callback_data=cb("f", item.id),
                    )
                ]
            )

    fid_token = folder_id or 0
    if total_pages > 1:
        rows.append(
            _pagination_row(
                clamped_page,
                total_pages,
                cb("dir", fid_token, clamped_page - 1),
                cb("dir", fid_token, clamped_page + 1),
            )
        )

    if folder_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "⬆️ Up", callback_data=cb("dir", parent_id or 0, 0)
                ),
                InlineKeyboardButton(
                    "⚙️ Folder Actions", callback_data=cb("dm", folder_id)
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("➕ New Folder", callback_data=cb("dn", fid_token)),
            InlineKeyboardButton("🔍 Search", callback_data=cb("srch")),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🏷️ Filter by Tag", callback_data=cb("ftag", 0)),
            InlineKeyboardButton("📂 Filter by Type", callback_data=cb("ftyp")),
        ]
    )

    return InlineKeyboardMarkup(rows), clamped_page, total_pages


def filtered_files_keyboard(
    *,
    files: Sequence[FileRecord],
    page: int,
    page_size: int,
    page_cb_prefix: tuple[object, ...],
    current_folder_id: int | None,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build a paginated file listing keyboard for search, tag, or type filters."""
    page_items, clamped_page, total_pages = paginate(files, page, page_size)
    rows: list[list[InlineKeyboardButton]] = []

    for item in page_items:
        icon = FILE_TYPE_ICONS.get(item.file_type, "📎")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {truncate(item.display_name)}",
                    callback_data=cb("f", item.id),
                )
            ]
        )

    if total_pages > 1:
        rows.append(
            _pagination_row(
                clamped_page,
                total_pages,
                cb(*page_cb_prefix, clamped_page - 1),
                cb(*page_cb_prefix, clamped_page + 1),
            )
        )

    nav_row = [
        InlineKeyboardButton(
            "⬅️ Back to Explorer",
            callback_data=cb("dir", current_folder_id or 0, 0),
        )
    ]
    if current_folder_id is not None:
        nav_row.append(InlineKeyboardButton("🏠 Root", callback_data=cb("home")))
    rows.append(nav_row)

    return InlineKeyboardMarkup(rows), clamped_page, total_pages


def file_detail_keyboard(
    file_row_id: int, current_folder_id: int | None
) -> InlineKeyboardMarkup:
    """Build the inline keyboard for the File Detail view."""
    rows = [
        [InlineKeyboardButton("📤 Send File", callback_data=cb("fs", file_row_id))],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=cb("fr", file_row_id)),
            InlineKeyboardButton(
                "📁 Move",
                callback_data=cb("fm", file_row_id, current_folder_id or 0, 0),
            ),
        ],
        [
            InlineKeyboardButton(
                "🏷️ Edit Tags", callback_data=cb("ft", file_row_id, 0)
            ),
            InlineKeyboardButton("🗑️ Delete", callback_data=cb("fd", file_row_id)),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=cb("back"))],
    ]
    return InlineKeyboardMarkup(rows)


def file_delete_confirm_keyboard(file_row_id: int) -> InlineKeyboardMarkup:
    """Confirmation buttons before deleting a file metadata record."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Confirm Delete", callback_data=cb("fdc", file_row_id)
                ),
                InlineKeyboardButton("❌ Cancel", callback_data=cb("f", file_row_id)),
            ]
        ]
    )


def folder_actions_keyboard(folder: FolderRecord) -> InlineKeyboardMarkup:
    """Build actions keyboard for the currently open folder."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Rename Folder", callback_data=cb("dr", folder.id)
                ),
                InlineKeyboardButton(
                    "📁 Move Folder",
                    callback_data=cb("dmv", folder.id, folder.parent_id or 0, 0),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑️ Delete Folder", callback_data=cb("dd", folder.id)
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back to Folder", callback_data=cb("dir", folder.id, 0)
                )
            ],
        ]
    )


def folder_delete_confirm_keyboard(folder_id: int) -> InlineKeyboardMarkup:
    """Confirmation buttons before recursively deleting a non-empty folder."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Yes, Delete Everything",
                    callback_data=cb("ddc", folder_id),
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=cb("dir", folder_id, 0)
                ),
            ]
        ]
    )


def move_picker_keyboard(
    *,
    mode: str,
    target_id: int,
    browse_folder_id: int | None,
    browse_parent_id: int | None,
    subfolders: Sequence[FolderRecord],
    page: int,
    page_size: int,
    excluded_ids: set[int] | None = None,
    can_confirm: bool = True,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build a folder-tree picker keyboard for moving a file or folder."""
    if mode not in ("file", "folder"):
        raise ValueError(f"Unsupported move picker mode: {mode}")

    nav_prefix = "fm" if mode == "file" else "dmv"
    confirm_prefix = "fmc" if mode == "file" else "dmc"
    cancel_cb = cb("f", target_id) if mode == "file" else cb("dm", target_id)

    excluded = excluded_ids or set()
    visible_folders = [f for f in subfolders if f.id not in excluded]
    page_items, clamped_page, total_pages = paginate(visible_folders, page, page_size)

    rows: list[list[InlineKeyboardButton]] = []
    for folder in page_items:
        rows.append(
            [
                InlineKeyboardButton(
                    f"📁 {truncate(folder.name)}",
                    callback_data=cb(nav_prefix, target_id, folder.id, 0),
                )
            ]
        )

    browse_token = browse_folder_id or 0
    if total_pages > 1:
        rows.append(
            _pagination_row(
                clamped_page,
                total_pages,
                cb(nav_prefix, target_id, browse_token, clamped_page - 1),
                cb(nav_prefix, target_id, browse_token, clamped_page + 1),
            )
        )

    if browse_folder_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "⬆️ Up",
                    callback_data=cb(nav_prefix, target_id, browse_parent_id or 0, 0),
                )
            ]
        )

    footer: list[InlineKeyboardButton] = []
    if can_confirm:
        footer.append(
            InlineKeyboardButton(
                "✅ Move Here",
                callback_data=cb(confirm_prefix, target_id, browse_token),
            )
        )
    footer.append(InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb))
    rows.append(footer)

    return InlineKeyboardMarkup(rows), clamped_page, total_pages


def tag_editor_keyboard(
    *,
    file_row_id: int,
    all_tags: Sequence[TagRecord],
    attached_tag_ids: set[int],
    page: int,
    page_size: int,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build toggle buttons for attaching/detaching tags on a file."""
    page_items, clamped_page, total_pages = paginate(all_tags, page, page_size)
    rows: list[list[InlineKeyboardButton]] = []

    current_row: list[InlineKeyboardButton] = []
    for tag in page_items:
        prefix = "✅" if tag.id in attached_tag_ids else "⬜"
        btn = InlineKeyboardButton(
            f"{prefix} #{truncate(tag.name, 22)}",
            callback_data=cb("ftt", file_row_id, tag.id, clamped_page),
        )
        current_row.append(btn)
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    if total_pages > 1:
        rows.append(
            _pagination_row(
                clamped_page,
                total_pages,
                cb("ft", file_row_id, clamped_page - 1),
                cb("ft", file_row_id, clamped_page + 1),
            )
        )

    rows.append(
        [
            InlineKeyboardButton(
                "✏️ Add / Remove by Text", callback_data=cb("fta", file_row_id)
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton("⬅️ Back to File", callback_data=cb("f", file_row_id))]
    )
    return InlineKeyboardMarkup(rows), clamped_page, total_pages


def tag_filter_keyboard(
    *,
    tags: Sequence[TagRecord],
    page: int,
    page_size: int,
    current_folder_id: int | None,
) -> tuple[InlineKeyboardMarkup, int, int]:
    """Build a paginated tag picker keyboard for filtering files by tag."""
    page_items, clamped_page, total_pages = paginate(tags, page, page_size)
    rows: list[list[InlineKeyboardButton]] = []

    for tag in page_items:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🏷️ #{truncate(tag.name, 28)} ({tag.file_count})",
                    callback_data=cb("tag", tag.id, 0),
                )
            ]
        )

    if total_pages > 1:
        rows.append(
            _pagination_row(
                clamped_page,
                total_pages,
                cb("ftag", clamped_page - 1),
                cb("ftag", clamped_page + 1),
            )
        )

    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ Back to Explorer",
                callback_data=cb("dir", current_folder_id or 0, 0),
            )
        ]
    )
    return InlineKeyboardMarkup(rows), clamped_page, total_pages


def type_filter_keyboard(
    *,
    type_counts: dict[str, int],
    current_folder_id: int | None,
) -> InlineKeyboardMarkup:
    """Build a file-type filter picker keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for ftype in VALID_FILE_TYPES:
        icon = FILE_TYPE_ICONS[ftype]
        label = FILE_TYPE_LABELS[ftype]
        count = type_counts.get(ftype, 0)
        code = TYPE_TO_CODE[ftype]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {label} ({count})",
                    callback_data=cb("typ", code, 0),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ Back to Explorer",
                callback_data=cb("dir", current_folder_id or 0, 0),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def cancel_input_keyboard() -> InlineKeyboardMarkup:
    """Single Cancel button shown while waiting for text input."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=cb("cxl"))]]
    )
