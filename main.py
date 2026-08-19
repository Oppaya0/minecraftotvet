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
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from pynput.keyboard import Controller, Key, KeyCode
except ImportError:
    print("Требуется библиотека pynput. Установите: pip install pynput", file=sys.stderr)
    raise


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
            return KeyCode.from_vk(ord(ch))          # VK_0..VK_9
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
    ("х", "*"),   # русская «х» между числами часто = умножение
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
    replies: list[str]
    match: str = "contains"           # contains | exact | regex
    case_sensitive: bool = False
    cooldown: float = 0.0             # секунд между срабатываниями этого правила
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

    def pick_reply(self) -> str:
        return random.choice(self.replies)


@dataclass
class Settings:
    min_delay: float = 2.0
    max_delay: float = 4.0
    log_path: str = ""
    chat_key: str = "t"                # клавиша открытия чата
    ignore_own_username: str = ""      # ваш ник, чтобы не отвечать себе
    type_interval: float = 0.02        # пауза между нажатиями клавиш (реалистичный ввод)
    block_user_input: bool = True      # блокировать клавиатуру/мышь пользователя во время печати бота
    remote_quiz_url: str = ""          # URL общей викторины (JSON вида {"вопрос": "ответ"})
    remote_quiz_interval_min: int = 60 # как часто скачивать обновления, в минутах


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.mtime: float = 0.0
        self.settings = Settings()
        self.rules: list[Rule] = []
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
            remote_quiz_url=str(s.get("remote_quiz_url", "")),
            remote_quiz_interval_min=int(s.get("remote_quiz_interval_min", 60)),
        )
        rules: list[Rule] = []
        for entry in raw.get("responses", []):
            rules.append(Rule(
                triggers=list(entry["triggers"]),
                replies=list(entry["replies"]),
                match=str(entry.get("match", "contains")),
                case_sensitive=bool(entry.get("case_sensitive", False)),
                cooldown=float(entry.get("cooldown", 0.0)),
            ))

        # Упрощённая секция "quiz": словарь "вопрос-подстрока" -> "ответ".
        # Каждая пара превращается в отдельное правило contains/без регистра.
        # Ответ может быть строкой или списком строк (тогда выбирается случайный).
        def _add_quiz(mapping: dict) -> int:
            n = 0
            for question, answer in mapping.items():
                replies = answer if isinstance(answer, list) else [str(answer)]
                rules.append(Rule(
                    triggers=[str(question)],
                    replies=[str(r) for r in replies],
                    match="contains",
                    case_sensitive=False,
                    cooldown=0.0,
                ))
                n += 1
            return n

        local_quiz = raw.get("quiz", {})
        n_local = _add_quiz(local_quiz) if isinstance(local_quiz, dict) else 0

        # Общая викторина, автоматически скачиваемая с URL.
        # Файл remote_quiz.json пишется фоновым потоком в папке рядом с
        # main.py/exe. Локальные вопросы (сверху) имеют приоритет —
        # если один и тот же ключ есть и там, и там, обе записи попадут
        # в правила, но локальная стоит раньше и, при равной длине
        # триггера, обычно сработает первой.
        remote_path = self.path.with_name("remote_quiz.json")
        n_remote = 0
        if remote_path.exists():
            try:
                remote = json.loads(remote_path.read_text(encoding="utf-8"))
                if isinstance(remote, dict):
                    n_remote = _add_quiz(remote)
            except Exception as e:
                print(f"[config] Не удалось прочитать remote_quiz.json: {e}",
                      file=sys.stderr)

        # Более специфичные (длинные) триггеры проверяются раньше — иначе
        # общий триггер вроде "Викторина" мог бы перебить конкретный вопрос.
        rules.sort(key=lambda r: -max((len(t) for t in r.triggers), default=0))

        self.rules = rules
        self.mtime = self.path.stat().st_mtime
        print(f"[config] Загружено правил: {len(self.rules)} "
              f"(локальный quiz: {n_local}, общий quiz: {n_remote})")

    def maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        remote_path = self.path.with_name("remote_quiz.json")
        remote_m = remote_path.stat().st_mtime if remote_path.exists() else 0.0
        # mtime сохраняется только для основного файла — для remote_quiz
        # используем "прошлое значение" через атрибут.
        remote_prev = getattr(self, "_remote_mtime", 0.0)
        if m != self.mtime or remote_m != remote_prev:
            try:
                self.load()
                self._remote_mtime = remote_m
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


