"""Сравнение ответов одной модели OpenRouter с выключенным и включённым рассуждением.

Берёт system prompt бота и три вопроса из раздела «Трудные вопросы» в test_cases.md,
отправляет каждый вопрос дважды (reasoning effort «none» и «high») и дописывает
результаты в конец comparison.md. Всего 6 запросов, без повторов и запасных моделей.

  python -X utf8 compare.py --check   — проверить вопросы и ключ, без запросов
  python -X utf8 compare.py           — запустить сравнение

Ключ OPENROUTER_API_KEY берётся из .env.
"""
import json
import os
import re
import sys
import time
import urllib.error
from datetime import datetime

import bot  # из бота берём только чтение .env, сборку промпта и разбор ответа

MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"  # бесплатная, рассуждение можно выключить
EFFORTS = ["none", "high"]
MAX_TOKENS = 8000   # с запасом: на «high» рассуждение расходует тот же лимит, что и ответ
TIMEOUT = 180       # секунд на один запрос
CASES_FILE = bot.BASE_DIR / "test_cases.md"
REPORT_FILE = bot.BASE_DIR / "comparison.md"
SECTION = "## Трудные вопросы: reasoning none и high"


def load_questions(text: str) -> list[str]:
    """Три реплики гостя из раздела «Трудные вопросы»."""
    section = re.search(r"^## Трудные вопросы[ \t]*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section:
        raise SystemExit("В test_cases.md нет раздела «Трудные вопросы»")
    questions = re.findall(r"^\*\*Гость:\*\*[ \t]*(.+?)[ \t]*$", section.group(1), re.M)
    if len(questions) != 3:
        raise SystemExit(f"Ожидалось 3 вопроса, найдено: {len(questions)}")
    return questions


def error_text(body) -> str:
    """Только сообщение об ошибке: в остальных полях OpenRouter бывают данные аккаунта."""
    try:
        data = json.loads(body) if isinstance(body, (str, bytes)) else body
        return str(data["error"]["message"])
    except Exception:
        return "ответ без описания ошибки"


def ask(key: str, system_prompt: str, question: str, effort: str) -> dict:
    """Один запрос. Всегда возвращает результат: при неудаче в нём заполнено поле error."""
    payload = {"model": MODEL, "max_tokens": MAX_TOKENS, "reasoning": {"effort": effort},
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": question}]}
    result = {"effort": effort, "content": "", "reasoning": "", "usage": {}, "finish": "", "served": "", "error": ""}
    started = time.perf_counter()
    try:
        resp = bot.http_json(bot.AI_URL, payload, timeout=TIMEOUT, headers={"Authorization": f"Bearer {key}"})
        if not resp.get("choices"):
            result["error"] = error_text(resp)
        else:
            choice = resp["choices"][0]
            message = choice.get("message") or {}
            result["content"] = (message.get("content") or "").strip()
            result["reasoning"] = (message.get("reasoning") or "").strip()
            result["usage"] = resp.get("usage") or {}
            result["finish"] = choice.get("finish_reason") or ""
            result["served"] = " / ".join(str(resp[k]) for k in ("model", "provider") if resp.get(k))
            if choice.get("error"):
                result["error"] = error_text(choice)
    except urllib.error.HTTPError as exc:
        result["error"] = f"HTTP {exc.code}: {error_text(exc.read())}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["seconds"] = time.perf_counter() - started
    return result


def tokens(result: dict, *path: str) -> str:
    value = result["usage"]
    for key in path:
        value = (value or {}).get(key)
    return "—" if value is None else str(value)


def cell(text: str) -> str:
    """Текст для ячейки таблицы: без переносов строк и без символов, ломающих разметку."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")
    return "<br>".join(line.strip() for line in text.splitlines() if line.strip())


def block(text: str) -> str:
    """Дословный текст в блоке кода; ограда длиннее любой цепочки ` внутри текста."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def render(questions: list[str], results: list[dict], started: str) -> str:
    """Раздел отчёта. results идут парами по вопросам: (вопрос 1, none), (вопрос 1, high), ..."""
    lines = [
        SECTION, "",
        f"Запуск `compare.py`: {started}. Модель `{MODEL}`, `max_tokens` = {MAX_TOKENS}. "
        "Каждый запрос состоит из system prompt бота и одного вопроса: без истории и без памятки "
        "`RULES_REMINDER`. По одной попытке, без повторов.", "",
        "Вопросы (из [test_cases.md](test_cases.md)):", "",
    ]
    lines += [f"{n}. {q}" for n, q in enumerate(questions, 1)]
    lines += [
        "",
        "| Вопрос | reasoning.effort | Время, с | Токены: запрос | Токены: ответ | из них рассуждение "
        "| Токены: всего | Ответ гостю | message.reasoning |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results):
        answer = bot.parse_reply(r["content"])[1]
        if r["error"]:
            answer = f"Ошибка: {r['error']}" + (f"\n{answer}" if answer else "")
        elif r["finish"] != "stop":
            answer = f"[finish_reason: {r['finish']}]\n{answer}"
        thought = f"{len(r['reasoning'])} символов, текст ниже" if r["reasoning"] else "пусто"
        lines.append("| " + " | ".join([
            str(i // len(EFFORTS) + 1), r["effort"], f"{r['seconds']:.1f}",
            tokens(r, "prompt_tokens"), tokens(r, "completion_tokens"),
            tokens(r, "completion_tokens_details", "reasoning_tokens"), tokens(r, "total_tokens"),
            cell(answer) or "пусто", thought]) + " |")
    lines += ["", "«Ответ гостю» — то, что бот отправил бы в чат (содержимое тега `<ответ>`). "
                  "Полный текст модели и рассуждения приведены дословно ниже."]
    for i, r in enumerate(results):
        lines += ["", f"### Вопрос {i // len(EFFORTS) + 1}, effort «{r['effort']}»", ""]
        if r["served"]:
            lines += [f"Ответила: {r['served']}; finish_reason: {r['finish']}.", ""]
        if r["error"]:
            lines += [f"Ошибка: {r['error']}", ""]
        lines += ["**Ответ модели (message.content):**", "",
                  block(r["content"]) if r["content"] else "Пусто.", "",
                  "**Рассуждение (message.reasoning):**", "",
                  block(r["reasoning"]) if r["reasoning"] else "Пусто."]
    return "\n".join(lines) + "\n"


def main() -> None:
    bot.load_env()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY не задан в .env")
    questions = load_questions(CASES_FILE.read_text(encoding="utf-8"))
    system_prompt = bot.build_system_prompt()
    previous = REPORT_FILE.read_text(encoding="utf-8") if REPORT_FILE.exists() else ""
    if SECTION in previous:
        raise SystemExit(f"В {REPORT_FILE.name} уже есть раздел «{SECTION[3:]}». "
                         "Удалите или переименуйте его перед новым запуском.")
    if "--check" in sys.argv[1:]:
        print(f"Модель: {MODEL}\nSystem prompt: {len(system_prompt)} символов\nКлюч: задан")
        for n, q in enumerate(questions, 1):
            print(f"{n}. {q}")
        print(f"Будет запросов: {len(questions) * len(EFFORTS)}. Сейчас ничего не отправлено.")
        return

    started = datetime.now(bot.TZ).strftime("%Y-%m-%d %H:%M (UTC+5)")
    head = previous.rstrip("\n") + "\n\n---\n\n" if previous else ""
    results = []
    for n, question in enumerate(questions, 1):
        for effort in EFFORTS:
            print(f"вопрос {n}, effort {effort}…", flush=True)
            r = ask(key, system_prompt, question, effort)
            results.append(r)
            print(f"  {r['seconds']:.1f} с, токенов: {tokens(r, 'total_tokens')}"
                  + (f", ошибка: {r['error']}" if r["error"] else ""), flush=True)
            # пишем после каждого запроса: если прервут, сделанное не пропадёт
            with REPORT_FILE.open("w", encoding="utf-8", newline="\n") as f:
                f.write(head + render(questions, results, started))
    print(f"Готово: {len(results)} запросов, результат в {REPORT_FILE.name}")


if __name__ == "__main__":
    main()
