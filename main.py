"""
Автоответчик для Minecraft.

Читает logs/latest.log в реальном времени, ищет совпадения по правилам из
responses.json и отправляет ответ в игровой чат через эмуляцию клавиатуры.

Особенности:
- Задержка перед ответом: случайная в диапазоне [min_delay, max_delay].
- Файл responses.json автоматически перечитывается при изменении — новые
  варианты можно добавлять на лету, без перезапуска программы.
- Игнорирование собственных сообщений (по нику) — чтобы бот не отвечал сам себе.
- Поддержка трёх режимов сопоставления: contains, exact, regex.
- Проигрывание звука при срабатывании конкретного правила/вопроса (поле
  "sound" в responses.json). Работает и в обычном .py, и в скомпилированной
  PyInstaller-версии — путь к .wav ищется рядом с exe/скриптом, если указан
  не абсолютный путь.
"""

from __future__ import annotations

import ctypes
import gzip
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from pynput.keyboard import Controller, Key, KeyCode
except ImportError:
    print("Требуется библиотека pynput. Установите: pip install pynput", file=sys.stderr)
    raise

# winsound — часть стандартной библиотеки Python на Windows. PyInstaller
# подхватывает его автоматически при сборке, отдельно ставить/указывать
# ничего не нужно. На не-Windows модуль просто отсутствует — там звук
# проигрываться не будет (play_sound тихо выйдет).
try:
    import winsound
except ImportError:
    winsound = None


def _chat_key(ch: str):
    """Возвращает объект клавиши для pynput. Для латинских букв и цифр
    используется VK-код (0x41..0x5A / 0x30..0x39), чтобы клавиша открытия
    чата срабатывала независимо от активной раскладки Windows
    (иначе на русской раскладке `t` может отправиться как «е» и чат
    не откроется)."""
    ch = str(ch).strip().lower()
    if len(ch) == 1:
        if "a" <= ch <= "z":
            return KeyCode.from_vk(ord(ch.upper()))  # VK_A..VK_Z
        if "0" <= ch <= "9":
            return KeyCode.from_vk(ord(ch))  # VK_0..VK_9
    return ch


# В скомпилированной версии (PyInstaller onefile) __file__ указывает во
# временную папку распаковки, а рядом с самим .exe лежит responses.json —
# поэтому в frozen-режиме берём путь от sys.executable.
if getattr(sys, "frozen", False):
    CONFIG_PATH = Path(sys.executable).with_name("responses.json")
else:
    CONFIG_PATH = Path(__file__).with_name("responses.json")

# Регулярка для строк чата в latest.log.
# Пример: [12:34:56] [Render thread/INFO]: <Nick> текст
# Некоторые серверы форматируют иначе (без угловых скобок), поэтому есть запасные паттерны.
CHAT_PATTERNS = [
    # <Ник> текст
    re.compile(r"\]:\s*<(?P<nick>[^>]+)>\s*(?P<text>.+)$"),
    # [CHAT] Ник: текст
    re.compile(r"\]:\s*\[CHAT\]\s*(?P<nick>[^:>\s]+)\s*[:>]\s*(?P<text>.+)$"),
]

# Fallback: любое сообщение в клиентском чате (Render thread/Chat/INFO).
# Используется в том числе для системных сообщений от плагинов (например,
# "[Викторина] Какой плагин добавляет регионы?"). Ник в этом случае — "".
SYSTEM_CHAT_PATTERN = re.compile(
    r"\[(?:Render thread|Client thread|Chat)/INFO\]:\s*(?P<text>.+)$"
)

# Minecraft §-коды форматирования (§a, §l, §r и т.д.) — убираем из текста.
_MC_COLOR_CODE = re.compile(r"§[0-9a-fk-or]", re.IGNORECASE)

# --- Мини-калькулятор для вопросов "Сколько будет ..." ----------------------

MATH_TRIGGERS = ("сколько будет", "чему равно", "посчитай")

