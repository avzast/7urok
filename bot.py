"""Telegram-бот: погода в Астане.

Только стандартная библиотека. Токен берётся из .env (BOT_TOKEN).
Погода: Open-Meteo (без ключа). Получение апдейтов: long polling.
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

CITY = "Астана"
LAT, LON = 51.1801, 71.4460
TIMEZONE = "Asia/Almaty"

WMO = {
    0: "Ясно ☀️", 1: "Преимущественно ясно 🌤", 2: "Переменная облачность ⛅", 3: "Пасмурно ☁️",
    45: "Туман 🌫", 48: "Изморозь 🌫",
    51: "Слабая морось 🌦", 53: "Морось 🌦", 55: "Сильная морось 🌧",
    56: "Ледяная морось 🌧", 57: "Сильная ледяная морось 🌧",
    61: "Слабый дождь 🌦", 63: "Дождь 🌧", 65: "Сильный дождь 🌧",
    66: "Ледяной дождь 🌧", 67: "Сильный ледяной дождь 🌧",
    71: "Слабый снег 🌨", 73: "Снег 🌨", 75: "Сильный снег ❄️", 77: "Снежные зёрна ❄️",
    80: "Слабый ливень 🌦", 81: "Ливень 🌧", 82: "Сильный ливень ⛈",
    85: "Снегопад 🌨", 86: "Сильный снегопад ❄️",
    95: "Гроза ⛈", 96: "Гроза с градом ⛈", 99: "Сильная гроза с градом ⛈",
}


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def http_json(url: str, data: dict | None = None, timeout: int = 40) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def wind_direction(deg: float) -> str:
    names = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]
    return names[round(deg / 45) % 8]


try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TIMEZONE)
except Exception:  # на Windows без пакета tzdata базы нет; Астана сейчас UTC+5
    TZ = timezone(timedelta(hours=5))

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def get_time() -> str:
    now = datetime.now(TZ)
    return (f"🕐 {now:%H:%M:%S}, {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}\n"
            f"Время в г. {CITY} (UTC+5)")


def get_weather() -> str:
    params = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": TIMEZONE, "wind_speed_unit": "ms",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                   "surface_pressure,wind_speed_10m,wind_direction_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1,
    })
    data = http_json(f"https://api.open-meteo.com/v1/forecast?{params}")
    cur, day = data["current"], data["daily"]
    pressure_mm = cur["surface_pressure"] * 0.750062  # гПа -> мм рт. ст.
    return (
        f"Погода в г. {CITY}: {WMO.get(cur['weather_code'], 'нет данных')}\n"
        f"🌡 Сейчас: {cur['temperature_2m']:+.0f}°C (ощущается как {cur['apparent_temperature']:+.0f}°C)\n"
        f"📈 Сегодня: {day['temperature_2m_min'][0]:+.0f}…{day['temperature_2m_max'][0]:+.0f}°C, "
        f"осадки {day['precipitation_probability_max'][0]}%\n"
        f"💨 Ветер: {cur['wind_speed_10m']:.1f} м/с, {wind_direction(cur['wind_direction_10m'])}\n"
        f"💧 Влажность: {cur['relative_humidity_2m']}%\n"
        f"🔽 Давление: {pressure_mm:.0f} мм рт. ст."
    )


def get_bitcoin() -> str:
    params = urllib.parse.urlencode({
        "ids": "bitcoin", "vs_currencies": "usd,kzt,rub", "include_24hr_change": "true",
    })
    btc = http_json(f"https://api.coingecko.com/api/v3/simple/price?{params}")["bitcoin"]
    change = btc["usd_24h_change"]
    arrow = "📈" if change >= 0 else "📉"
    return (
        "Курс биткойна (BTC)\n\n"
        f"💵 ${btc['usd']:,.0f}\n"
        f"🇰🇿 {btc['kzt']:,.0f} ₸\n"
        f"🇷🇺 {btc['rub']:,.0f} ₽\n\n"
        f"{arrow} За 24 часа: {change:+.2f}%"
    ).replace(",", " ")


NEWS_CHANNEL = "tengrinews"
NEWS_URL = f"https://t.me/s/{NEWS_CHANNEL}"  # публичная веб-версия канала, вход и ключ не нужны
NEWS_REFRESH_SECONDS = 60
NEWS_COUNT = 5
NEWS_TITLE_MAX = 160

_news = {"items": [], "updated": None}
_news_lock = threading.Lock()


def fetch_news() -> list[dict]:
    """Скачивает последние посты канала с публичной страницы t.me/s/<канал>."""
    req = urllib.request.Request(NEWS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8")
    items = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        post = re.search(r'data-post="([^"]+)"', block)
        text = re.search(r'<div class="tgme_widget_message_text[^"]*js-message_text[^"]*"[^>]*>(.*?)</div>',
                         block, re.S)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not (post and text and when):
            continue  # пост без текста (только медиа) пропускаем
        raw = re.sub(r"<br\s*/?>", "\n", text.group(1))
        clean = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        title = next((line.strip() for line in clean.splitlines() if line.strip()), "")
        if not title:
            continue
        if len(title) > NEWS_TITLE_MAX:
            title = title[:NEWS_TITLE_MAX].rstrip() + "…"
        items.append({
            "title": title,
            "link": f"https://t.me/{post.group(1)}",
            "time": datetime.fromisoformat(when.group(1)).astimezone(TZ),
        })
    return items[-NEWS_COUNT:][::-1]  # самые свежие сверху


def news_updater() -> None:
    """Фоновый поток: обновляет кэш новостей раз в минуту."""
    while True:
        try:
            items = fetch_news()
            with _news_lock:
                _news["items"], _news["updated"] = items, datetime.now(TZ)
        except Exception as exc:
            print("news error:", exc)
        time.sleep(NEWS_REFRESH_SECONDS)


def get_news() -> str:
    with _news_lock:
        items, updated = list(_news["items"]), _news["updated"]
    if not items:
        return "📰 Новости Tengrinews: пока недоступны, попробуй чуть позже."
    lines = [f"📰 Свежие новости Tengrinews (обновлено {updated:%H:%M:%S})"]
    for item in items:
        lines.append(f"\n• {item['time']:%H:%M} — {item['title']}\n{item['link']}")
    return "\n".join(lines)


def get_report() -> str:
    """Время + погода + курс биткойна + новости одним сообщением (погода и BTC — параллельно)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [("Погода", pool.submit(get_weather)), ("Курс биткойна", pool.submit(get_bitcoin))]
    parts = [get_time()]
    for name, future in futures:
        try:
            parts.append(future.result())
        except Exception as exc:  # один недоступный сервис не ломает весь ответ
            print(f"{name} error:", exc)
            parts.append(f"{name}: не удалось получить данные, попробуй чуть позже.")
    parts.append(get_news())
    return "\n\n".join(parts)


