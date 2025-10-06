from __future__ import annotations

from telegram import ReplyKeyboardMarkup

# main menu keyboard
def main_menu_kb() -> ReplyKeyboardMarkup:
    # order: birthdays, friends, groups, settings, about
    return ReplyKeyboardMarkup(
        [
            ["🎂 дни рождения"],
            ["👥 управление друзьями", "👪 группы"],
            ["⚙️ настройки", "ℹ️ о проекте"],
        ],
        resize_keyboard=True,
    )

# friends menu keyboard
def friends_menu_kb() -> ReplyKeyboardMarkup:
    # actions only; the list is printed by the handler
    return ReplyKeyboardMarkup(
        [
            ["➕ добавить друга", "➖ удалить друга"],
            ["⬅️ выйти"],
        ],
        resize_keyboard=True,
    )

# groups menu keyboard
def groups_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["➕ создать группу", "🔑 присоединиться к группе"],
            ["📝 управление группами", "🚪 покинуть группу"],
            ["⬅️ выйти"],
        ],
        resize_keyboard=True,
    )

# group management keyboard
def group_mgmt_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["✏️ переименовать группу", "➕ добавить участника"],
            ["🗑 удалить участника"],
            ["👪 группы", "⬅️ выйти"],
        ],
        resize_keyboard=True,
    )

# settings menu keyboard (merged settings + stats)
def settings_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["дата рождения", "часовой пояс"],
            ["отложенность", "язык"],
            ["⬅️ выйти"],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

# about screen keyboard with stars
def about_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["⭐ 50", "⭐ 100", "⭐ 500"],
            ["⬅️ выйти"],
        ],
        resize_keyboard=True,
    )