# Замены русских слов на арифметические знаки.
_MATH_WORDS = [
    (r"\bплюс\b", "+"),
    (r"\bминус\b", "-"),
    (r"\bумножить(?:\s+на)?\b", "*"),
    (r"\bумножено(?:\s+на)?\b", "*"),
    (r"\bразделить(?:\s+на)?\b", "/"),
    (r"\bделить(?:\s+на)?\b", "/"),
    (r"\bв\s+степени\b", "**"),
    ("×", "*"),
    ("х", "*"),  # русская «х» между числами часто = умножение
    ("·", "*"),
    ("÷", "/"),
    (":", "/"),
    ("−", "-"),
    ("–", "-"),
    ("—", "-"),
    (",", "."),
]

# Разрешённые символы в очищенном выражении.
_MATH_SAFE = re.compile(r"^[\d\s+\-*/().]+$")


def try_math_answer(text: str) -> str | None:
    """Если сообщение похоже на "сколько будет <выражение>?" — вычислить
    результат и вернуть строкой. Иначе вернуть None."""
    low = text.lower()
    if not any(t in low for t in MATH_TRIGGERS):
        return None

    # Отрезаем всё до триггера включительно.
    idx = -1
    for t in MATH_TRIGGERS:
        i = low.find(t)
        if i != -1 and (idx == -1 or i < idx):
            idx = i + len(t)
    expr = text[idx:] if idx != -1 else text

    # Заменяем словесные операторы, чистим хвост.
    expr = expr.lower()
    for pat, repl in _MATH_WORDS:
        expr = re.sub(pat, repl, expr)
    expr = expr.strip(" ?!.\t\r\n=")

    if not expr or not _MATH_SAFE.match(expr):
        return None

    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — вход отфильтрован _MATH_SAFE
    except Exception:
        return None

    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, float):
        value = round(value, 4)
    return str(value)


@dataclass
class Rule:
    triggers: list[str]
    replies: list[str] = field(default_factory=list)
    match: str = "contains"  # contains | exact | regex
    case_sensitive: bool = False
    cooldown: float = 0.0  # секунд между срабатываниями этого правила
    ignore_dedup: bool = False
    sound: str = ""  # путь к .wav, проигрывается при срабатывании правила
    _last_fired: float = 0.0

    def matches(self, text: str) -> bool:
        haystack = text if self.case_sensitive else text.lower()
        for trig in self.triggers:
            needle = trig if self.case_sensitive else trig.lower()
            if self.match == "exact":
                if haystack.strip() == needle.strip():
                    return True
            elif self.match == "regex":
                flags = 0 if self.case_sensitive else re.IGNORECASE
                if re.search(trig, text, flags):
                    return True
            else:  # contains
                if needle in haystack:
                    return True
        return False

    def pick_reply(self) -> str | None:
        """Возвращает случайный ответ, либо None, если у правила есть
        только звук без текстовых ответов (правило "тихого" алерта)."""
        if not self.replies:
            return None
        return random.choice(self.replies)


