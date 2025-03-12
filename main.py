import logging
import datetime
from datetime import timedelta, time
import time
import uuid
import pytz
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters
)
import sqlite3

# logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# timezone preps
SYSTEM_TIMEZONE_OFFSET = -time.timezone // 3600
logger.info(f"Системная таймзона: GMT{'+' if SYSTEM_TIMEZONE_OFFSET >= 0 else ''}{SYSTEM_TIMEZONE_OFFSET}")

# conversation states
(
    AWAITING_REGISTRATION_BIRTHDAY,  # 0: waiting for registration birthday input
    MAIN_MENU,                       # 1: main menu
    AWAITING_FRIEND_ID,              # 2: waiting for friend's username input
    AWAITING_FRIEND_BIRTHDAY,        # 3: waiting for friend's birthday input
    AWAITING_FRIEND_TO_DELETE,       # 4: waiting for friend deletion input
    GROUPS_MENU,                     # 5: groups menu
    AWAITING_GROUP_NAME,             # 6: waiting for group name input
    AWAITING_GROUP_KEY,              # 7: waiting for group join code input
    AWAITING_GROUP_TO_LEAVE,         # 8: waiting for group leave code input
    GROUP_MANAGEMENT_MENU,           # 9: group management menu
    AWAITING_NEW_GROUP_NAME,         # 10: waiting for new group name input
    AWAITING_USER_TO_KICK,           # 11: waiting for username input to kick from group
    AWAITING_ALERT_HOURS,            # 12: waiting for alert hours input
    GROUP_PARTICIPANTS_MENU,         # 13: group participants menu
    SETTINGS_MENU,                   # 14: settings menu
    SETTINGS_BIRTHDAY,               # 15: waiting for new birthday input (in settings)
    SETTINGS_TIMEZONE                # 16: waiting for timezone input (in settings)
) = range(17)

# age calculation stuff
def calculate_age(birthday_str, reference_date=None):
    if not birthday_str or len(birthday_str) < 10:
        return None
    try:
        day, month, year = birthday_str.split('-')
        birth_date = datetime.date(int(year), int(month), int(day))
        
        if reference_date is None:
            reference_date = datetime.date.today()
            
        age = reference_date.year - birth_date.year
        if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    except (ValueError, IndexError, TypeError):
        return None

def calculate_upcoming_age(birthday_str):
    if not birthday_str or len(birthday_str) < 10:
        return None
    
    try:
        day, month, year = birthday_str.split('-')
        birth_date = datetime.date(int(year), int(month), int(day))
        today = datetime.date.today()
        
        upcoming_birthday = birth_date.replace(year=today.year)
        if upcoming_birthday < today:
            upcoming_birthday = upcoming_birthday.replace(year=today.year + 1)
            
        upcoming_age = upcoming_birthday.year - birth_date.year
        return upcoming_age
    except (ValueError, IndexError, TypeError):
        return None

# text formatting
def format_days_word(days):
    if days == 0:
        return "сегодня"
    if 11 <= days % 100 <= 14:
        return f"через {days} дней"
    if days % 10 == 1:
        return f"через {days} день"
    if 2 <= days % 10 <= 4:
        return f"через {days} дня"
    return f"через {days} дней"
def format_hours_word(hours):
    if 11 <= hours % 100 <= 14:
        return "часов"
    if hours % 10 == 1:
        return "час"
    if 2 <= hours % 10 <= 4:
        return "часа"
    return "часов"

# -------------------------------
# Database functions

