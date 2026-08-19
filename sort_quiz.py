"""Сортировка вопросов и удаление дублей в quiz JSON-файлах."""

import json
import sys
import os


def sort_and_dedup(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    data: dict = json.loads(raw)

    seen: dict[str, str] = {}
    dupes = 0
    for q, a in data.items():
        norm = q.strip()
        if norm in seen:
            dupes += 1
            if not seen[norm] and a:
                seen[norm] = a
        else:
            seen[norm] = a

    sorted_data = dict(sorted(seen.items(), key=lambda kv: kv[0].lower()))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted_data, f, ensure_ascii=False, indent=4)
        f.write("\n")

    print(f"{os.path.basename(path)}: {len(data)} -> {len(sorted_data)} "
          f"(удалено дублей: {dupes})")


if __name__ == "__main__":
    targets = sys.argv[1:] or ["quiz_shared.json"]
    for t in targets:
        if not os.path.isfile(t):
            print(f"Файл не найден: {t}")
            continue
        sort_and_dedup(t)