@dataclass
class Settings:
    min_delay: float = 2.0
    max_delay: float = 4.0
    log_path: str = ""
    chat_key: str = "t"  # клавиша открытия чата
    ignore_own_username: str = ""  # ваш ник, чтобы не отвечать себе
    type_interval: float = 0.02  # пауза между нажатиями клавиш (реалистичный ввод)
    block_user_input: bool = True  # блокировать клавиатуру/мышь пользователя во время печати бота
    auto_focus: bool = True  # автоматически переключаться на окно Minecraft перед ответом
    remote_quiz_url: str = ""  # URL общей викторины (JSON вида {"вопрос": "ответ"})
    remote_quiz_interval_min: int = 60  # как часто скачивать обновления, в минутах


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.mtime: float = 0.0
        self.settings = Settings()
        self.rules: list[Rule] = []
        self._saving = False
        self.load()

    def load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        s = raw.get("settings", {})
        self.settings = Settings(
            min_delay=float(s.get("min_delay", 2.0)),
            max_delay=float(s.get("max_delay", 4.0)),
            log_path=os.path.expandvars(os.path.expanduser(s.get("log_path", ""))),
            chat_key=str(s.get("chat_key", "t")),
            ignore_own_username=str(s.get("ignore_own_username", "")),
            type_interval=float(s.get("type_interval", 0.02)),
            block_user_input=bool(s.get("block_user_input", True)),
            auto_focus=bool(s.get("auto_focus", True)),
            remote_quiz_url=str(s.get("remote_quiz_url", "")),
            remote_quiz_interval_min=int(s.get("remote_quiz_interval_min", 60)),
        )

        rules: list[Rule] = []
        for entry in raw.get("responses", []):
            rules.append(Rule(
                triggers=list(entry["triggers"]),
                replies=list(entry.get("replies", [])),
                match=str(entry.get("match", "contains")),
                case_sensitive=bool(entry.get("case_sensitive", False)),
                cooldown=float(entry.get("cooldown", 0.0)),
                ignore_dedup=bool(entry.get("ignore_dedup", False)),
                sound=str(entry.get("sound", "")).strip(),
            ))

        # Упрощённая секция "quiz": словарь "вопрос-подстрока" -> "ответ".
        # Каждая пара превращается в отдельное правило contains/без регистра.
        # Ответ может быть:
        #   - строкой                              -> один вариант ответа
        #   - списком строк                        -> случайный вариант ответа
        #   - объектом {"answer": ..., "sound": ..} -> ответ (строка/список)
        #     плюс путь к звуку, который проигрывается при совпадении.
        #     Поле "answer" можно не указывать — тогда правило будет играть
        #     только звук, без отправки сообщения в чат.
        def _add_quiz(mapping: dict) -> int:
            n = 0
            for question, answer in mapping.items():
                sound = ""
                value = answer
                if isinstance(value, dict):
                    sound = str(value.get("sound", "")).strip()
                    value = value.get("answer", "")

                if isinstance(value, list):
                    replies = [str(r) for r in value if str(r).strip()]
                else:
                    replies = [str(value)] if str(value).strip() else []

                # Пропускаем записи без ответа и без звука (например, из
                # pending_quiz.json, где вопросы ждут заполнения пользователем).
                if not replies and not sound:
                    continue

                rules.append(Rule(
                    triggers=[str(question)],
                    replies=replies,
                    match="contains",
                    case_sensitive=False,
                    cooldown=0.0,
                    sound=sound,
                ))
                n += 1
            return n

        quiz = raw.get("quiz", {})
        if not isinstance(quiz, dict):
            quiz = {}
        n_quiz = _add_quiz(quiz)

        # Пересортировка quiz: с ответами первыми, без — в конце.
        def _quiz_is_empty(v: Any) -> bool:
            if isinstance(v, str):
                return not v.strip()
            if isinstance(v, dict):
                return not str(v.get("answer", "")).strip()
            return True

        sorted_quiz = dict(sorted(
            quiz.items(),
            key=lambda kv: (not kv[1].strip() if isinstance(kv[1], str) else True,
                            kv[0].lower()),
        ))
        if list(sorted_quiz.keys()) != list(quiz.keys()):
            raw["quiz"] = sorted_quiz
            self._saving = True
            self.path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._saving = False

        # Более специфичные (длинные) триггеры проверяются раньше — иначе
        # общий триггер вроде "Викторина" мог бы перебить конкретный вопрос.
        rules.sort(key=lambda r: -max((len(t) for t in r.triggers), default=0))
        self.rules = rules
        self.mtime = self.path.stat().st_mtime
        print(f"[config] Загружено правил: {len(self.rules)} (quiz: {n_quiz})")

    def maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self.mtime and not getattr(self, "_saving", False):
            try:
                self.load()
            except Exception as e:
                print(f"[config] Ошибка перезагрузки: {e}", file=sys.stderr)


def follow(path: Path):
    """Аналог `tail -F`: начинает с конца файла и отдаёт новые строки.
    Корректно обрабатывает ротацию (пересоздание latest.log при запуске игры)."""
    while not path.exists():
        print(f"[log] Ожидаю появления файла: {path}")
        time.sleep(2)

    f = path.open("r", encoding="utf-8", errors="replace")
    f.seek(0, os.SEEK_END)
    inode = path.stat().st_ino if hasattr(path.stat(), "st_ino") else None

    while True:
        line = f.readline()
        if line:
            yield line.rstrip("\n")
            continue

        time.sleep(0.25)
        try:
            st = path.stat()
        except FileNotFoundError:
            continue

        new_inode = st.st_ino if hasattr(st, "st_ino") else None
        if new_inode is not None and inode is not None and new_inode != inode:
            print("[log] Обнаружена ротация файла, переоткрываю.")
            try:
                f.close()
            except Exception:
                pass
            f = path.open("r", encoding="utf-8", errors="replace")
            inode = new_inode
            continue

        if st.st_size < f.tell():
            print("[log] Файл усечён, переоткрываю.")
            try:
                f.close()
            except Exception:
                pass
            f = path.open("r", encoding="utf-8", errors="replace")
            inode = new_inode


