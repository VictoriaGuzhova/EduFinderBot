import requests
import telebot
from telebot import types
from datetime import datetime, timedelta
import sqlite3
import xml.etree.ElementTree as ET
from config import token

bot = telebot.TeleBot(token)

# Состояния пользователей
user_states = {}

# Кэш
cache = {}  # общий кэш: { "Python": [результаты] }

# Инициализация бд
def init_db():
    conn = sqlite3.connect("users.db")
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Таблица истории поиска
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        query TEXT,
        source TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
    )
    """)

    # Таблица избранного
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        authors TEXT,
        year TEXT,
        abstract TEXT,
        link TEXT,
        source TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
    )
    """)

    conn.commit()
    conn.close()

init_db()

# Функции работы с бд
def add_user(chat_id, username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)", (chat_id, username))
    conn.commit()
    conn.close()

def add_search(chat_id, query, source):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO search_history (chat_id, query, source) VALUES (?, ?, ?)", (chat_id, query, source))
    conn.commit()
    conn.close()

def get_history(chat_id, limit=10):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT query, source, timestamp FROM search_history WHERE chat_id=? ORDER BY timestamp DESC LIMIT ?", (chat_id, limit))
    rows = cursor.fetchall()
    conn.close()

    adjusted = []
    for query, source, ts in rows:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(hours=4)  # UTC+4 (+1 от МСК)
            ts_adj = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_adj = ts
        adjusted.append((query, source, ts_adj))
    return adjusted

def clear_user_history(chat_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM search_history WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def add_favorite(chat_id, item, source):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO favorites (chat_id, title, authors, year, abstract, link, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, item['title'], item['authors'], str(item['year']), item['abstract'], item['link'], source))
    conn.commit()
    conn.close()

