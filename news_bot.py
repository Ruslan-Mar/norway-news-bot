"""
Норвежский новостной бот для Telegram-канала.

Каждое утро:
1. Собирает свежие новости (за последние ~24 часа) из NRK, VG, Aftenposten и Dagbladet.
2. Выбирает топ-N (по умолчанию 5) самых свежих/важных.
3. Переводит и адаптирует их на русский язык одним постом через Gemini.
4. Публикует пост в Telegram-канал со ссылками на оригиналы.
5. Запоминает, какие новости уже публиковались, чтобы не дублировать на следующий день.

Запуск: python news_bot.py
Требуемые переменные окружения: TELEGRAM_TOKEN, CHANNEL_ID, GEMINI_KEY
"""

import os
import sys
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import google.generativeai as genai

# ==========================================
# НАСТРОЙКИ
# ==========================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

# Сколько новостей включать в один утренний пост
TOP_N_NEWS = 5

# За какой период считаем новости "свежими" (часы)
FRESHNESS_WINDOW_HOURS = 24

# Файл, где храним ссылки на уже опубликованные новости (защита от дублей)
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "published_history.json")
# Сколько дней хранить историю (чтобы файл не рос бесконечно)
HISTORY_RETENTION_DAYS = 7

# Источники новостей: RSS-ленты норвежских СМИ
RSS_SOURCES = {
    "NRK": [
        "https://www.nrk.no/toppsaker.rss",
        "https://www.nrk.no/oslo/toppsaker.rss",
    ],
    "VG": [
        "https://www.vg.no/rss/feed/?format=rss",
    ],
    "Aftenposten": [
        "https://www.aftenposten.no/rss",
    ],
    "Dagbladet": [
        "https://www.dagbladet.no/rss/nyheter/",
    ],
}

REQUEST_TIMEOUT = 10
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://t.me/)"
}


def load_history():
    """Загружает историю опубликованных ссылок, отбрасывая устаревшие записи."""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    cleaned = {}
    for link, published_at in data.items():
        try:
            ts = datetime.fromisoformat(published_at)
        except ValueError:
            continue
        if ts >= cutoff:
            cleaned[link] = published_at
    return cleaned


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️ Не удалось сохранить историю публикаций: {e}")