def _strip_mc_codes(s: str) -> str:
    """Убирает Minecraft §-коды форматирования из строки."""
    return _MC_COLOR_CODE.sub("", s)


def parse_chat(line: str) -> tuple[str, str] | None:
    for pat in CHAT_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group("nick"), _strip_mc_codes(m.group("text")).strip()

    m = SYSTEM_CHAT_PATTERN.search(line)
    if m:
        text = _strip_mc_codes(m.group("text")).strip()
        # Отсеиваем очевидно неигровые строки клиента.
        low = text.lower()
        noisy = ("loaded", "saving", "stopping", "starting", "compil",
                 "opengl", "sound engine", "mod ", "reloading", "chunk")
        if any(k in low for k in noisy):
            return None
        return "", text

    return None


_BLOCK_WARNED = False


def _block_user_input(block: bool) -> bool:
    """Блокировка / разблокировка ввода пользователя на Windows через
    user32!BlockInput. Синтетический ввод от SendInput (pynput) продолжает
    работать. Требует прав администратора: без них функция возвращает 0.
    На не-Windows тихо возвращает False."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.BlockInput(bool(block)))
    except Exception:
        return False


def _focus_minecraft() -> bool:
    """Находит окно с «Minecraft» в заголовке и переключает на него (Windows).
    Возвращает True если окно найдено и активировано."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = [None]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _):
            if user32.IsWindowVisible(h):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(h, buf, 256)
                if "lemoncraft" in buf.value.lower():
                    hwnd[0] = h
                    return False
            return True

        user32.EnumWindows(cb, 0)
        if hwnd[0]:
            user32.ShowWindow(hwnd[0], 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd[0])
            time.sleep(0.3)
            return True
    except Exception:
        pass
    return False


