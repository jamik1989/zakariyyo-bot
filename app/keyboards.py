# app/keyboards.py
from telegram import ReplyKeyboardMarkup, KeyboardButton


def operator_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )
