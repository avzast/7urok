"""Telegram-бот вымышленного турагентства «Попутный ветер» (Астана).

Только стандартная библиотека. Секреты берутся из .env:
  BOT_TOKEN           — токен бота от @BotFather
  OPENROUTER_API_KEY  — ключ OpenRouter для ИИ-консультанта
  MANAGER_CHAT_ID     — (необязательно) чат менеджера, куда пересылаются заявки
  AI_MODELS           — (необязательно) свои модели через запятую

Что умеет бот:
  - кнопки «Туры», «Заявка», «Контакты»;
  - ИИ-консультант (system prompt лежит в system_prompt.txt) отвечает на вопросы о турах;
  - заявка на подбор тура через JSON: модель достаёт поля (направление, даты, гости, имя, телефон)
    из сообщения гостя, код проверяет их и переспрашивает недостающее; затем подтверждение.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROMPT_FILE = BASE_DIR / "system_prompt.txt"
EXTRACT_FILE = BASE_DIR / "extract_prompt.txt"  # промпт для разбора заявки в JSON
REQUESTS_FILE = BASE_DIR / "requests.jsonl"  # заявки; в .gitignore, там телефоны клиентов
TZ = timezone(timedelta(hours=5))  # Астана, UTC+5

# ---------------------------------------------------------------- данные агентства (вымышленные)
AGENCY = {
    "name": "Попутный ветер",
    "city": "Астана",
    "address": "ул. Кенесары, 40, офис 12",
    "hours": "пн–сб, 10:00–19:00, воскресенье — выходной",
    "phone": "+7 700 000-00-00",
    "manager": "Айгерим",
}

TOURS = [
    {"emoji": "🇹🇷", "title": "Турция, Анталья", "nights": 7, "hotel": "отель 5★", "meal": "всё включено",
     "season": "май–октябрь", "price": 310_000,
     "highlights": "песчаные пляжи, аквапарк, экскурсии в Каппадокию и Памуккале"},
    {"emoji": "🇦🇪", "title": "ОАЭ, Дубай", "nights": 6, "hotel": "отель 4★", "meal": "завтраки",
     "season": "ноябрь–апрель", "price": 420_000,
     "highlights": "небоскрёбы, шопинг, сафари в пустыне, пляжи Джумейры"},
    {"emoji": "🇪🇬", "title": "Египет, Шарм-эль-Шейх", "nights": 7, "hotel": "отель 4★", "meal": "всё включено",
     "season": "круглый год", "price": 290_000,
     "highlights": "коралловые рифы, дайвинг и снорклинг, прогулки по Красному морю"},
    {"emoji": "🇹🇭", "title": "Таиланд, Пхукет", "nights": 10, "hotel": "отель 4★", "meal": "завтраки",
     "season": "ноябрь–апрель", "price": 560_000,
     "highlights": "острова Пхи-Пхи, тайский массаж, уличная еда, вечерние рынки"},
    {"emoji": "🇬🇪", "title": "Грузия, Тбилиси и Батуми", "nights": 6, "hotel": "отели 3★", "meal": "завтраки",
     "season": "май–октябрь", "price": 240_000,
     "highlights": "старый город, винные дегустации, горы и Чёрное море, трансферы включены"},
    {"emoji": "🌲", "title": "Боровое (Казахстан)", "nights": 5, "hotel": "санаторий", "meal": "питание 3 раза в день",
     "season": "круглый год", "price": 120_000,
     "highlights": "озёра и сосновый бор, оздоровительные процедуры, лыжи зимой"},
]


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₸"


def tour_card(t: dict) -> str:
    return (f"{t['emoji']} {t['title']}\n"
            f"🗓 {t['nights']} ночей · {t['hotel']} · {t['meal']}\n"
            f"✨ {t['highlights']}\n"
            f"💰 от {money(t['price'])} за человека")


def tours_text() -> str:
    cards = "\n\n".join(tour_card(t) for t in TOURS)
    return (f"Наши направления:\n\n{cards}\n\n"
            "Цена «от» — за человека при размещении вдвоём на ближайшие даты. "
            "Точную стоимость рассчитает менеджер.\n"
            "Понравилось что-то? Нажмите «📝 Заявка» — подберём даты и отель.")


def contacts_text() -> str:
    return (f"📞 Турагентство «{AGENCY['name']}»\n"
            f"📍 {AGENCY['city']}, {AGENCY['address']}\n"
            f"🕒 {AGENCY['hours']}\n"
            f"☎️ {AGENCY['phone']} (менеджер {AGENCY['manager']})")


def build_system_prompt(template: str | None = None) -> str:
    """Собирает system prompt: текст из system_prompt.txt + каталог туров и контакты.

    template — необязательный готовый текст шаблона (нужен, чтобы сравнивать версии промпта)."""
    catalog = "\n".join(
        f"- {t['title']}: {t['nights']} ночей, {t['hotel']}, {t['meal']}; лучший сезон: {t['season']}; "
        f"особенности: {t['highlights']}; цена от {money(t['price'])} за человека."
        for t in TOURS)
    contacts = (f"Агентство «{AGENCY['name']}», {AGENCY['city']}, {AGENCY['address']}. "
                f"Часы работы: {AGENCY['hours']}. Телефон: {AGENCY['phone']}, менеджер {AGENCY['manager']}.")
    text = template if template is not None else PROMPT_FILE.read_text(encoding="utf-8")
    return (text.replace("{{КАТАЛОГ}}", catalog).replace("{{КОНТАКТЫ}}", contacts)
            .replace("{{МЕНЕДЖЕР}}", AGENCY["manager"]).replace("{{ТЕЛЕФОН}}", AGENCY["phone"]))


# ---------------------------------------------------------------- вспомогательное
def load_env(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def http_json(url: str, data: dict | None = None, timeout: int = 40, headers: dict | None = None) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# ---------------------------------------------------------------- ИИ (OpenRouter)
AI_URL = "https://openrouter.ai/api/v1/chat/completions"
# Цепочка бесплатных моделей: если первая перегружена (429) или вернула пустой ответ — берём следующую.
AI_MODELS_DEFAULT = ["google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free",
                     "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "openrouter/free"]
AI_MAX_TOKENS = 2500     # запас: часть моделей тратит токены на «рассуждения» до ответа
AI_TIMEOUT = 30          # секунд на одну модель
HISTORY_LIMIT = 10       # сколько последних сообщений диалога помнить
USER_MESSAGE_MAX = 1000  # длинные сообщения обрезаем
USER_LIMIT = (6, 60)     # не больше 6 сообщений в 60 секунд от одного чата


def ai_models() -> list[str]:
    custom = os.environ.get("AI_MODELS", "")
    return [m.strip() for m in custom.split(",") if m.strip()] or AI_MODELS_DEFAULT


def ask_ai(system_prompt: str, history: list[dict]) -> str:
    """Спрашивает ИИ через OpenRouter, перебирая модели по цепочке. RuntimeError, если все отказали."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан в .env")
    messages = [{"role": "system", "content": system_prompt}] + history
    last_error = "нет моделей"
    partial = None  # обрезанный по длине ответ: берём, только если ни одна модель не дала полный
    for model in ai_models():
        try:
            resp = http_json(AI_URL, {"model": model, "messages": messages, "max_tokens": AI_MAX_TOKENS},
                             timeout=AI_TIMEOUT, headers={"Authorization": f"Bearer {key}"})
            choice = resp["choices"][0]
            answer = (choice["message"].get("content") or "").strip()
            if answer and choice.get("finish_reason") == "length":
                partial = partial or answer
                last_error = f"{model}: ответ обрезан по длине"
            elif answer:
                print(f"ИИ ответил: {resp.get('model', model)}")
                return answer
            else:
                last_error = f"{model}: пустой ответ"
        except urllib.error.HTTPError as exc:
            last_error = f"{model}: HTTP {exc.code}"
        except Exception as exc:
            last_error = f"{model}: {exc}"
        print("ИИ пропуск:", last_error)
    if partial:  # лучше обрезать до последнего законченного предложения, чем оборвать на полуслове
        cut = max(partial.rfind(c) for c in ".!?")
        return partial[:cut + 1] if cut > 40 else partial
    raise RuntimeError(last_error)


