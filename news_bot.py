"""
Норвежский новостной бот для Telegram-канала.

Каждое утро:
1. Собирает свежие новости (за последние ~24 часа) из NRK, VG, Aftenposten и Dagbladet.
2. Выбирает топ-N (по умолчанию 5) самых свежих.
3. Переводит каждую новость отдельно через Gemini.
4. Публикует каждую новость отдельным постом: картинка + текст + ссылка на оригинал.
5. Запоминает, какие новости уже публиковались, чтобы не дублировать.

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

# Сколько новостей публиковать каждое утро
TOP_N_NEWS = 5

# За какой период считаем новости "свежими" (часы)
FRESHNESS_WINDOW_HOURS = 24

# Файл с историей опубликованных ссылок
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "published_history.json")
HISTORY_RETENTION_DAYS = 7

# Флаг-заглушка если картинка не найдена
FALLBACK_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d9/Flag_of_Norway.svg/1280px-Flag_of_Norway.svg.png"

# Источники новостей
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


# ==========================================
# БЛОК 1 — ПАМЯТЬ БОТА
# ==========================================

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


# ==========================================
# БЛОК 2 — СБОР НОВОСТЕЙ ИЗ RSS
# ==========================================

def parse_pub_date(item):
    """Парсит дату публикации из RSS-элемента."""
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
    """Скачивает и парсит один RSS-фид."""
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
    """Собирает свежие новости из всех источников."""
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
                if pub_date is not None and pub_date < cutoff:
                    continue

                candidates.append({
                    "source": source_name,
                    "title": title,
                    "description": description,
                    "link": link,
                    "pub_date": pub_date,
                })

    candidates.sort(
        key=lambda x: x["pub_date"] if x["pub_date"] is not None else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    print(f"\n✅ Найдено {len(candidates)} свежих новостей, не публиковавшихся ранее.")
    return candidates[:TOP_N_NEWS]


# ==========================================
# БЛОК 3 — КАРТИНКИ (новый блок)
# Ищем og:image на странице новости.
# og:image — это специальный тег который сайты
# добавляют чтобы соцсети показывали превью.
# ==========================================

def fetch_og_image(url):
    """
    Заходит на страницу новости и ищет тег og:image.
    Если находит — возвращает URL картинки.
    Если нет — возвращает None, и тогда используем флаг-заглушку.
    """
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=REQUEST_HEADERS)
        if response.status_code != 200:
            return None

        # Ищем строку вида: <meta property="og:image" content="https://...">
        # Делаем это простым поиском по тексту, без тяжёлых библиотек
        html = response.text
        marker = 'property="og:image"'
        pos = html.find(marker)
        if pos == -1:
            # Некоторые сайты пишут по-другому
            marker = "property='og:image'"
            pos = html.find(marker)
        if pos == -1:
            return None

        # После маркера ищем content="..."
        content_pos = html.find('content="', pos)
        if content_pos == -1:
            content_pos = html.find("content='", pos)
        if content_pos == -1:
            return None

        # Определяем какая кавычка используется
        quote_char = html[content_pos + 8]
        start = content_pos + 9
        end = html.find(quote_char, start)
        if end == -1:
            return None

        image_url = html[start:end].strip()
        if image_url.startswith("http"):
            return image_url
        return None

    except requests.RequestException:
        return None


# ==========================================
# БЛОК 4 — ПЕРЕВОД ЧЕРЕЗ GEMINI
# Теперь переводим каждую новость отдельно,
# а не все вместе — чтобы каждый пост был
# самостоятельным и красивым.
# ==========================================

def translate_single_news(news, model):
    """
    Переводит одну новость через Gemini.
    Возвращает готовый текст для Telegram-поста.
    """
    today = datetime.now().strftime("%d.%m.%Y")

    prompt = f"""Ты профессиональный редактор новостей.
Переведи и адаптируй эту норвежскую новость на русский язык для украинцев, живущих в Норвегии.

Заголовок: {news['title']}
Описание: {news['description']}
Источник: {news['source']}
Дата: {today}

