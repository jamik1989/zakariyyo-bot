from pathlib import Path
import sys
import py_compile

ROOT = Path(r"D:\My Project\zakariyyo-railway")

ORDER_PATH = ROOT / "app" / "handlers" / "order.py"
CONFIRM_PATH = ROOT / "app" / "handlers" / "confirm.py"

ORDER_FUNC = """def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )

"""

CONFIRM_FUNC = """def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )

"""

def replace_top_level_function(text: str, func_name: str, new_block: str) -> str:
    lines = text.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"def {func_name}("):
            start = i
            break

    if start is None:
        raise RuntimeError(f"{func_name} function not found")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        stripped = line.lstrip()

        # keyingi top-level def/async/class topilsa, shu yerda tugaydi
        if line.startswith("def ") or line.startswith("async def ") or line.startswith("class "):
            end = j
            break

    new_lines = lines[:start] + [new_block] + lines[end:]
    return "".join(new_lines)

def fix_file(path: Path, new_block: str):
    text = path.read_text(encoding="utf-8")
    fixed = replace_top_level_function(text, "_menu_keyboard", new_block)
    path.write_text(fixed, encoding="utf-8", newline="\n")
    print(f"FIXED: {path}")

def main():
    fix_file(ORDER_PATH, ORDER_FUNC)
    fix_file(CONFIRM_PATH, CONFIRM_FUNC)

    py_compile.compile(str(ORDER_PATH), doraise=True)
    py_compile.compile(str(CONFIRM_PATH), doraise=True)
    print("ALL OK")

if __name__ == "__main__":
    main()