class Bot:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params) -> dict:
        resp = http_json(self.base + method, params, timeout=params.get("timeout", 0) + 15)
        if not resp.get("ok"):
            raise RuntimeError(f"{method}: {resp.get('description')}")
        return resp["result"]

    def send(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", chat_id=chat_id, text=text,
                  link_preview_options={"is_disabled": True})  # без больших превью у ссылок на новости

    def handle(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/start":
            self.send(chat_id, f"Привет! Отправь мне любое сообщение — в ответ пришлю актуальное "
                               f"время в г. {CITY}, погоду и курс биткойна.")
            return
        print(f"сообщение из чата {chat_id}, отвечаю")
        self.call("sendChatAction", chat_id=chat_id, action="typing")
        self.send(chat_id, get_report())

    def run(self) -> None:
        me = self.call("getMe")
        print(f"Бот @{me['username']} запущен. Ctrl+C — остановить.")
        self.call("deleteWebhook")  # getUpdates не работает при установленном webhook
        self.call("setMyCommands", commands=[
            {"command": "start", "description": "О боте"},
        ])
        offset = None
        while True:
            try:
                updates = self.call("getUpdates", offset=offset, timeout=30,
                                    allowed_updates=["message"])
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
    threading.Thread(target=news_updater, daemon=True).start()
    Bot(token).run()