def _copy_to_clipboard(text: str) -> bool:
    """Копирует текст в буфер обмена Windows (CF_UNICODETEXT).
    Работает независимо от раскладки клавиатуры."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p

        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            encoded = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(0x0002, len(encoded))
            if not h:
                return False
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, encoded, len(encoded))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(13, h)
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


class Typist:
    def __init__(self, settings: Settings):
        self.kb = Controller()
        self.settings = settings

    def _open_chat_and_paste(self, message: str) -> None:
        s = self.settings
        key = _chat_key(s.chat_key)
        self.kb.press(key)
        self.kb.release(key)
        time.sleep(0.25)

        if _copy_to_clipboard(message):
            a_key = KeyCode.from_vk(0x41)
            v_key = KeyCode.from_vk(0x56)
            # Ctrl+A — выделить всё (убирает лишний символ от клавиши чата
            # на русской раскладке, где T → "е").
            self.kb.press(Key.ctrl_l)
            self.kb.press(a_key)
            self.kb.release(a_key)
            self.kb.release(Key.ctrl_l)
            time.sleep(0.05)
            # Ctrl+V — вставка заменяет выделение.
            self.kb.press(Key.ctrl_l)
            self.kb.press(v_key)
            self.kb.release(v_key)
            self.kb.release(Key.ctrl_l)
        else:
            for ch in message:
                self.kb.type(ch)
                if s.type_interval > 0:
                    time.sleep(s.type_interval)

        time.sleep(0.05)
        self.kb.press(Key.enter)
        self.kb.release(Key.enter)

    def send_chat(self, message: str) -> None:
        # Попытка 1: работает из обычного состояния.
        self._open_chat_and_paste(message)
        time.sleep(0.3)
        # Esc: закроет инвентарь (если открыт) или откроет паузу (если нет).
        self.kb.press(Key.esc)
        self.kb.release(Key.esc)
        time.sleep(0.2)
        # Попытка 2: работает если был инвентарь (теперь закрыт).
        self._open_chat_and_paste(message)
        time.sleep(0.2)
        # Финальный Esc: закроет паузу, если она была открыта из попытки 1.
        self.kb.press(Key.esc)
        self.kb.release(Key.esc)


# --- Сбор вопросов викторины из архивных логов -----------------------------

# Строка чата, содержащая вопрос викторины.
_QUIZ_LINE_MARK = "[Викторина]"

# Служебные строки, которые не являются вопросами.
_QUIZ_SKIP_SUBSTR = ("победил", "правильный ответ", "викторина окончена",
                     "начинается", "начинаем", "закончилась")

# Математические вопросы решает встроенный калькулятор, вручную не нужны.
_MATH_SKIP = ("сколько будет", "чему равно", "посчитай")


def _iter_log_lines(folder: Path):
    """Итерирует по строкам всех *.log и *.log.gz в папке (не рекурсивно)."""
    if not folder.is_dir():
        return
    for p in folder.iterdir():
        try:
            if p.suffix == ".log":
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        yield line
            elif p.name.endswith(".log.gz"):
                with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        yield line
        except Exception as e:
            print(f"[collect] Ошибка чтения {p.name}: {e}", file=sys.stderr)


def collect_pending_questions(log_folder: Path, known_questions: set[str],
                               config_path: Path) -> int:
    """Проходит по старым логам, вытаскивает уникальные вопросы викторины,
    добавляет их в responses.json → quiz с пустым ответом.
    Возвращает количество новых записей."""
    if not config_path.exists():
        return 0
    try:
        cfg_data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[collect] Не удалось прочитать {config_path.name}: {e}",
              file=sys.stderr)
        return 0
    if not isinstance(cfg_data, dict):
        return 0

    quiz = cfg_data.get("quiz", {})
    if not isinstance(quiz, dict):
        quiz = {}

    found: dict[str, str] = {}
    for line in _iter_log_lines(log_folder):
        if _QUIZ_LINE_MARK not in line:
            continue
        low = line.lower()
        if any(s in low for s in _QUIZ_SKIP_SUBSTR):
            continue

        i = line.find(_QUIZ_LINE_MARK)
        text = line[i + len(_QUIZ_LINE_MARK):].strip()
        text = text.rstrip("\r\n\t ")
        if not text:
            continue

        low_text = text.lower()
        if any(m in low_text for m in _MATH_SKIP):
            continue
        if text in quiz or text in found:
            continue
        if any(k.lower() in low_text or low_text in k.lower()
               for k in known_questions):
            continue

        found[text] = ""

    if not found:
        return 0
    return _merge_quiz_into_config(found, config_path)


def _merge_quiz_into_config(source: dict[str, str], config_path: Path) -> int:
    """Мержит вопросы из source в responses.json → quiz.
    Новые вопросы добавляются; пустые ответы заменяются непустыми.
    Результат сортируется: сначала с ответами, потом без, внутри по алфавиту.
    Дубли удаляются. Возвращает количество добавленных/обновлённых записей."""
    try:
        cfg_data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(cfg_data, dict):
        return 0

    quiz = cfg_data.get("quiz")
    if not isinstance(quiz, dict):
        quiz = {}

    changed = 0
    for q, a in source.items():
        q = q.strip()
        a_str = str(a).strip() if a else ""
        if q not in quiz:
            quiz[q] = a_str
            changed += 1
        elif isinstance(quiz[q], str) and not quiz[q] and a_str:
            quiz[q] = a_str
            changed += 1

    # Сортировка: сначала вопросы с ответами, потом без; внутри по алфавиту.
    def _is_empty(v: Any) -> bool:
        if isinstance(v, str):
            return not v.strip()
        if isinstance(v, dict):
            return not str(v.get("answer", "")).strip()
        return True

    cfg_data["quiz"] = dict(sorted(
        quiz.items(),
        key=lambda kv: (_is_empty(kv[1]), kv[0].lower()),
    ))
    config_path.write_text(
        json.dumps(cfg_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return changed


def fetch_remote_quiz(url: str, dest: Path, config_path: Path | None = None) -> bool:
    """Скачивает удалённый файл викторины (JSON вида {"вопрос": "ответ"})
    и, если он валиден и отличается от локального, сохраняет в dest.
    Если указан config_path — мержит вопросы в responses.json → quiz.
    Возвращает True при успешном обновлении."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "minecraftotvet/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read().decode("utf-8")

        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            print(f"[remote] {url}: ожидался JSON-объект, пропускаю.", file=sys.stderr)
            return False

        old = dest.read_text(encoding="utf-8") if dest.exists() else ""
        if old.strip() == data.strip():
            return False

        dest.write_text(data, encoding="utf-8")
        print(f"[remote] Обновлено {len(parsed)} записей викторины: {dest.name}")

        if config_path:
            merged = _merge_quiz_into_config(parsed, config_path)
            if merged:
                print(f"[remote] Добавлено/обновлено в responses.json: {merged}")
        return True
    except Exception as e:
        print(f"[remote] Ошибка обновления с {url}: {e}", file=sys.stderr)
        return False