def get_favorites(chat_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT title, authors, year, abstract, link, source FROM favorites WHERE chat_id=? ORDER BY timestamp DESC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def clear_user_favorites(chat_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM favorites WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

# Поиск в википедии
def parse_wikipedia_response(data):
    results = []
    for item in data.get("query", {}).get("search", [])[:5]:
        title = item["title"]
        snippet = item["snippet"].replace("<span class=\"searchmatch\">", "").replace("</span>", "")
        page_id = item["pageid"]
        link = f"https://ru.wikipedia.org/?curid={page_id}"
        results.append({
            "title": title,
            "authors": "Wikipedia contributors",
            "year": "—",
            "abstract": snippet,
            "link": link
        })
    return results

def search_wikipedia(query, chat_id):
    if query in cache:
        add_search(chat_id, query, "wikipedia")
        return cache[query], None
    url = "https://ru.wikipedia.org/w/api.php"
    params = {"action":"query","list":"search","srsearch":query,"utf8":1,"format":"json"}
    headers = {"User-Agent":"EduFinderBot/1.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return None, f"⚠️ Ошибка: {e}"
    results = parse_wikipedia_response(data)
    cache[query] = results
    add_search(chat_id, query, "wikipedia")
    return results, None

# Поиск в arxiv
def parse_arxiv_response(xml_text):
    results = []
    root = ET.fromstring(xml_text)
    ns = {"atom":"http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", ns)[:5]:
        title_el = entry.find("atom:title", ns)
        title = (title_el.text or "").strip() if title_el is not None else "—"
        authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns) if a.find("atom:name", ns) is not None]
        abstract_el = entry.find("atom:summary", ns)
        abstract = (abstract_el.text or "").strip() if abstract_el is not None else "—"
        updated_el = entry.find("atom:updated", ns)
        year = "—"
        if updated_el is not None and updated_el.text:
            try:
                year = datetime.fromisoformat(updated_el.text.replace("Z","+00:00")).year
            except Exception:
                year = "—"
        link_el = entry.find("atom:id", ns)
        link = link_el.text.strip() if link_el is not None and link_el.text else "https://arxiv.org"
        results.append({"title":title,"authors":", ".join(authors) if authors else "arXiv authors","year":year,"abstract":abstract,"link":link})
    return results

def search_arxiv(query, chat_id):
    key = f"arxiv:{query}"
    if key in cache:
        add_search(chat_id, query, "arxiv")
        return cache[key], None
    url = "http://export.arxiv.org/api/query"
    params = {"search_query":f"all:{query}","start":0,"max_results":5,"sortBy":"relevance","sortOrder":"descending"}
    headers = {"User-Agent":"EduFinderBot/1.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=8)
        response.raise_for_status()
        xml_text = response.text
    except Exception as e:
        return None, f"⚠️ Ошибка: {e}"
    results = parse_arxiv_response(xml_text)
    cache[key] = results
    add_search(chat_id, query, "arxiv")
    return results, None

# Вывод результатов
def display_results(chat_id, query, results, source):
    if not results:
        bot.send_message(chat_id, "❌ По вашему запросу ничего не найдено.")
        return
    max_cnt = len(results)
    cnt = 0
    for i, res in enumerate(results, start=1):
        cnt = cnt + 1
        text = (
            f"{i}\n"
            f"Название: {res['title']}\n"
            f"Автор(ы): {res['authors']}\n"
            f"Год: {res['year']}\n"
            f"Аннотация: {res['abstract']}\n"
            f"Ссылка: {res['link']}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('Добавить в избранное', callback_data=f"add_fav_{source}_{i}_{query}"))
        bot.send_message(chat_id, text, reply_markup=markup)
        if cnt == max_cnt:
            bot.send_message(chat_id, f"✅ Найдено {len(results)} материалов ({source}) по запросу: \"{query}\"", reply_markup=main_menu())

# Главное меню
def main_menu():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row('🔍Новый поиск')
    keyboard.row('❤️Избранное', '🗞История поиска')
    keyboard.row('⚙️Настройки', '❓Помощь')
    return keyboard

# Команды и обработчики
@bot.message_handler(func=lambda message: message.text == 'Старт')
def start(message):
    add_user(message.chat.id, message.from_user.username or "")
    bot.send_message(message.chat.id, "👋 Добро пожаловать в EDU FINDER BOT!", reply_markup=main_menu())

@bot.message_handler(func=lambda message: message.text == '❓Помощь')
def about(message):
    bot.send_message(message.chat.id, 'ℹ️ Нажми 🔍Новый поиск, выбери источник (Википедия или arXiv), а затем введи запрос.')

@bot.message_handler(func=lambda message: message.text == '🔍Новый поиск')
def search_command(message):
    chat_id = message.chat.id
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📖 Википедия", callback_data="choose_wikipedia"))
    markup.add(types.InlineKeyboardButton("🧪 arXiv", callback_data="choose_arxiv"))
    bot.send_message(chat_id, "Где будем искать материалы?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["choose_wikipedia", "choose_arxiv"])
def choose_source(call):
    chat_id = call.message.chat.id
    if call.data == "choose_wikipedia":
        user_states[chat_id] = 'awaiting_query_wikipedia'
        bot.send_message(chat_id, "📖 Введите тему, название работы или автора для поиска в Википедии…")
    elif call.data == "choose_arxiv":
        user_states[chat_id] = 'awaiting_query_arxiv'
        bot.send_message(chat_id, "🧪 Введите ключевые слова для поиска на arXiv…")
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == 'awaiting_query_wikipedia')
def handle_user_query_wikipedia(message):
    chat_id = message.chat.id
    query = message.text.strip()
    bot.send_message(chat_id, "🔍 Идёт поиск материалов в Википедии…")
    results, error = search_wikipedia(query, chat_id)
    if error:
        bot.send_message(chat_id, error)
    else:
        display_results(chat_id, query, results, source="wikipedia")
    user_states.pop(chat_id, None)

@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == 'awaiting_query_arxiv')
def handle_user_query_arxiv(message):
    chat_id = message.chat.id
    query = message.text.strip()
    bot.send_message(chat_id, "🔎 Идёт поиск материалов на arXiv…")
    results, error = search_arxiv(query, chat_id)
    if error:
        bot.send_message(chat_id, error)
    else:
        display_results(chat_id, query, results, source="arxiv")
    user_states.pop(chat_id, None)

# Добавление в избранное
@bot.callback_query_handler(func=lambda call: call.data.startswith("add_fav_"))
def add_to_favorites(call):
    chat_id = call.message.chat.id
    parts = call.data.split("_")
    source = parts[2]  # 'wikipedia' или 'arxiv'
    item_index = int(parts[3]) - 1
    query = "_".join(parts[4:])
    if source == "arxiv":
        key = f"arxiv:{query}"
        results = cache.get(key, [])
    else:
        results = cache.get(query, [])
    if 0 <= item_index < len(results):
        res = results[item_index]
        add_favorite(chat_id, res, source=source)
        bot.answer_callback_query(call.id, "Добавлено в избранное ✅")
        bot.send_message(chat_id, "⭐ Материал добавлен в избранное.")
    else:
        bot.answer_callback_query(call.id, "Не удалось добавить", show_alert=True)

# Избранное
@bot.message_handler(func=lambda message: message.text == '❤️Избранное')
def favorites(message):
    fav_list = get_favorites(message.chat.id)
    if not fav_list:
        bot.send_message(message.chat.id, "⭐ Избранное\n\nУ вас пока нет сохранённых материалов.")
    else:
        lines = []
        for i, (title, authors, year, abstract, link, source) in enumerate(fav_list, start=1):
            lines.append(f"{i}. [{source}] {title} — {authors}, {year}\n{abstract}\n{link}")

        text = "⭐️ Избранное:\n\n" + "\n\n".join(lines)

        if len(text) > 4096:
            for x in range(0, len(text), 4096):
                bot.send_message(message.chat.id, text[x:x + 4096])
        else:
            bot.send_message(message.chat.id, text)

# История поиска
@bot.message_handler(func=lambda message: message.text == '🗞История поиска')
def history(message):
    chat_id = message.chat.id
    user_history = get_history(chat_id, limit=10)
    if not user_history:
        bot.send_message(chat_id, "🕗 История поиска\n\nИстория пока пуста.")
    else:
        header = "🕗 Ваша история поиска:\n\n"
        lines = [
            f"{i}. {q} — [{src}] — {ts}"
            for i, (q, src, ts) in enumerate(user_history, start=1)
        ]
        text = header + "\n".join(lines)

        for x in range(0, len(text), 4096):
            bot.send_message(message.chat.id, text[x:x + 4096])
# Настройки
@bot.message_handler(func=lambda message: message.text == '⚙️Настройки')
def settings(message):
    markup = telebot.types.InlineKeyboardMarkup()
    clear_cache_btn = telebot.types.InlineKeyboardButton("🧹 Очистить мою историю", callback_data="clear_cache")
    clear_fav_btn = telebot.types.InlineKeyboardButton("🗑️ Очистить избранное", callback_data="clear_fav")
    markup.add(clear_cache_btn)
    markup.add(clear_fav_btn)
    bot.send_message(message.chat.id, "⚙️ Настройки\n\nВыберите действие:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "clear_cache")
def clear_cache(call):
    chat_id = call.message.chat.id
    clear_user_history(chat_id)
    bot.answer_callback_query(call.id, "История очищена ✅")
    bot.send_message(chat_id, "🧹 Ваша история поиска успешно очищена.")

@bot.callback_query_handler(func=lambda call: call.data == "clear_fav")
def clear_fav(call):
    chat_id = call.message.chat.id
    clear_user_favorites(chat_id)
    bot.answer_callback_query(call.id, "Избранное очищено ✅")
    bot.send_message(chat_id, "🗑️ Ваше избранное успешно очищено.")

# Дополнительные команды для CRUD
@bot.message_handler(commands=['update_user'])
def cmd_update_user(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /update_user новое_имя")
        return
    new_username = parts[1].strip()
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username=? WHERE chat_id=?", (new_username, message.chat.id))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"✅ Имя пользователя обновлено: {new_username}")

@bot.message_handler(commands=['delete_user'])
def cmd_delete_user(message):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE chat_id=?", (message.chat.id,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, "🗑️ Профиль пользователя удалён из БД.")

@bot.message_handler(commands=['delete_fav'])
def cmd_delete_fav(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.send_message(message.chat.id, "Использование: /delete_fav ID")
        return
    entry_id = int(parts[1].strip())
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM favorites WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"🗑️ Избранный материал удалён (ID: {entry_id}).")

@bot.message_handler(commands=['update_fav'])
def cmd_update_fav(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Использование: /update_fav ID новое_название")
        return
    entry_id = int(parts[1])
    new_title = parts[2].strip()
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE favorites SET title=? WHERE id=?", (new_title, entry_id))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"✅ Избранное обновлено (ID: {entry_id}) → {new_title}")

@bot.message_handler(commands=['delete_history'])
def cmd_delete_history(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        bot.send_message(message.chat.id, "Использование: /delete_history ID")
        return
    entry_id = int(parts[1].strip())
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM search_history WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"🗑️ Запись истории удалена (ID: {entry_id}).")

@bot.message_handler(commands=['update_history'])
def cmd_update_history(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Использование: /update_history ID новый_запрос")
        return
    entry_id = int(parts[1])
    new_query = parts[2].strip()
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE search_history SET query=? WHERE id=?", (new_query, entry_id))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"✅ История обновлена (ID: {entry_id}) → {new_query}")

# Запуск бота
if __name__ == "__main__":
    bot.polling(none_stop=True)

