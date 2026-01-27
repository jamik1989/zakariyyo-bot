def parse_check_text(text: str) -> dict:
    # Minimal: keyin sizning chek formatga moslab kengaytiramiz
    # Hozircha: butun textni "comment" sifatida olamiz
    return {
        "comment": text.strip(),
        "items": []  # keyin tovarlar ajratamiz
    }