def sync_remote_quiz(cfg: Config) -> bool:
    """Разово, синхронно, скачивает удалённую викторину. Возвращает True,
    если файл обновлён (и, значит, конфиг стоит перечитать)."""
    url = cfg.settings.remote_quiz_url.strip()
    if not url:
        return False
    dest = cfg.path.with_name("remote_quiz.json")
    return fetch_remote_quiz(url, dest, cfg.path)


def start_remote_updater(cfg: Config) -> None:
    """Запускает фоновый поток обновления удалённой викторины по интервалу
    из настроек. Первое (стартовое) скачивание делается синхронно через
    sync_remote_quiz — этот поток спит сначала interval, потом качает."""
    dest = cfg.path.with_name("remote_quiz.json")

    def loop():
        while True:
            interval_min = max(1, int(cfg.settings.remote_quiz_interval_min))
            time.sleep(interval_min * 60)
            url = cfg.settings.remote_quiz_url.strip()
            if url:
                fetch_remote_quiz(url, dest, cfg.path)

    if cfg.settings.remote_quiz_url.strip():
        t = threading.Thread(target=loop, name="remote-quiz-updater", daemon=True)
        t.start()
        print(f"[remote] Автообновление викторины: {cfg.settings.remote_quiz_url} "
              f"каждые {cfg.settings.remote_quiz_interval_min} мин.")


def maybe_block(settings: Settings) -> bool:
    """Блокирует ввод пользователя, если включено в настройках.
    Возвращает True, если блокировка действительно установлена."""
    global _BLOCK_WARNED
    if not settings.block_user_input:
        return False
    blocked = _block_user_input(True)
    if not blocked and not _BLOCK_WARNED and os.name == "nt":
        print("[bot] Блокировка ввода не сработала — запусти скрипт от "
              "имени администратора, чтобы включить её.", file=sys.stderr)
        _BLOCK_WARNED = True
    return blocked