def get_db_connection():
    conn = sqlite3.connect('birthday_bot.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        display_name TEXT,
        birthday TEXT,
        alert_hours INTEGER DEFAULT 0,
        timezone INTEGER DEFAULT 3
    )
    ''')
    # friends table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS friends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        friend_username TEXT,
        birthday TEXT,
        FOREIGN KEY (username) REFERENCES users (username),
        UNIQUE(username, friend_username)
    )
    ''')
    # groups table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS groups (
        group_id TEXT PRIMARY KEY,
        name TEXT,
        creator_username TEXT,
        code TEXT UNIQUE,
        FOREIGN KEY (creator_username) REFERENCES users (username)
    )
    ''')
    # group_members table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT,
        username TEXT,
        FOREIGN KEY (group_id) REFERENCES groups (group_id),
        FOREIGN KEY (username) REFERENCES users (username),
        UNIQUE(group_id, username)
    )
    ''')
    # telegram_ids table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS telegram_ids (
        username TEXT PRIMARY KEY,
        chat_id INTEGER UNIQUE
    )
    ''')
    
    # db migration -> add timezones
    try:
        cursor.execute("SELECT timezone FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN timezone INTEGER DEFAULT 3")
        logger.info("Добавлена колонка timezone в таблицу users")
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# -------------------------------
# User functions

def is_user_registered(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result is not None and result['birthday'] is not None

def register_user(username, display_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
                   (username, display_name))
    conn.commit()
    conn.close()

def update_user_birthday_db(username, birthday):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET birthday = ? WHERE username = ?", (birthday, username))
    cursor.execute("UPDATE friends SET birthday = ? WHERE friend_username = ?", (birthday, username))
    conn.commit()
    conn.close()

def get_user_birthday(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT birthday FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result['birthday'] if result and result['birthday'] else None

def get_user_by_username(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username, display_name, birthday FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result

# -------------------------------
# Timezone functions

def update_timezone_settings(username, timezone):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET timezone = ? WHERE username = ?", (timezone, username))
    conn.commit()
    conn.close()

def get_user_timezone(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result['timezone'] if result else 3  # defaults GMT+3

# -------------------------------
# Friends functions

def add_friend(username, friend_username, birthday=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT birthday FROM users WHERE username = ?", (friend_username,))
    friend_record = cursor.fetchone()
    if friend_record and friend_record['birthday']:
        birthday = friend_record['birthday']
    cursor.execute("INSERT OR REPLACE INTO friends (username, friend_username, birthday) VALUES (?, ?, ?)",
                   (username, friend_username, birthday))
    conn.commit()
    conn.close()

def get_friends(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT friend_username, birthday FROM friends WHERE username = ?", (username,))
    result = cursor.fetchall()
    conn.close()
    return result

def delete_friend(username, friend_username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM friends WHERE username = ? AND friend_username = ?", (username, friend_username))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0

# -------------------------------
# Groups functions

def create_group(name, creator_username):
    conn = get_db_connection()
    cursor = conn.cursor()
    group_id = str(uuid.uuid4())
    code = str(uuid.uuid4())[:8]
    cursor.execute("INSERT INTO groups (group_id, name, creator_username, code) VALUES (?, ?, ?, ?)",
                   (group_id, name, creator_username, code))
    cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, creator_username))
    conn.commit()
    conn.close()
    return group_id, code

def join_group(code, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT group_id FROM groups WHERE code = ?", (code,))
    result = cursor.fetchone()
    if not result:
        conn.close()
        return False, None
    group_id = result['group_id']
    try:
        cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, username))
        cursor.execute("SELECT name FROM groups WHERE group_id = ?", (group_id,))
        group_name = cursor.fetchone()['name']
        conn.commit()
        conn.close()
        return True, group_name
    except sqlite3.IntegrityError:
        conn.close()
        return False, None

def leave_group(code, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT group_id FROM groups WHERE code = ?", (code,))
    result = cursor.fetchone()
    if not result:
        conn.close()
        return False, None
    group_id = result['group_id']
    cursor.execute("SELECT name, creator_username FROM groups WHERE group_id = ?", (group_id,))
    group = cursor.fetchone()
    if group['creator_username'] == username:
        cursor.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        cursor.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        conn.commit()
        conn.close()
        return True, f"{group['name']} (удалена, так как вы её создатель)"
    else:
        cursor.execute("DELETE FROM group_members WHERE group_id = ? AND username = ?", (group_id, username))
        conn.commit()
        conn.close()
        return True, group['name']

def get_user_groups(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT g.group_id, g.name, g.code, g.creator_username,
               COUNT(gm.username) as member_count
        FROM groups g
        JOIN group_members gm ON g.group_id = gm.group_id
        WHERE g.group_id IN (
            SELECT group_id FROM group_members WHERE username = ?
        )
        GROUP BY g.group_id
        """, (username,)
    )
    result = cursor.fetchall()
    conn.close()
    return result

def get_managed_groups(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT g.group_id, g.name, g.code, g.creator_username,
               COUNT(gm.username) as member_count
        FROM groups g
        JOIN group_members gm ON g.group_id = gm.group_id
        WHERE g.creator_username = ?
        GROUP BY g.group_id
        """, (username,)
    )
    result = cursor.fetchall()
    conn.close()
    return result

def rename_group(group_id, new_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE groups SET name = ? WHERE group_id = ?", (new_name, group_id))
    conn.commit()
    conn.close()

def get_group_members(group_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.username, u.display_name, u.birthday
        FROM users u
        JOIN group_members gm ON u.username = gm.username
        WHERE gm.group_id = ?
        """, (group_id,)
    )
    result = cursor.fetchall()
    conn.close()
    return result

def kick_from_group(group_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM group_members WHERE group_id = ? AND username = ?", (group_id, username))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

# -------------------------------
# Statistics and alert functions

def get_user_stats(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM friends WHERE friend_username = ?", (username,))
    follower_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM group_members WHERE username = ?", (username,))
    group_count = cursor.fetchone()[0]
    conn.close()
    return follower_count, group_count

def update_alert_settings(username, hours):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET alert_hours = ? WHERE username = ?", (hours, username))
    conn.commit()
    conn.close()

def get_user_alert_settings(username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT alert_hours FROM users WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result['alert_hours'] if result else 0

def get_birthday_people(date_str):
    # birthday stored as DD-MM-ГГГГ, so we compare first 5 characters (DD-MM)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT username, display_name, birthday
        FROM users
        WHERE substr(birthday, 1, 5) = ?
        """, (date_str,)
    )
    users_with_birthday = cursor.fetchall()
    conn.close()
    return users_with_birthday

def get_followers(birthday_username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT u.username, u.alert_hours
        FROM users u
        JOIN friends f ON u.username = f.username
        WHERE f.friend_username = ?
        """, (birthday_username,)
    )
    direct_followers = cursor.fetchall()
    cursor.execute(
        """
        SELECT DISTINCT u.username, u.alert_hours
        FROM users u
        JOIN group_members gm1 ON u.username = gm1.username
        JOIN group_members gm2 ON gm1.group_id = gm2.group_id
        WHERE gm2.username = ? AND u.username != ?
        """, (birthday_username, birthday_username)
    )
    group_followers = cursor.fetchall()
    conn.close()
    all_followers = {}
    for follower in direct_followers:
        all_followers[follower['username']] = follower['alert_hours']
    for follower in group_followers:
        if follower['username'] not in all_followers:
            all_followers[follower['username']] = follower['alert_hours']
    return all_followers

# -------------------------------
# Keyboards

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["👥 управление друзьями"],
        ["👪 группы", "⚙️ настройки"],
        ["📊 статистика", "❓ помощь"]
    ], resize_keyboard=True)

def get_friends_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ добавить друга", "👀 список друзей"],
        ["➖ удалить друга"],
        ["◀️ вернуться в главное меню"]
    ], resize_keyboard=True)

def get_groups_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ создать группу", "🔑 присоединиться к группе"],
        ["📝 управление группами", "🚪 покинуть группу"],
        ["📋 список участников"],
        ["◀️ вернуться в главное меню"]
    ], resize_keyboard=True)

def get_group_management_keyboard():
    return ReplyKeyboardMarkup([
        ["✏️ переименовать группу", "👞 исключить пользователя"],
        ["◀️ вернуться к группам"]
    ], resize_keyboard=True)

def get_settings_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["Изменить уведомления", "Изменить дату рождения"],
        ["Изменить часовой пояс"],
        ["◀️ назад"]
    ], resize_keyboard=True)

def get_back_button():
    return ReplyKeyboardMarkup([["◀️ отмена"]], resize_keyboard=True)

# -------------------------------
# Save Telegram chat id

def save_chat_id(username, chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO telegram_ids (username, chat_id) VALUES (?, ?)",
                   (username, chat_id))
    conn.commit()
    conn.close()

# -------------------------------
# Additional function: get shared groups between current user and friend

def get_shared_groups(current_username, friend_username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT g.name
        FROM groups g
        JOIN group_members gm_friend ON g.group_id = gm_friend.group_id
        WHERE gm_friend.username = ?
          AND g.group_id IN (
              SELECT group_id FROM group_members WHERE username = ?
          )
        """, (friend_username, current_username)
    )
    groups = cursor.fetchall()
    conn.close()
    return [row['name'] for row in groups]

# -------------------------------
# Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    chat_id = update.effective_chat.id
    if not username:
        await update.message.reply_text("Для использования бота необходимо установить username в Telegram.")
        return ConversationHandler.END
    save_chat_id(username, chat_id)
    register_user(username, user.first_name)
    if is_user_registered(username):
        await update.message.reply_text(f"С возвращением, {user.first_name}!",
                                        reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    else:
        await update.message.reply_text(f"Привет, {user.first_name}! Введите дату рождения в формате ДД-ММ-ГГГГ (например, 15-05-1990):",
                                        reply_markup=get_back_button())
        return AWAITING_REGISTRATION_BIRTHDAY

async def registration_birthday_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    birthday = update.message.text.strip()
    username = user.username
    if birthday.lower() == "◀️ отмена":
        await update.message.reply_text("Регистрация отменена. Для работы введите дату рождения (/start).")
        return ConversationHandler.END
    try:
        datetime.datetime.strptime(birthday, "%d-%m-%Y")
        update_user_birthday_db(username, birthday)
        await update.message.reply_text(f"Отлично! Ваш день рождения: {birthday}.\n\nВыберите дальнейшее действие:",
                                        reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Введите дату в формате ДД-ММ-ГГГГ (например, 15-05-1990):")
        return AWAITING_REGISTRATION_BIRTHDAY

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    username = user.username

    if text == "👥 управление друзьями":
        await update.message.reply_text("Выберите действие:", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU

    elif text == "👪 группы":
        groups = get_user_groups(username)
        message = "Ваши группы:\n\n"
        if not groups:
            message = "У вас пока нет групп.\n\n"
        else:
            for group in groups:
                creator = "👑 вы создатель" if group['creator_username'] == username else ""
                message += f"📌 {group['name']} (код: {group['code']}) — {group['member_count']} участников {creator}\n"
        message += "\nВыберите действие:"
        await update.message.reply_text(message, reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU

    elif text == "⚙️ настройки":
        await update.message.reply_text("Выберите настройку:", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU

    elif text == "📊 статистика":
        birthday = get_user_birthday(username)
        follower_count, group_count = get_user_stats(username)
        timezone = get_user_timezone(username)
        sign = "+" if timezone >= 0 else ""
        
        await update.message.reply_text(
            f"📊 Ваша статистика:\n\n"
            f"День рождения: {birthday}\n"
            f"Следят за вашим днем рождения: {follower_count}\n"
            f"Групп: {group_count}\n"
            f"Часовой пояс: GMT{sign}{timezone}\n\n"
            f"Выберите действие:",
            reply_markup=get_main_menu_keyboard()
        )
        return MAIN_MENU

    elif text == "❓ помощь":
        await update.message.reply_text("Этот бот держит все дни рождения ваших друзей!\n\nВозможности:\n✅ Добавьте любого пользователя Telegram и получайте уведомления о дне рождения\n✅ Зарегистрируйтесь в боте, чтобы синхронизировать ваш день рождения\n✅ Вы можете добавить незарегистрированного пользователя и указать свою дату\n✅ Объединяйтесь в группы, чтобы все участники получали уведомления о днях рождения друг друга\n✅ Поставьте напоминание за N часов до даты, чтобы подготовиться заранее\n✅ Настройте часовой пояс, чтобы правильно поздравлять людей в другом часовом поясе\n✅ Статистика даст вам знать, сколько людей ждут ваш день рождения\n\n(иногда бот может отваливаться, попробуйте снова ввести /start для перелогина)",
                                        reply_markup=get_main_menu_keyboard())
        return MAIN_MENU

    elif text == "◀️ вернуться в главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
        return MAIN_MENU

    elif text == "➕ добавить друга":
        await update.message.reply_text("Введите username друга (например, username или @username):", reply_markup=get_back_button())
        return AWAITING_FRIEND_ID

    elif text == "👀 список друзей":
        return await handle_list_friends(update, context)

    elif text == "➖ удалить друга":
        await update.message.reply_text("Введите username друга для удаления (например, username или @username):", reply_markup=get_back_button())
        return AWAITING_FRIEND_TO_DELETE

    return MAIN_MENU

def get_group_members_for_user(username):
    """
    Get all users from groups that the current user is in
    Returns a list of dictionaries with username, birthday, and group names
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all groups that user is in
    cursor.execute(
        """
        SELECT g.group_id, g.name
        FROM groups g
        JOIN group_members gm ON g.group_id = gm.group_id
        WHERE gm.username = ?
        """, (username,)
    )
    groups = cursor.fetchall()
    
    group_members = {}
    
    # For each group, get all members except the current user
    for group in groups:
        group_id = group['group_id']
        group_name = group['name']
        
        cursor.execute(
            """
            SELECT u.username, u.birthday
            FROM users u
            JOIN group_members gm ON u.username = gm.username
            WHERE gm.group_id = ? AND u.username != ?
            """, (group_id, username)
        )
        members = cursor.fetchall()
        
        for member in members:
            member_username = member['username']
            if member_username not in group_members:
                group_members[member_username] = {
                    'username': member_username,
                    'birthday': member['birthday'],
                    'groups': [group_name]
                }
            else:
                group_members[member_username]['groups'].append(group_name)
    
    conn.close()
    return list(group_members.values())

# Updated function for listing friends with age and timezone info
async def handle_list_friends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    direct_friends = get_friends(username)
    group_friends = get_group_members_for_user(username)
    
    all_contacts = {}
    for friend in direct_friends:
        friend_username = friend['friend_username']
        all_contacts[friend_username] = {
            'username': friend_username,
            'birthday': friend['birthday'],
            'is_direct_friend': True
        }
    
    for friend in group_friends:
        friend_username = friend['username']
        if friend_username not in all_contacts:
            all_contacts[friend_username] = {
                'username': friend_username,
                'birthday': friend['birthday'],
                'is_direct_friend': False,
                'groups': friend['groups']
            }
    
    if not all_contacts:
        await update.message.reply_text("У вас пока нет друзей и контактов в группах.", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    
    today = datetime.date.today()
    user_timezone = get_user_timezone(username)
    
    def days_until(bday_str):
        try:
            bday = datetime.datetime.strptime(bday_str, "%d-%m-%Y").date()
            next_bday = bday.replace(year=today.year)
            if next_bday < today:
                next_bday = next_bday.replace(year=today.year + 1)
            return (next_bday - today).days
        except Exception:
            return float('inf')
    
    # Convert to list and sort by days until birthday
    contacts_list = list(all_contacts.values())
    contacts_sorted = sorted(contacts_list, key=lambda x: days_until(x['birthday']) if x['birthday'] else float('inf'))
    
    message = "Ваши контакты:\n\n"
    
    for idx, contact in enumerate(contacts_sorted, 1):
        contact_username = contact['username']
        birthday_str = contact['birthday'] if contact['birthday'] else "не указан"
        
        # Group info only for non-direct friends
        group_info = ""
        if not contact.get('is_direct_friend', True) and 'groups' in contact:
            group_info = f" (в группе: {', '.join(contact['groups'])})"
        
        # Calculate days until birthday
        days = days_until(contact['birthday'])
        days_info = f" - {format_days_word(days)}" if contact['birthday'] and days != float('inf') else ""
        
        # Calculate upcoming age
        upcoming_age = calculate_upcoming_age(contact['birthday'])
        age_info = f" ({upcoming_age} лет)" if upcoming_age else ""
        
        contact_timezone = get_user_timezone(contact_username)
        timezone_info = format_timezone_difference(user_timezone, contact_timezone) if contact_timezone != user_timezone else ""
        
        message += f"{idx}. @{contact_username}{timezone_info} — Дата: {birthday_str}{age_info}{days_info}{group_info}\n"
    
    await update.message.reply_text(message, reply_markup=get_friends_menu_keyboard())
    return MAIN_MENU

async def friend_username_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    friend_username = update.message.text.strip()
    if friend_username.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    if friend_username.startswith('@'):
        friend_username = friend_username[1:]
    friend = get_user_by_username(friend_username)
    if friend:
        add_friend(user.username, friend_username, friend['birthday'])
        await update.message.reply_text(f"Пользователь @{friend_username} добавлен в список друзей!", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    else:
        context.user_data['temp_friend_username'] = friend_username
        await update.message.reply_text(f"Пользователь @{friend_username} не найден. Укажите дату рождения (ДД-ММ-ГГГГ):", reply_markup=get_back_button())
        return AWAITING_FRIEND_BIRTHDAY

async def friend_birthday_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    birthday = update.message.text.strip()
    if birthday.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    friend_username = context.user_data.get('temp_friend_username')
    try:
        datetime.datetime.strptime(birthday, "%d-%m-%Y")
        add_friend(user.username, friend_username, birthday)
        await update.message.reply_text(f"Пользователь @{friend_username} добавлен с датой рождения {birthday}.", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Введите дату в формате ДД-ММ-ГГГГ:", reply_markup=get_back_button())
        return AWAITING_FRIEND_BIRTHDAY

async def friend_to_delete_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    friend_username = update.message.text.strip()
    if friend_username.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_friends_menu_keyboard())
        return MAIN_MENU
    if friend_username.startswith('@'):
        friend_username = friend_username[1:]
    success = delete_friend(user.username, friend_username)
    if success:
        await update.message.reply_text(f"Пользователь @{friend_username} удалён.", reply_markup=get_friends_menu_keyboard())
    else:
        await update.message.reply_text(f"Пользователь @{friend_username} не найден.", reply_markup=get_friends_menu_keyboard())
    return MAIN_MENU

async def handle_groups_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    username = user.username
    if text == "◀️ вернуться в главное меню":
        await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    elif text == "➕ создать группу":
        await update.message.reply_text("Введите название новой группы:", reply_markup=get_back_button())
        return AWAITING_GROUP_NAME
    elif text == "🔑 присоединиться к группе":
        await update.message.reply_text("Введите код группы:", reply_markup=get_back_button())
        return AWAITING_GROUP_KEY
    elif text == "📝 управление группами":
        managed = get_managed_groups(username)
        context.user_data['managed_groups'] = {group['code']: dict(group) for group in managed}
        message = "Группы, которыми вы управляете:\n\n"
        if not managed:
            message = "У вас нет групп под управлением.\n\n"
        else:
            for group in managed:
                message += f"📌 {group['name']} (код: {group['code']}) — {group['member_count']} участников\n"
        message += "\nВыберите действие:"
        await update.message.reply_text(message, reply_markup=get_group_management_keyboard())
        return GROUP_MANAGEMENT_MENU
    elif text == "🚪 покинуть группу":
        await update.message.reply_text("Введите код группы для выхода:", reply_markup=get_back_button())
        return AWAITING_GROUP_TO_LEAVE
    elif text == "📋 список участников":
        groups = get_user_groups(username)
        if not groups:
            await update.message.reply_text("У вас нет групп.", reply_markup=get_groups_menu_keyboard())
            return GROUPS_MENU
        context.user_data['participant_groups'] = {group['code']: dict(group) for group in groups}
        keyboard = []
        for code, group in context.user_data['participant_groups'].items():
            keyboard.append([f"📋 {group['name']} ({code})"])
        keyboard.append(["◀️ назад"])
        await update.message.reply_text("Выберите группу для просмотра списка участников:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return GROUP_PARTICIPANTS_MENU
    return GROUPS_MENU

async def group_name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    group_name = update.message.text.strip()
    username = user.username
    if group_name.lower() == "◀️ отмена":
        await update.message.reply_text("Создание группы отменено.", reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU
    group_id, code = create_group(group_name, username)
    await update.message.reply_text(f"Группа '{group_name}' создана!\nКод для приглашения: {code}\nПоделитесь кодом с друзьями.", reply_markup=get_groups_menu_keyboard())
    return GROUPS_MENU

async def group_key_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    code = update.message.text.strip()
    if code.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU
    success, group_name = join_group(code, username)
    if success:
        await update.message.reply_text(f"Вы успешно присоединились к группе '{group_name}'!", reply_markup=get_groups_menu_keyboard())
    else:
        await update.message.reply_text("Неверный код или вы уже состоите в группе.", reply_markup=get_groups_menu_keyboard())
    return GROUPS_MENU

async def group_to_leave_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    code = update.message.text.strip()
    if code.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU
    success, group_name = leave_group(code, username)
    if success:
        await update.message.reply_text(f"Вы покинули группу '{group_name}'.", reply_markup=get_groups_menu_keyboard())
    else:
        await update.message.reply_text("Неверный код группы или вы не состоите в группе.", reply_markup=get_groups_menu_keyboard())
    return GROUPS_MENU

async def handle_group_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    username = user.username
    if text == "◀️ вернуться к группам":
        await update.message.reply_text("Меню групп:", reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU
    elif text == "✏️ переименовать группу":
        if 'managed_groups' not in context.user_data or not context.user_data['managed_groups']:
            await update.message.reply_text("У вас нет групп под управлением.", reply_markup=get_group_management_keyboard())
            return GROUP_MANAGEMENT_MENU
        keyboard = []
        for code, group in context.user_data['managed_groups'].items():
            keyboard.append([f"✏️ {group['name']} ({code})"])
        keyboard.append(["◀️ отмена"])
        await update.message.reply_text("Выберите группу для переименования:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        context.user_data['awaiting_rename_selection'] = True
        return GROUP_MANAGEMENT_MENU
    elif context.user_data.get('awaiting_rename_selection') and text.startswith("✏️ "):
        parts = text.split("(")
        if len(parts) > 1:
            code = parts[1].rstrip(")")
            group = context.user_data.get('managed_groups', {}).get(code)
            if group:
                context.user_data['group_to_rename'] = {'code': code, 'group_id': group['group_id'], 'name': group['name']}
                await update.message.reply_text(f"Введите новое название для группы '{group['name']}':", reply_markup=get_back_button())
                return AWAITING_NEW_GROUP_NAME
        await update.message.reply_text("Ошибка при выборе группы. Попробуйте снова.", reply_markup=get_group_management_keyboard())
        context.user_data['awaiting_rename_selection'] = False
        return GROUP_MANAGEMENT_MENU
    elif text == "👞 исключить пользователя":
        if 'managed_groups' not in context.user_data or not context.user_data['managed_groups']:
            await update.message.reply_text("У вас нет групп под управлением.", reply_markup=get_group_management_keyboard())
            return GROUP_MANAGEMENT_MENU
        keyboard = []
        for code, group in context.user_data['managed_groups'].items():
            keyboard.append([f"👞 {group['name']} ({code})"])
        keyboard.append(["◀️ отмена"])
        await update.message.reply_text("Выберите группу для исключения пользователя:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        context.user_data['awaiting_kick_selection'] = True
        return GROUP_MANAGEMENT_MENU
    elif context.user_data.get('awaiting_kick_selection') and text.startswith("👞 "):
        parts = text.split("(")
        if len(parts) > 1:
            code = parts[1].rstrip(")")
            group = context.user_data.get('managed_groups', {}).get(code)
            if group:
                members = get_group_members(group['group_id'])
                if not members or len(members) <= 1:
                    await update.message.reply_text("В группе нет других участников кроме вас.", reply_markup=get_group_management_keyboard())
                    context.user_data['awaiting_kick_selection'] = False
                    return GROUP_MANAGEMENT_MENU
                context.user_data['group_to_kick_from'] = {'group_id': group['group_id'], 'name': group['name']}
                message = f"Участники группы '{group['name']}':\n\n"
                for member in members:
                    if member['username'] != username:
                        message += f"@{member['username']}\n"
                message += "\nВведите username для исключения (например, username или @username):"
                await update.message.reply_text(message, reply_markup=get_back_button())
                return AWAITING_USER_TO_KICK
        await update.message.reply_text("Ошибка при выборе группы. Попробуйте снова.", reply_markup=get_group_management_keyboard())
        context.user_data['awaiting_kick_selection'] = False
        return GROUP_MANAGEMENT_MENU
    return GROUP_MANAGEMENT_MENU

async def new_group_name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    if new_name.lower() == "◀️ отмена":
        await update.message.reply_text("Переименование отменено.", reply_markup=get_group_management_keyboard())
        context.user_data['awaiting_rename_selection'] = False
        return GROUP_MANAGEMENT_MENU
    group_info = context.user_data.get('group_to_rename')
    if not group_info:
        await update.message.reply_text("Ошибка. Повторите выбор группы.", reply_markup=get_group_management_keyboard())
        return GROUP_MANAGEMENT_MENU
    rename_group(group_info['group_id'], new_name)
    if 'managed_groups' in context.user_data and group_info['code'] in context.user_data['managed_groups']:
        context.user_data['managed_groups'][group_info['code']]['name'] = new_name
    await update.message.reply_text(f"Группа переименована с '{group_info['name']}' на '{new_name}'.", reply_markup=get_group_management_keyboard())
    context.user_data['awaiting_rename_selection'] = False
    return GROUP_MANAGEMENT_MENU

async def user_to_kick_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kick_username = update.message.text.strip()
    if kick_username.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_group_management_keyboard())
        context.user_data['awaiting_kick_selection'] = False
        return GROUP_MANAGEMENT_MENU
    if kick_username.startswith('@'):
        kick_username = kick_username[1:]
    group_info = context.user_data.get('group_to_kick_from')
    if not group_info:
        await update.message.reply_text("Ошибка. Повторите выбор группы.", reply_markup=get_group_management_keyboard())
        return GROUP_MANAGEMENT_MENU
    if kick_username == update.effective_user.username:
        await update.message.reply_text("Вы не можете исключить себя. Для выхода используйте соответствующую опцию.", reply_markup=get_group_management_keyboard())
        return GROUP_MANAGEMENT_MENU
    success = kick_from_group(group_info['group_id'], kick_username)
    if success:
        await update.message.reply_text(f"Пользователь @{kick_username} исключён из группы '{group_info['name']}'.", reply_markup=get_group_management_keyboard())
    else:
        await update.message.reply_text(f"Пользователь @{kick_username} не найден в группе '{group_info['name']}'.", reply_markup=get_group_management_keyboard())
    context.user_data['awaiting_kick_selection'] = False
    return GROUP_MANAGEMENT_MENU

async def alert_hours_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user
    username = user.username
    if text.lower() == "◀️ отмена":
        await update.message.reply_text("Настройка отложенного уведомления отменена.", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU
    try:
        hours = int(text)
        if 0 <= hours <= 72:
            update_alert_settings(username, hours)
            await update.message.reply_text(f"Настройки уведомлений обновлены! Вы будете получать уведомление за {hours} часов до дня рождения.",
                                            reply_markup=get_settings_menu_keyboard())
            return SETTINGS_MENU
        else:
            await update.message.reply_text("Введите число от 0 до 72:", reply_markup=get_back_button())
            return AWAITING_ALERT_HOURS
    except ValueError:
        await update.message.reply_text("Введите корректное число:", reply_markup=get_back_button())
        return AWAITING_ALERT_HOURS

async def send_birthday_alerts(context: ContextTypes.DEFAULT_TYPE):
    now_system = datetime.datetime.now()
    logger.info(f"Запуск проверки дней рождения (системное время): {now_system}")
    
    now_utc = now_system - timedelta(hours=SYSTEM_TIMEZONE_OFFSET)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT username, birthday, timezone FROM users WHERE birthday IS NOT NULL")
    all_users = cursor.fetchall()
    
    for person in all_users:
        person_username = person['username']
        birthday_str = person['birthday']
        person_timezone = person['timezone']
        
        if not birthday_str or len(birthday_str) < 10:
            continue
        
        try:
            birthday_day, birthday_month, birthday_year = birthday_str.split('-')
            birthday_day = int(birthday_day)
            birthday_month = int(birthday_month)
            birthday_year = int(birthday_year)
        except (ValueError, IndexError):
            logger.error(f"Некорректный формат даты рождения для @{person_username}: {birthday_str}")
            continue
        
        person_now = now_system + timedelta(hours=person_timezone - SYSTEM_TIMEZONE_OFFSET)
        current_year = person_now.year
        
        try:
            birthday_date_this_year = datetime.datetime(current_year, birthday_month, birthday_day, 0, 0, 0)
            person_birthday = birthday_date_this_year + timedelta(hours=person_timezone - SYSTEM_TIMEZONE_OFFSET)
            
            if person_birthday < person_now:
                birthday_date_next_year = datetime.datetime(current_year + 1, birthday_month, birthday_day, 0, 0, 0) 
                person_birthday = birthday_date_next_year + timedelta(hours=person_timezone - SYSTEM_TIMEZONE_OFFSET)
                
            next_birthday = person_birthday
            
            # Calculate upcoming age
            upcoming_birthday_date = datetime.date(next_birthday.year, birthday_month, birthday_day)
            upcoming_age = upcoming_birthday_date.year - int(birthday_year)
            
        except ValueError:
            logger.error(f"Ошибка при вычислении даты дня рождения для @{person_username}: {birthday_str}")
            continue
        
        followers = get_followers(person_username)
        for follower_username, alert_hours in followers.items():
            try:
                cursor.execute("""
                    SELECT t.chat_id, u.timezone 
                    FROM telegram_ids t 
                    JOIN users u ON t.username = u.username 
                    WHERE t.username = ?
                """, (follower_username,))
                follower_info = cursor.fetchone()
                
                if not follower_info:
                    logger.warning(f"Не найден chat_id или часовой пояс для подписчика @{follower_username}")
                    continue
                
                chat_id = follower_info['chat_id']
                follower_timezone = follower_info['timezone']
                
                follower_now = now_system + timedelta(hours=follower_timezone - SYSTEM_TIMEZONE_OFFSET)
                hours_until_birthday = (next_birthday - follower_now).total_seconds() / 3600
                
                should_notify = alert_hours - 0.5 <= hours_until_birthday <= alert_hours + 0.5
                
                logger.info(f"@{person_username} ДР через {hours_until_birthday:.1f}ч, " +
                          f"уведомление за {alert_hours}ч для @{follower_username}, " +
                          f"отправка: {'ДА' if should_notify else 'НЕТ'}")
                
                if should_notify:
                    days_until_birthday = int(hours_until_birthday / 24)
                    remaining_hours = int(hours_until_birthday % 24)
                    
                    # Include age in notification
                    age_info = f" (исполнится {upcoming_age})"
                    
                    if alert_hours == 0:
                        message = f"🎂 Сегодня день рождения у пользователя @{person_username}{age_info}! 🎉"
                    else:
                        hours_word = format_hours_word(alert_hours)
                        
                        if days_until_birthday == 0:
                            if remaining_hours == 0:
                                message = f"🎂 Сегодня день рождения у пользователя @{person_username}{age_info}! 🎉"
                            else:
                                message = f"🎂 Сегодня через {remaining_hours} {format_hours_word(remaining_hours)} день рождения у пользователя @{person_username}{age_info}! 🎉"
                        elif days_until_birthday == 1:
                            message = f"🎂 Завтра день рождения у пользователя @{person_username}{age_info}! 🎉"
                        else:
                            days_word = format_days_word_only(days_until_birthday)
                            message = f"🎂 Через {days_until_birthday} {days_word} день рождения у пользователя @{person_username}{age_info}! 🎉"
                    
                    logger.info(f"Отправка уведомления для @{follower_username} о ДР @{person_username}")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message
                    )
                
            except Exception as e:
                logger.error(f"Ошибка при проверке уведомления: {str(e)}", exc_info=True)
    
    conn.close()
    logger.info("Проверка дней рождения завершена")

async def send_delayed_birthday_alert(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data['chat_id']
    person_username = job_data['person_username']
    alert_hours = job_data['alert_hours']
    upcoming_age = job_data.get('upcoming_age')
    
    try:
        hours_word = "час" if alert_hours == 1 else ("часа" if 2 <= alert_hours % 10 <= 4 and (alert_hours % 100 < 10 or alert_hours % 100 > 20) else "часов")
        
        age_info = f" (исполнится {upcoming_age})" if upcoming_age else ""
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎂 Через {alert_hours} {hours_word} день рождения у пользователя @{person_username}{age_info}! 🎉"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке отложенного уведомления: {str(e)}", exc_info=True)

# debug force checking for birthdays
async def force_check_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно запускает проверку дней рождения"""
    user = update.effective_user
    username = user.username
    
    await update.message.reply_text("Запускаю проверку дней рождения...")
    
    try:
        await send_birthday_alerts(context)
        
        await update.message.reply_text("Проверка дней рождения выполнена! Отложенные уведомления придут через 30 секунд.",
                                        reply_markup=get_main_menu_keyboard())
        
    except Exception as e:
        logger.error(f"Ошибка при проверке дней рождения: {str(e)}", exc_info=True)
        await update.message.reply_text("Произошла ошибка при проверке дней рождения.",
                                        reply_markup=get_main_menu_keyboard())
    
    return MAIN_MENU

# -------------------------------
# Settings handlers

async def handle_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Изменить уведомления":
        await update.message.reply_text("Введите за сколько часов вы желаете получать оповещения о дне рождения (0-72):", reply_markup=get_back_button())
        return AWAITING_ALERT_HOURS
    elif text == "Изменить дату рождения":
        await update.message.reply_text("Введите новую дату рождения в формате ДД-ММ-ГГГГ (например, 07-02-2002):", reply_markup=get_back_button())
        return SETTINGS_BIRTHDAY
    elif text == "Изменить часовой пояс":
        username = update.effective_user.username
        current_tz = get_user_timezone(username)
        sign = "+" if current_tz >= 0 else ""
        tz_info = f"Ваш текущий часовой пояс: GMT{sign}{current_tz}"
        await update.message.reply_text(
            f"{tz_info}\n\nВведите новый часовой пояс (от -12 до +14, например: 3 для Москвы, 2 для Киева, 8 для Пекина):", 
            reply_markup=get_back_button()
        )
        return SETTINGS_TIMEZONE
    elif text == "◀️ назад":
        await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())
        return MAIN_MENU
    else:
        await update.message.reply_text("Выберите настройку:", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU

async def settings_birthday_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    birthday = update.message.text.strip()
    username = user.username
    if birthday.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU
    try:
        datetime.datetime.strptime(birthday, "%d-%m-%Y")
        update_user_birthday_db(username, birthday)
        await update.message.reply_text(f"День рождения обновлён: {birthday}.", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU
    except ValueError:
        await update.message.reply_text("Неверный формат даты. Введите дату в формате ДД-ММ-ГГГГ:", reply_markup=get_back_button())
        return SETTINGS_BIRTHDAY

async def settings_timezone_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    timezone_str = update.message.text.strip()
    username = user.username
    
    if timezone_str.lower() == "◀️ отмена":
        await update.message.reply_text("Операция отменена.", reply_markup=get_settings_menu_keyboard())
        return SETTINGS_MENU
    
    try:
        timezone = int(timezone_str)
        if -12 <= timezone <= 14:
            update_timezone_settings(username, timezone)
            sign = "+" if timezone >= 0 else ""
            await update.message.reply_text(
                f"Часовой пояс обновлен: GMT{sign}{timezone}.", 
                reply_markup=get_settings_menu_keyboard()
            )
            return SETTINGS_MENU
        else:
            await update.message.reply_text(
                "Неверное значение. Введите число от -12 до +14:", 
                reply_markup=get_back_button()
            )
            return SETTINGS_TIMEZONE
    except ValueError:
        await update.message.reply_text(
            "Введите целое число от -12 до +14:", 
            reply_markup=get_back_button()
        )
        return SETTINGS_TIMEZONE

# -------------------------------
# Group participants handlers

async def handle_group_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    username = update.effective_user.username
    user_timezone = get_user_timezone(username)
    
    if text == "◀️ назад":
        await update.message.reply_text("Меню групп:", reply_markup=get_groups_menu_keyboard())
        return GROUPS_MENU
        
    if text.startswith("📋 "):
        parts = text.split("(")
        if len(parts) > 1:
            code = parts[1].rstrip(")")
            group = context.user_data.get('participant_groups', {}).get(code)
            
            if group:
                members = get_group_members(group['group_id'])
                today = datetime.date.today()
                
                def days_until(bday_str):
                    try:
                        bday = datetime.datetime.strptime(bday_str, "%d-%m-%Y").date()
                        next_bday = bday.replace(year=today.year)
                        if next_bday < today:
                            next_bday = next_bday.replace(year=today.year + 1)
                        return (next_bday - today).days
                    except Exception:
                        return float('inf')
                        
                members_sorted = sorted(members, key=lambda x: days_until(x['birthday']) if x['birthday'] else float('inf'))
                msg = f"Участники группы '{group['name']}':\n\n"
                
                for member in members_sorted:
                    member_username = member['username']
                    owner_mark = "👑" if member_username == group['creator_username'] else ""
                    
                    # Calculate days until birthday
                    days = days_until(member['birthday'])
                    days_info = f" - {format_days_word(days)}" if member['birthday'] and days != float('inf') else ""
                    
                    # Calculate upcoming age
                    upcoming_age = calculate_upcoming_age(member['birthday'])
                    age_info = f" ({upcoming_age} лет)" if upcoming_age else ""
                    
                    # Get timezone info
                    member_timezone = get_user_timezone(member_username)
                    timezone_info = format_timezone_difference(user_timezone, member_timezone) if member_timezone != user_timezone else ""
                    
                    msg += f"@{member_username}{timezone_info} {owner_mark} — Дата: {member['birthday']}{age_info}{days_info}\n"
                    
                await update.message.reply_text(msg)
                
                groups = get_user_groups(username)
                context.user_data['participant_groups'] = {g['code']: dict(g) for g in groups}
                keyboard = []
                for code, g in context.user_data['participant_groups'].items():
                    keyboard.append([f"📋 {g['name']} ({code})"])
                keyboard.append(["◀️ назад"])
                await update.message.reply_text("Выберите группу для просмотра списка участников:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
                return GROUP_PARTICIPANTS_MENU
                
    await update.message.reply_text("Пожалуйста, выберите группу из списка.", reply_markup=get_groups_menu_keyboard())
    return GROUP_PARTICIPANTS_MENU

# -------------------------------
# Cancel handler

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.", reply_markup=get_main_menu_keyboard())
    return MAIN_MENU

# -------------------------------
# Main function with increased timeouts

def main():
    init_db()
    
    # Увеличиваем тайм-ауты
    application = Application.builder()\
        .token("INSERT_YOUR_TOKEN_HERE")\
        .read_timeout(30)\
        .write_timeout(30)\
        .build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("force_check", force_check_birthdays)
        ],
        states={
            AWAITING_REGISTRATION_BIRTHDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, registration_birthday_entered)],
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu)],
            AWAITING_FRIEND_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, friend_username_entered)],
            AWAITING_FRIEND_BIRTHDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, friend_birthday_entered)],
            AWAITING_FRIEND_TO_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, friend_to_delete_entered)],
            GROUPS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_groups_menu)],
            AWAITING_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_name_entered)],
            AWAITING_GROUP_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_key_entered)],
            AWAITING_GROUP_TO_LEAVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_to_leave_entered)],
            GROUP_MANAGEMENT_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_management)],
            AWAITING_NEW_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_group_name_entered)],
            AWAITING_USER_TO_KICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_to_kick_entered)],
            AWAITING_ALERT_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_hours_entered)],
            GROUP_PARTICIPANTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_participants)],
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_settings_menu)],
            SETTINGS_BIRTHDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_birthday_entered)],
            SETTINGS_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_timezone_entered)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("force_check", force_check_birthdays))
    
    job_queue = application.job_queue
    
    now = datetime.datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    seconds_until_next_hour = (next_hour - now).total_seconds()
    
    logger.info(f"Текущее время: {now.strftime('%H:%M:%S')}")
    logger.info(f"Следующий полный час: {next_hour.strftime('%H:%M:%S')}")
    logger.info(f"Секунд до следующего часа: {seconds_until_next_hour}")
    
    job_queue.run_repeating(
        send_birthday_alerts, 
        interval=3600,
        first=seconds_until_next_hour,
        name="hourly_birthday_check"
    )
    
    logger.info(f"Проверка дней рождения запланирована на {next_hour.strftime('%H:%M:%S')} и далее каждый час")
    
    logger.info("Запуск бота...")
    application.run_polling()
    logger.info("Бот успешно запущен")

if __name__ == '__main__':
    main()