# ---------------------------------------------------------------- клавиатуры
def keyboard(rows: list[list[str]], placeholder: str = "") -> dict:
    kb = {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True, "is_persistent": True}
    if placeholder:
        kb["input_field_placeholder"] = placeholder
    return kb


BTN_TOURS, BTN_BOOK, BTN_CONTACTS = "🌍 Туры", "📝 Заявка", "📞 Контакты"
BTN_CANCEL, BTN_SEND = "❌ Отмена", "✅ Отправить"
MAIN_KB = keyboard([[BTN_TOURS, BTN_BOOK], [BTN_CONTACTS]], "Выберите раздел или задайте вопрос")
CANCEL_KB = keyboard([[BTN_CANCEL]])
CONFIRM_KB = keyboard([[BTN_SEND, BTN_CANCEL]])
DEST_KB = keyboard([[t["title"].split(",")[0] for t in TOURS[i:i + 3]] for i in range(0, len(TOURS), 3)]
                   + [[BTN_CANCEL]])

# ---------------------------------------------------------------- заявка: поля, проверка, JSON
FIELDS = ["dest", "dates", "people", "name", "phone"]
FIELD_TITLES = {"dest": "🌍 Направление", "dates": "🗓 Даты", "people": "👥 Гости", "name": "🙂 Имя",
                "phone": "☎️ Телефон"}
