# app/keyboards.py
from telegram import ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("/start")],
            [KeyboardButton("/register"), KeyboardButton("/login")],
            [KeyboardButton("/kiritish"), KeyboardButton("/cancel")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Buyruq tanlang…",
    )