Напиши пост строго по этому шаблону (без Markdown-разметки типа ** или #):

🇳🇴 [Заголовок на русском, кратко и по делу]

[2-3 предложения сути новости, живым языком]

Почему это важно: [1 предложение — практический вывод для иммигранта]

📰 {news['source']} • {today}

Не добавляй ссылки в текст — они будут добавлены отдельно.
Не пиши ничего от себя, только сам пост по шаблону."""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Ошибка Gemini для новости '{news['title']}': {e}")
        # Если Gemini недоступен — публикуем оригинальный заголовок
        return f"🇳🇴 {news['title']}\n\n{news['description']}\n\n📰 {news['source']} • {today}"


# ==========================================
# БЛОК 5 — ОТПРАВКА В TELEGRAM
# Теперь каждая новость = отдельный пост
# с картинкой (sendPhoto) или без (sendMessage)
# ==========================================

def send_news_post(news, text):
    """
    Отправляет одну новость в канал.
    Сначала пробует отправить с картинкой (sendPhoto).
    Если картинка не найдена или не загрузилась — использует флаг 🇳🇴.
    Ссылка на оригинал добавляется как кнопка под постом.
    """
    # Кнопка-ссылка под постом (inline keyboard)
    reply_markup = {
        "inline_keyboard": [[
            {"text": f"📖 Читать на {news['source']}", "url": news['link']}
        ]]
    }

    # Ищем картинку на странице новости
    print(f"   Ищем картинку для: {news['title'][:50]}...")
    image_url = fetch_og_image(news['link'])

    if image_url:
        print(f"   ✅ Картинка найдена")
    else:
        print(f"   🇳🇴 Картинка не найдена — используем флаг")
        image_url = FALLBACK_IMAGE_URL

    # Пробуем отправить с картинкой
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "photo": image_url,
        "caption": text,
        "reply_markup": reply_markup,
    }

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        data = response.json()

        if data.get("ok"):
            print(f"   ✅ Пост опубликован!")
            return True

        # Если картинка не загрузилась — отправляем просто текст
        print(f"   ⚠️ Не удалось отправить фото: {data.get('description')} — отправляем текстом")
        url_text = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload_text = {
            "chat_id": CHANNEL_ID,
            "text": text,
            "reply_markup": reply_markup,
        }
        response2 = requests.post(url_text, json=payload_text, timeout=REQUEST_TIMEOUT)
        data2 = response2.json()
        if data2.get("ok"):
            print(f"   ✅ Пост опубликован (текстом)!")
            return True
        else:
            print(f"   ❌ Ошибка отправки: {data2}")
            return False

    except requests.RequestException as e:
        print(f"   ❌ Сетевая ошибка: {e}")
        return False


# ==========================================
# БЛОК 6 — ПРОВЕРКА НАСТРОЕК
# ==========================================

def validate_config():
    missing = [name for name, val in [
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
        ("CHANNEL_ID", CHANNEL_ID),
        ("GEMINI_KEY", GEMINI_KEY),
    ] if not val]
    if missing:
        print(f"❌ Не заданы переменные окружения: {', '.join(missing)}")
        sys.exit(1)


# ==========================================
# БЛОК 7 — ГЛАВНАЯ ФУНКЦИЯ
# Дирижёр: вызывает все блоки по порядку
# ==========================================

def main():
    print("🔄 Запуск новостного бота...")
    validate_config()
    genai.configure(api_key=GEMINI_KEY)
    model = genai.GenerativeModel(model_name="models/gemini-2.5-flash")

    # Шаг 1: загружаем память о прошлых публикациях
    history = load_history()

    # Шаг 2: собираем свежие новости
    news_list = collect_fresh_news(history)
    if not news_list:
        print("❌ Свежих неопубликованных новостей не найдено.")
        return

    print(f"\nВыбраны для публикации ({len(news_list)}):")
    for n in news_list:
        print(f"   [{n['source']}] {n['title']}")

    # Шаг 3: публикуем каждую новость отдельным постом
    print(f"\n📤 Публикуем {len(news_list)} постов...")
    published_count = 0

    for i, news in enumerate(news_list, start=1):
        print(f"\n--- Новость {i}/{len(news_list)} ---")

        # Переводим через Gemini
        text = translate_single_news(news, model)

        # Отправляем в Telegram
        success = send_news_post(news, text)

        if success:
            # Запоминаем что опубликовали
            history[news['link']] = datetime.now(timezone.utc).isoformat()
            published_count += 1

        # Пауза между постами чтобы не спамить
        if i < len(news_list):
            print("   ⏳ Пауза 3 секунды...")
            time.sleep(3)

    # Шаг 4: сохраняем обновлённую историю
    save_history(history)

    print(f"\n🎉 Готово! Опубликовано {published_count} из {len(news_list)} новостей.")


if __name__ == "__main__":
    main()
