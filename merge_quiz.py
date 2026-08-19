"""
Объединение вопросов викторины из чужого JSON с твоим responses.json.

Что делает:
- НЕ трогает settings, responses и любые другие секции твоего файла.
- Из чужого файла берёт квиз-пары "вопрос" -> "ответ". Поддерживает 3 формата:
    1) {"quiz": {"вопрос": "ответ", ...}}       — responses.json
    2) {"вопрос": "ответ", ...}                  — quiz_shared.json / pending_quiz.json
    3) {"quiz": {...}, "pending_quiz": {...}}    — соберёт из всех разделов
- Добавляет в твой quiz только те вопросы, которых у тебя ещё НЕТ.
- Если у тебя вопрос уже есть, но ответ пустой ("") — подставит ответ из чужого.
- Твои непустые ответы НЕ меняются (даже если у чужого другой ответ).
- Заодно докидывает найденные ответы в твой pending_quiz.json, если он есть.
- Перед изменениями делает резервную копию responses.json.backup.

Использование:
    python merge_quiz.py "путь\\к\\чужому.json"
    (или перетащить чужой JSON на merge_quiz.bat)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
MY_CONFIG = HERE / "responses.json"
MY_PENDING = HERE / "pending_quiz.json"


def extract_quiz(data) -> dict[str, str]:
    """Достаёт словарь "вопрос" -> "ответ" из произвольной JSON-структуры."""
    out: dict[str, str] = {}
    if isinstance(data, dict):
        # Известные секции — quiz, pending_quiz, remote_quiz.
        for key in ("quiz", "pending_quiz", "remote_quiz", "shared", "questions"):
            sect = data.get(key)
            if isinstance(sect, dict):
                for k, v in sect.items():
                    if isinstance(v, list):
                        v = next((str(x) for x in v if str(x).strip()), "")
                    out[str(k).strip()] = str(v).strip() if v is not None else ""
        # Плоский формат: {"вопрос": "ответ", ...}
        looks_flat = all(
            not isinstance(v, (dict, list)) for v in data.values()
        ) and data
        if looks_flat and not out:
            for k, v in data.items():
                out[str(k).strip()] = str(v).strip() if v is not None else ""
    return out


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_pretty(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python merge_quiz.py <путь к чужому JSON>")
        return 1

    other_path = Path(sys.argv[1]).expanduser().resolve()
    if not other_path.exists():
        print(f"Файл не найден: {other_path}", file=sys.stderr)
        return 1

    if not MY_CONFIG.exists():
        print(f"Не найден твой responses.json рядом со скриптом: {MY_CONFIG}",
              file=sys.stderr)
        return 1

    try:
        other = load_json(other_path)
    except Exception as e:
        print(f"Не удалось прочитать {other_path.name}: {e}", file=sys.stderr)
        return 1

    other_quiz = extract_quiz(other)
    if not other_quiz:
        print("В файле не нашлось ни одной пары 'вопрос' -> 'ответ'.")
        return 1

    print(f"[merge] Прочитано вопросов из чужого файла: {len(other_quiz)}")

    my = load_json(MY_CONFIG)
    if not isinstance(my, dict):
        print("Твой responses.json имеет неожиданный формат.", file=sys.stderr)
        return 1

    if "quiz" not in my or not isinstance(my.get("quiz"), dict):
        my["quiz"] = {}
    my_quiz: dict = my["quiz"]

    added = 0
    filled = 0
    for q, a in other_quiz.items():
        if not q:
            continue
        if q not in my_quiz:
            my_quiz[q] = a
            added += 1
        else:
            existing = my_quiz[q]
            # Пустой ответ у нас — заполняем из чужого (если у него не пусто).
            if isinstance(existing, str) and not existing.strip() and a:
                my_quiz[q] = a
                filled += 1
            # Список из одной пустой строки — тот же случай.
            elif isinstance(existing, list) and not any(str(x).strip() for x in existing) and a:
                my_quiz[q] = a
                filled += 1

    if added == 0 and filled == 0:
        print("[merge] Ничего нового: все вопросы уже есть с ответами.")
    else:
        backup = MY_CONFIG.with_suffix(".json.backup")
        shutil.copyfile(MY_CONFIG, backup)
        save_json_pretty(MY_CONFIG, my)
        print(f"[merge] responses.json обновлён. Резервная копия: {backup.name}")
        print(f"        добавлено новых вопросов: {added}")
        print(f"        заполнено пустых ответов: {filled}")

    # Заодно — если у нас есть pending_quiz.json, попробуем заполнить и там.
    if MY_PENDING.exists():
        try:
            pending = load_json(MY_PENDING)
            if isinstance(pending, dict):
                pfilled = 0
                for q in list(pending.keys()):
                    v = pending[q]
                    if isinstance(v, str) and not v.strip() and other_quiz.get(q, "").strip():
                        pending[q] = other_quiz[q]
                        pfilled += 1
                if pfilled:
                    save_json_pretty(MY_PENDING, pending)
                    print(f"[merge] pending_quiz.json — заполнено ответов: {pfilled}")
        except Exception as e:
            print(f"[merge] pending_quiz.json пропущен: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
