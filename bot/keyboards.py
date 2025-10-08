from __future__ import annotations

from telegram import ReplyKeyboardMarkup
from .i18n import t

def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)

# ----- main menu -----
def main_menu_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_birthdays", update=update, context=context), t("btn_groups", update=update, context=context)],
            [t("btn_friends", update=update, context=context), t("btn_settings", update=update, context=context)],
            [t("btn_about", update=update, context=context)],
        ]
    )

# ----- groups -----
def groups_menu_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_group_create", update=update, context=context), t("btn_group_join", update=update, context=context)],
            [t("btn_group_leave", update=update, context=context), t("btn_groups_manage", update=update, context=context)],
            [t("btn_back_main", update=update, context=context)],
        ]
    )

def group_mgmt_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_group_rename", update=update, context=context)],
            [t("btn_group_member_add", update=update, context=context), t("btn_group_member_del", update=update, context=context)],
            [t("btn_back", update=update, context=context)],
        ]
    )

# ----- friends -----
def friends_menu_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_friend_add", update=update, context=context), t("btn_friend_del", update=update, context=context)],
            [t("btn_back_main", update=update, context=context)],
        ]
    )

# ----- settings -----
def settings_menu_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_settings_bday", update=update, context=context), t("btn_settings_tz", update=update, context=context)],
            [t("btn_settings_alert", update=update, context=context), t("btn_settings_lang", update=update, context=context)],
            [t("btn_back_main", update=update, context=context)],
        ]
    )

# ----- about / donate -----
def about_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            ["⭐ 50", "⭐ 100"],
            ["⭐ 500"],
            [t("btn_back_main", update=update, context=context)],
        ]
    )

# single cancel keyboard (used inside convs)
def cancel_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[t("btn_cancel", update=update, context=context)]], resize_keyboard=True, one_time_keyboard=True)

# ----- birthdays nested: wishlist -----
def birthdays_wishlist_kb(*, update=None, context=None) -> ReplyKeyboardMarkup:
    return _kb(
        [
            [t("btn_wishlist_my", update=update, context=context), t("btn_wishlist_edit", update=update, context=context)],
            [t("btn_wishlist_view", update=update, context=context)],
            [t("btn_back_main", update=update, context=context)],
        ]
    )