def parse_chat(line: str) -> tuple[str, str] | None:
    for pat in CHAT_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group("nick"), m.group("text").strip()
    m = SYSTEM_CHAT_PATTERN.search(line)
    if m:
        text = m.group("text").strip()
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


class Typist:
    def __init__(self, settings: Settings):
        self.kb = Controller()
        self.settings = settings

    def send_chat(self, message: str) -> None:
        s = self.settings
        # Открыть чат. Клавишу передаём как VK-код — иначе на русской
        # раскладке `t` уходит как «е» и чат не открывается.
        key = _chat_key(s.chat_key)
        self.kb.press(key)
        self.kb.release(key)
        time.sleep(0.25)
        # Ввести текст посимвольно с небольшой паузой
        for ch in message:
            self.kb.type(ch)
            if s.type_interval > 0:
                time.sleep(s.type_interval)
        time.sleep(0.05)
        # Отправить
        self.kb.press(Key.enter)
        self.kb.release(Key.enter)


def fetch_remote_quiz(url: str, dest: Path) -> bool:
    """Скачивает удалённый файл викторины (JSON вида {"вопрос": "ответ"})
    и, если он валиден и отличается от локального, сохраняет в dest.
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
        return True
    except Exception as e:
        print(f"[remote] Ошибка обновления с {url}: {e}", file=sys.stderr)
        return False


def start_remote_updater(cfg: Config) -> None:
    """Запускает фоновый поток: сразу скачивает remote_quiz.json и
    затем повторяет по интервалу из настроек."""
    dest = cfg.path.with_name("remote_quiz.json")

    def loop():
        while True:
            url = cfg.settings.remote_quiz_url.strip()
            interval_min = max(1, int(cfg.settings.remote_quiz_interval_min))
            if url:
                fetch_remote_quiz(url, dest)
            time.sleep(interval_min * 60)

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
        print("[bot]  Блокировка ввода не сработала — запусти скрипт от "
              "имени администратора, чтобы включить её.", file=sys.stderr)
        _BLOCK_WARNED = True
    return blocked


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
    start_remote_updater(cfg)
    print("Готов к работе. Не закрывайте окно — держите Minecraft в фокусе, "
          "чтобы бот мог печатать в чат.\n")

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
        if key == last_answered_key:
            # Дубликат последней обработанной строки — пропускаем.
            continue

        # Сначала пробуем встроенный калькулятор ("Сколько будет 2+2?").
        math_reply = try_math_answer(text)
        if math_reply is not None:
            delay = random.uniform(cfg.settings.min_delay, cfg.settings.max_delay)
            print(f"[chat] <{nick}> {text}")
            print(f"[math] -> через {delay:.3f}с: {math_reply}")
            blocked = maybe_block(cfg.settings)
            try:
                time.sleep(delay)
                typist.send_chat(math_reply)
            except Exception as e:
                print(f"[bot]  ошибка отправки: {e}", file=sys.stderr)
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
                reply = rule.pick_reply()
                # random.uniform даёт float с полноценными миллисекундами,
                # так что задержка распределена по всему диапазону.
                delay = random.uniform(cfg.settings.min_delay, cfg.settings.max_delay)
                print(f"[chat] <{nick}> {text}")
                print(f"[bot]  -> через {delay:.3f}с: {reply}")
                blocked = maybe_block(cfg.settings)
                try:
                    time.sleep(delay)
                    typist.send_chat(reply)
                except Exception as e:
                    print(f"[bot]  ошибка отправки: {e}", file=sys.stderr)
                finally:
                    if blocked:
                        _block_user_input(False)
                rule._last_fired = time.time()
                last_answered_key = key
                break  # одно правило на сообщение


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