def play_sound(path: str) -> None:
    """Проигрывает .wav-файл асинхронно (не блокирует поток бота).

    Относительные пути ищутся рядом с responses.json — то есть рядом со
    скриптом в обычном режиме и рядом с .exe в скомпилированной
    (PyInstaller) версии, так же, как уже работает сам responses.json.
    Работает только на Windows (winsound); на других ОС — тихо ничего
    не делает."""
    if not path:
        return
    if winsound is None or os.name != "nt":
        return
    full = path if os.path.isabs(path) else str(CONFIG_PATH.parent / path)
    try:
        winsound.PlaySound(full, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print(f"[sound] Ошибка воспроизведения {full}: {e}", file=sys.stderr)


def main() -> None:
    if not CONFIG_PATH.exists():
        print(f"Не найден файл конфигурации: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    cfg = Config(CONFIG_PATH)

    if not cfg.settings.log_path:
        print("В settings.log_path не указан путь к latest.log", file=sys.stderr)
        sys.exit(1)

    log_path = Path(cfg.settings.log_path)
    print(f"[log] Слежу за файлом: {log_path}")
    print(f"[config] Слежу за конфигом: {CONFIG_PATH}")

    # Синхронно скачиваем свежий remote_quiz.json ДО сбора pending — иначе
    # сборщик считает "неизвестными" те вопросы, ответы на которые уже
    # есть в общей базе, и добавляет их в pending_quiz.json зря.
    try:
        if sync_remote_quiz(cfg):
            cfg.load()
    except Exception as e:
        print(f"[remote] Первичное обновление не удалось: {e}", file=sys.stderr)

    start_remote_updater(cfg)

    # Разово при старте: собрать все вопросы викторины из архивных логов
    # рядом с latest.log и добавить их прямо в responses.json → quiz с
    # пустым ответом. Пустые ответы бот игнорирует, так что просто
    # открой responses.json и допиши правильные — подтянутся на лету.
    try:
        known = {r.triggers[0] for r in cfg.rules if r.triggers}
        added = collect_pending_questions(log_path.parent, known, CONFIG_PATH)
        if added:
            print(f"[collect] Собрано новых вопросов из логов: {added}. "
                  f"Открой {CONFIG_PATH.name} → секция \"quiz\" и впиши ответы.")
            cfg.load()  # подтянуть свежий responses.json
    except Exception as e:
        print(f"[collect] Ошибка сбора вопросов: {e}", file=sys.stderr)

    if cfg.settings.auto_focus:
        print("Готов к работе. Окно Minecraft будет активировано автоматически "
              "перед ответом — можно работать в другом окне.\n")
    else:
        print("Готов к работе. Держите Minecraft в фокусе, чтобы бот мог "
              "печатать в чат.\n")

    typist = Typist(cfg.settings)

    # Последнее сообщение, на которое мы ответили. Нужно, чтобы не отвечать
    # дважды на одну и ту же строку, если она повторится подряд (например,
    # бот-викторина повторил вопрос через таймер). Как только придёт любое
    # другое сообщение, а затем этот же вопрос снова — ответ будет отправлен.
    last_answered_key: tuple[str, str] | None = None

    for line in follow(log_path):
        cfg.maybe_reload()
        typist.settings = cfg.settings  # применяем изменения настроек на лету

        parsed = parse_chat(line)
        if not parsed:
            continue
        nick, text = parsed

        if cfg.settings.ignore_own_username and \
                nick.lower() == cfg.settings.ignore_own_username.lower():
            continue

        key = (nick, text.strip().lower())
      

        # Сначала пробуем встроенный калькулятор ("Сколько будет 2+2?").
        math_reply = try_math_answer(text)
        if math_reply is not None:
            delay = random.uniform(cfg.settings.min_delay, cfg.settings.max_delay)
            print(f"[chat] <{nick}> {text}")
            print(f"[math] -> через {delay:.3f}с: {math_reply}")
            blocked = maybe_block(cfg.settings)
            try:
                time.sleep(delay)
                if cfg.settings.auto_focus:
                    _focus_minecraft()
                typist.send_chat(math_reply)
            except Exception as e:
                print(f"[bot] ошибка отправки: {e}", file=sys.stderr)
            finally:
                if blocked:
                    _block_user_input(False)
            last_answered_key = key
            continue

        now = time.time()
        for rule in cfg.rules:
            if rule.cooldown and (now - rule._last_fired) < rule.cooldown:
                continue
            if rule.matches(text):
    if key == last_answered_key and not rule.ignore_dedup:
        break  # это дубликат, и правило не разрешает игнорировать лок
    reply = rule.pick_reply()
    ...

                # Звук проигрывается сразу, не дожидаясь задержки перед
                # ответом — это сигнал "сработал триггер", а не часть
                # самого ответа в чат.
                if rule.sound:
                    print(f"[sound] -> {rule.sound}")
                    play_sound(rule.sound)

                if reply is not None:
                    # random.uniform даёт float с полноценными миллисекундами,
                    # так что задержка распределена по всему диапазону.
                    delay = random.uniform(cfg.settings.min_delay, cfg.settings.max_delay)
                    print(f"[bot] -> через {delay:.3f}с: {reply}")
                    blocked = maybe_block(cfg.settings)
                    try:
                        time.sleep(delay)
                        if cfg.settings.auto_focus:
                            _focus_minecraft()
                        typist.send_chat(reply)
                    except Exception as e:
                        print(f"[bot] ошибка отправки: {e}", file=sys.stderr)
                    finally:
                        if blocked:
                            _block_user_input(False)
                else:
                    # Правило "тихого" алерта: только звук, без текста в чат.
                    print("[bot] правило без текстового ответа — только звук.")

                rule._last_fired = time.time()
                last_answered_key = key
                break  # одно правило на сообщение


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
