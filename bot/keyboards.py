from __future__ import annotations

from telegram import ReplyKeyboardMarkup


def main_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["🎂 дни рождения", "👪 группы"],
        ["👥 управление друзьями", "⚙️ настройки"],
        ["ℹ️ о проекте"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def groups_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ создать группу", "🔑 присоединиться к группе"],
        ["🚪 покинуть группу", "📝 управление группами"],
        ["⬅️ выйти"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def group_mgmt_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["✏️ переименовать группу"],
        ["➕ добавить участника", "🗑 удалить участника"],
        ["⬅️ выйти"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def friends_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ добавить друга", "➖ удалить друга"],
        ["⬅️ выйти"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def settings_menu_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["дата рождения", "часовой пояс"],
        ["отложенность", "язык"],
        ["◀️ вернуться в главное меню"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def about_kb() -> ReplyKeyboardMarkup:
    rows = [
        ["⭐ 50", "⭐ 100", "⭐ 500"],
        ["⬅️ выйти"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)