def parse_pub_date(item):
    """Пытается распарсить дату публикации из RSS-айтема. Возвращает aware datetime в UTC или None."""
    for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date", "published"):
        el = item.find(tag)
        if el is not None and el.text:
            try:
                dt = parsedate_to_datetime(el.text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_feed(url):
    """Скачивает и парсит один RSS-фид. Возвращает список айтемов (raw XML elements) или []."""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
        if response.status_code != 200:
            print(f"   ⚠️ {url} → HTTP {response.status_code}")
            return []
        root = ET.fromstring(response.content)
        return root.findall(".//item")
    except ET.ParseError as e:
        print(f"   ⚠️ Ошибка парсинга XML {url}: {e}")
        return []
    except requests.RequestException as e:
        print(f"   ⚠️ Ошибка сети {url}: {e}")
        return []


def collect_fresh_news(history):
    """Собирает свежие новости из всех источников, фильтрует по времени и истории публикаций."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_WINDOW_HOURS)
    candidates = []

    for source_name, urls in RSS_SOURCES.items():
        for url in urls:
            print(f"Проверяем источник {source_name} ({url})...")
            items = fetch_feed(url)
            for item in items:
                title_el = item.find("title")
                link_el = item.find("link")
                desc_el = item.find("description")

                title = title_el.text.strip() if title_el is not None and title_el.text else None
                link = link_el.text.strip() if link_el is not None and link_el.text else None
                description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

                if not title or not link:
                    continue
                if link in history:
                    continue

                pub_date = parse_pub_date(item)
                # Если дату распарсить не удалось — не отбрасываем новость,
                # но и не можем подтвердить, что она "свежая"; помечаем как None
                # и сортируем такие в конец списка.
                if pub_date is not None and pub_date < cutoff:
                    continue

                candidates.append({
                    "source": source_name,
                    "title": title,
                    "description": description,
                    "link": link,
                    "pub_date": pub_date,
                })

    # Сортируем: сначала те, у кого есть дата (самые свежие первыми),
    # затем те, у кого даты нет.
    candidates.sort(
        key=lambda x: x["pub_date"] if x["pub_date"] is not None else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    print(f"\n✅ Найдено {len(candidates)} свежих новостей (за последние {FRESHNESS_WINDOW_HOURS}ч), не публиковавшихся ранее.")
    return candidates[:TOP_N_NEWS]


def build_digest_prompt(news_list):
    """Формирует промпт для Gemini, чтобы получить один пост со всеми новостями."""
    items_text = ""
    for i, news in enumerate(news_list, start=1):
        items_text += (
            f"\n--- Новость {i} (источник: {news['source']}) ---\n"
            f"Заголовок: {news['title']}\n"
            f"Описание: {news['description']}\n"
        )

    prompt = f"""Ты профессиональный переводчик и редактор новостей.
Ниже даны {len(news_list)} норвежских новостей за сегодня. Переведи и адаптируй их на русский язык
для украинских иммигрантов, живущих в Норвегии. Текст должен быть понятным, живым и практически полезным.

Сделай ОДИН пост-дайджест по следующему шаблону (используй простые дефисы для списков,
не используй Markdown-разметку типа ** или #, чтобы не ломать сообщение в Telegram):

🇳🇴 НОВОСТИ НОРВЕГИИ — [сегодняшняя дата в формате ДД.ММ.ГГГГ]

Затем для каждой новости отдельный блок строго в таком виде:

[номер]. [Заголовок на русском, кратко и по делу]
[1-2 предложения сути новости]
Почему это важно: [короткий практический вывод для иммигранта]

Если несколько новостей примерно об одном и том же — объедини их в один пункт, не повторяйся.
Не добавляй ссылки и не упоминай источники в тексте — они будут добавлены отдельно после твоего текста.
Не пиши никакого вступления или заключения от себя, только сам дайджест по шаблону.

Вот новости:
{items_text}
"""
    return prompt


def translate_and_adapt(news_list):
    """Отправляет пачку новостей в Gemini, получает готовый текст дайджеста."""
    print("\n2. Отправляем новости в Gemini для перевода и адаптации...")
    prompt = build_digest_prompt(news_list)

    model = genai.GenerativeModel(model_name="models/gemini-2.5-flash")
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"❌ Ошибка при обращении к Gemini API: {e}")
        return None


def append_sources(digest_text, news_list):
    """Добавляет в конец поста список ссылок на оригиналы."""
    sources_block = "\n\n📎 Источники:\n"
    for i, news in enumerate(news_list, start=1):
        sources_block += f"{i}. {news['source']}: {news['link']}\n"
    return digest_text.strip() + sources_block


def send_to_telegram(text):
    """Публикация сообщения в Telegram-канал. Разбивает на части, если текст слишком длинный."""
    print("\n3. Публикуем в Telegram...")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # Telegram ограничивает сообщение 4096 символами
    MAX_LEN = 4096
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [text]

    for chunk in chunks:
        payload = {"chat_id": CHANNEL_ID, "text": chunk}
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            data = response.json()
        except requests.RequestException as e:
            print(f"❌ Сетевая ошибка при отправке в Telegram: {e}")
            return False
        except ValueError:
            print(f"❌ Telegram вернул не-JSON ответ: {response.text}")
            return False

        if not data.get("ok"):
            print(f"❌ Ошибка отправки в Telegram: {data}")
            return False
        time.sleep(0.5)  # небольшая пауза между частями, если их несколько

    print("🎉 Успех! Дайджест опубликован в канале!")
    return True


def validate_config():
    missing = [name for name, val in [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
        ("CHANNEL_ID", CHANNEL_ID),
        ("GEMINI_KEY", GEMINI_KEY),
    ] if not val]
    if missing:
        print(f"❌ Не заданы переменные окружения: {', '.join(missing)}")
        print("   Установи их перед запуском, например через export или GitHub Secrets.")
        sys.exit(1)


def main():
    print("🔄 Запуск новостного бота...")
    validate_config()
    genai.configure(api_key=GEMINI_KEY)

    history = load_history()

    news_list = collect_fresh_news(history)
    if not news_list:
        print("❌ Свежих неопубликованных новостей не найдено. Завершение работы.")
        return

    print(f"\nВыбраны для дайджеста ({len(news_list)}):")
    for n in news_list:
        print(f"   [{n['source']}] {n['title']}")

    digest = translate_and_adapt(news_list)
    if not digest:
        print("❌ Не удалось получить ответ от Gemini. Завершение работы без публикации.")
        return

    final_text = append_sources(digest, news_list)

    if send_to_telegram(final_text):
        # Отмечаем новости как опубликованные только при успешной отправке
        now_iso = datetime.now(timezone.utc).isoformat()
        for n in news_list:
            history[n["link"]] = now_iso
        save_history(history)
    else:
        print("⚠️ Публикация не удалась — история не обновлена, новости останутся кандидатами на следующий запуск.")


if __name__ == "__main__":
    main()