FIELD_ASK = {"dest": "направление", "dates": "даты поездки", "people": "сколько человек поедет",
             "name": "ваше имя", "phone": "номер телефона"}
# запуск заявки из обычной переписки, если гость сам просит оформить
BOOKING_WORDS = re.compile(r"(?i)заявк|забронировать|хочу поехать|хочу в тур")
QUESTIONS = {
    "dest": ("Куда хотите поехать? Выберите направление кнопкой или напишите своё.", DEST_KB),
    "dates": ("Когда планируете поездку? Например: «10–17 января» или «в мае».", CANCEL_KB),
    "people": ("Сколько человек поедет? Напишите, например: «2 взрослых и ребёнок 5 лет».", CANCEL_KB),
    "name": ("Как вас зовут?", CANCEL_KB),
    "phone": ("Номер телефона для связи, например: +7 700 123-45-67", CANCEL_KB),
}


def validate(step: str, text: str) -> tuple[str | None, str]:
    """Возвращает (значение, текст ошибки). Если значение None — ответ не принят."""
    text = " ".join(text.split())
    if step == "phone":
        digits = re.sub(r"\D", "", text)
        if not (10 <= len(digits) <= 15) or not re.fullmatch(r"[\d\s()+\-]+", text):
            return None, "Не похоже на номер телефона. Напишите его цифрами, например: +7 700 123-45-67"
        return text, ""
    if step == "people" and not re.search(r"\d", text) and not re.search(r"(?i)один|одн|двое|двух|трое|трёх|четвер", text):
        return None, "Напишите, сколько человек поедет, например: «2 взрослых и ребёнок»."
    if not (2 <= len(text) <= 80):
        return None, "Ответ должен быть от 2 до 80 символов, попробуйте ещё раз."
    return text, ""


def parse_json(raw: str) -> dict | None:
    """Достаёт JSON-объект из ответа модели (модель может добавить текст или ```-обёртку)."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def check_field(field: str, raw, source_text: str) -> tuple[str | None, str]:
    """Проверяет значение, которое вернула модель. (значение, ошибка): оба пустые — поля в сообщении нет."""
    if raw is None:
        return None, ""
    value = " ".join(str(raw).split())
    if not value or value.lower() in ("null", "none", "нет", "-"):
        return None, ""
    value, error = validate(field, value)
    if value is None:
        return None, error
    # защита от выдумок модели: телефон и имя обязаны буквально присутствовать в сообщении гостя
    if field == "phone" and re.sub(r"\D", "", value) not in re.sub(r"\D", "", source_text):
        return None, ""
    if field == "name" and value.split()[0].lower() not in source_text.lower():
        return None, ""
    return value, ""


def extract_fields(extract_prompt: str, text: str, known: dict, asked: str) -> dict | None:
    """Просит модель вернуть JSON с полями заявки. None — если ИИ недоступен или вернул не JSON."""
    user = (f"Уже известно: {json.dumps(known, ensure_ascii=False)}\n"
            f"Бот спросил у гостя: {asked}\n"
            f"Сообщение гостя: {text[:USER_MESSAGE_MAX]}")
    try:
        raw = ask_ai(extract_prompt, [{"role": "user", "content": user}])
    except Exception as exc:
        print("разбор заявки: ИИ недоступен:", exc)
        return None
    obj = parse_json(raw)
    if obj is None:
        print("разбор заявки: ИИ вернул не JSON:", raw[:120])
    return obj


def summary(data: dict) -> str:
    return (f"Проверьте заявку:\n\n"
            f"🌍 Направление: {data['dest']}\n"
            f"🗓 Даты: {data['dates']}\n"
            f"👥 Гости: {data['people']}\n"
            f"🙂 Имя: {data['name']}\n"
            f"☎️ Телефон: {data['phone']}\n\n"
            "Отправляем менеджеру?")


# ---------------------------------------------------------------- бот
class Bot:
    def __init__(self, token: str, system_prompt: str, extract_prompt: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.system_prompt = system_prompt
        self.extract_prompt = extract_prompt
        self.history: dict[int, list[dict]] = {}   # память диалога с ИИ (только в оперативной памяти)
        self.booking: dict[int, dict] = {}         # заявка: {"data": {поля}, "confirm": bool}
        self.recent: dict[int, list[float]] = {}   # для ограничения частоты сообщений

    def call(self, method: str, **params) -> dict:
        resp = http_json(self.base + method, params, timeout=params.get("timeout", 0) + 15)
        if not resp.get("ok"):
            raise RuntimeError(f"{method}: {resp.get('description')}")
        return resp["result"]

    def send(self, chat_id: int, text: str, kb: dict = MAIN_KB) -> None:
        self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=kb,
                  link_preview_options={"is_disabled": True})

    def too_fast(self, chat_id: int) -> bool:
        limit, window = USER_LIMIT
        now = time.time()
        times = [t for t in self.recent.get(chat_id, []) if now - t < window]
        times.append(now)
        self.recent[chat_id] = times
        return len(times) > limit

    # ----- заявка
    def reask(self, chat_id: int, prefix: str = "") -> None:
        """Показывает, что уже записано, и просит недостающее (или предлагает подтвердить заявку)."""
        state = self.booking[chat_id]
        data = state["data"]
        missing = [f for f in FIELDS if f not in data]
        if not missing:
            state["confirm"] = True
            self.send(chat_id, (prefix + "\n\n" if prefix else "") + summary(data), CONFIRM_KB)
            return
        parts = [prefix] if prefix else []
        if data:
            parts.append("Записала: " + "; ".join(f"{FIELD_TITLES[f]}: {data[f]}" for f in FIELDS if f in data))
            parts.append("Осталось уточнить: " + ", ".join(FIELD_ASK[f] for f in missing) + ".")
        else:
            parts.append("Опишите поездку своими словами: куда, когда, сколько человек, ваше имя и телефон. "
                         "Можно по частям, я всё запомню.")
        self.send(chat_id, "\n".join(parts), DEST_KB if missing[0] == "dest" else CANCEL_KB)

    def start_booking(self, chat_id: int) -> None:
        self.booking[chat_id] = {"data": {}, "confirm": False}
        self.reask(chat_id)

    def cancel_booking(self, chat_id: int) -> None:
        self.booking.pop(chat_id, None)
        self.send(chat_id, "Хорошо, заявку отменили. Если передумаете — нажмите «📝 Заявка».")

    def booking_answer(self, chat_id: int, text: str, user: dict) -> None:
        """Сообщение гостя во время заявки: модель достаёт поля в JSON, код проверяет, бот переспрашивает."""
        state = self.booking[chat_id]
        data = state["data"]
        if state.get("confirm"):
            if text == BTN_SEND:
                self.finish_booking(chat_id, data, user)
            else:
                self.send(chat_id, "Нажмите «✅ Отправить» или «❌ Отмена».", CONFIRM_KB)
            return
        self.call("sendChatAction", chat_id=chat_id, action="typing")
        first_missing = next(f for f in FIELDS if f not in data)
        found = extract_fields(self.extract_prompt, text, data, QUESTIONS[first_missing][0])
        errors, updated = [], False
        if found is None:
            # запасной путь: ИИ недоступен или ответил не JSON — весь текст относим к первому недостающему полю
            found = {first_missing: text}
        for field in FIELDS:
            value, error = check_field(field, found.get(field), text)
            if value is not None:
                if data.get(field) != value:  # новое или исправленное значение
                    updated = True
                data[field] = value
            elif error:
                errors.append(error)
        if errors:
            self.send(chat_id, errors[0], DEST_KB if first_missing == "dest" else CANCEL_KB)
            if updated:
                self.reask(chat_id)
            return
        self.reask(chat_id, "" if updated else "Не нашла в сообщении данных для заявки.")

    def finish_booking(self, chat_id: int, data: dict, user: dict) -> None:
        record = {"time": datetime.now(TZ).isoformat(timespec="seconds"), "chat_id": chat_id,
                  "username": user.get("username"), **data}
        with REQUESTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        manager_chat = os.environ.get("MANAGER_CHAT_ID")
        if manager_chat:
            try:
                self.call("sendMessage", chat_id=manager_chat,
                          text="🆕 Новая заявка\n\n" + summary(data).split("\n\nОтправляем")[0])
            except Exception as exc:
                print("не удалось уведомить менеджера:", exc)
        self.booking.pop(chat_id, None)
        print(f"заявка сохранена: {data['dest']}, чат {chat_id}")
        self.send(chat_id, f"Спасибо, {data['name']}! Заявка принята ✅\n"
                           f"Менеджер {AGENCY['manager']} свяжется с вами по номеру {data['phone']} "
                           f"в рабочее время ({AGENCY['hours']}).")

    # ----- обработка сообщений
    def handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not text:
            self.send(chat_id, "Я понимаю только текст. Напишите вопрос или выберите раздел кнопками 🙂")
            return
        if command == "/start":
            self.booking.pop(chat_id, None)
            self.send(chat_id, f"Здравствуйте! Я Лина, консультант турагентства «{AGENCY['name']}» ✈️\n"
                               "Расскажу о турах, помогу выбрать отдых и оформить заявку. "
                               "Выберите раздел кнопками внизу или задайте вопрос своими словами.")
            return
        if self.too_fast(chat_id):
            self.send(chat_id, "Слишком много сообщений подряд, подождите минуту 🙂")
            return
        print(f"сообщение из чата {chat_id}, отвечаю")

        if text == BTN_CANCEL or command == "/cancel":
            self.cancel_booking(chat_id)
            return
        if text == BTN_BOOK or command == "/book":
            self.start_booking(chat_id)
            return
        in_booking = chat_id in self.booking
        if text == BTN_TOURS or command == "/tours":
            self.send(chat_id, tours_text(), CANCEL_KB if in_booking else MAIN_KB)
            if in_booking:
                self.reask(chat_id)
            return
        if text == BTN_CONTACTS or command == "/contacts":
            self.send(chat_id, contacts_text(), CANCEL_KB if in_booking else MAIN_KB)
            if in_booking:
                self.reask(chat_id)
            return
        if in_booking:
            self.booking_answer(chat_id, text, message.get("from") or {})
            return
        if BOOKING_WORDS.search(text):  # гость сам просит оформить заявку: сразу разбираем его сообщение
            self.booking[chat_id] = {"data": {}, "confirm": False}
            self.booking_answer(chat_id, text, message.get("from") or {})
            return
        self.answer_with_ai(chat_id, text)

    def answer_with_ai(self, chat_id: int, text: str) -> None:
        self.call("sendChatAction", chat_id=chat_id, action="typing")
        history = self.history.setdefault(chat_id, [])
        history.append({"role": "user", "content": text[:USER_MESSAGE_MAX]})
        try:
            answer = ask_ai(self.system_prompt, history)
        except Exception as exc:  # все модели недоступны — бот не молчит
            print("ИИ недоступен:", exc)
            history.pop()
            self.send(chat_id, "Консультант сейчас занят 🙈 Посмотрите «🌍 Туры», оставьте заявку "
                               f"кнопкой «📝 Заявка» или позвоните: {AGENCY['phone']}.")
            return
        history.append({"role": "assistant", "content": answer})
        del history[:-HISTORY_LIMIT]
        while history and history[0]["role"] != "user":  # диалог всегда начинается с реплики клиента
            history.pop(0)
        self.send(chat_id, answer)

    def run(self) -> None:
        while True:  # при сбое сети на старте не падаем, а пробуем снова
            try:
                me = self.call("getMe")
                self.call("deleteWebhook")  # getUpdates не работает при установленном webhook
                self.call("setMyCommands", commands=[
                    {"command": "start", "description": "Начать"},
                    {"command": "tours", "description": "Каталог туров"},
                    {"command": "book", "description": "Оставить заявку"},
                    {"command": "contacts", "description": "Контакты агентства"},
                    {"command": "cancel", "description": "Отменить заявку"},
                ])
                break
            except Exception as exc:
                print("старт не удался, повтор через 5 секунд:", exc)
                time.sleep(5)
        print(f"Бот @{me['username']} запущен. Ctrl+C — остановить.")
        offset = None
        while True:
            try:
                updates = self.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            except Exception as exc:
                print("polling error:", exc)
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        self.handle(upd["message"])
                    except Exception as exc:
                        print("handler error:", exc)


if __name__ == "__main__":
    load_env()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не найден в .env")
    Bot(token, build_system_prompt(), EXTRACT_FILE.read_text(encoding="utf-8")).run()
