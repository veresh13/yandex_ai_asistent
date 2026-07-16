import os
import logging
import datetime
import time
import re
import sys
import json
import hashlib
from caldav import DAVClient
from icalendar import Calendar, Event
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from telegram_bot_calendar import WMonthTelegramCalendar
import pytz

# ============================================================
#  НАСТРОЙКИ — ЧИТАЮТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME")
CALDAV_APP_PASSWORD = os.environ.get("CALDAV_APP_PASSWORD")
CALDAV_URL = "https://caldav.yandex.ru/calendars/retail.4.32%40vkusvill.ru/events-31428694/"

if not all([TELEGRAM_TOKEN, CALDAV_USERNAME, CALDAV_APP_PASSWORD]):
    raise ValueError("Не заданы переменные окружения: TELEGRAM_TOKEN, CALDAV_USERNAME, CALDAV_APP_PASSWORD")

TIMEZONE = pytz.timezone('Europe/Moscow')
STATE_FILE = 'calendar_state.json'
CONTACTS_FILE = 'contacts.json'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    sys.stdout.flush()

SELECTING_DATE, SELECTING_TIME, SELECTING_DURATION, SELECTING_TITLE, SELECTING_DESCRIPTION, SELECTING_ATTENDEES = range(6)

# ============================================================
#  ГЛАВНОЕ МЕНЮ
# ============================================================

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📅 Найти слот", callback_data="menu_find")],
        [InlineKeyboardButton("➕ Добавить контакт", callback_data="menu_add")],
        [InlineKeyboardButton("📇 Контакты", callback_data="menu_contacts")],
        [InlineKeyboardButton("❓ Помощь", callback_data="menu_help")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_menu(chat_id, context):
    await context.bot.send_message(
        chat_id=chat_id,
        text="🏠 *Главное меню*\nВыбери действие:",
        reply_markup=main_menu_keyboard(),
        parse_mode='Markdown'
    )

# ============================================================
#  РАБОТА С АДРЕСНОЙ КНИГОЙ
# ============================================================

def load_contacts():
    if os.path.exists(CONTACTS_FILE):
        try:
            with open(CONTACTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_contacts(contacts):
    with open(CONTACTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(contacts, f, indent=2, ensure_ascii=False)

def resolve_attendee_list(input_str, contacts):
    parts = [p.strip() for p in input_str.split(',') if p.strip()]
    emails = []
    for part in parts:
        if part in contacts:
            emails.append(contacts[part])
        else:
            if '@' in part:
                emails.append(part)
    return emails

def get_display_name(email, contacts):
    for name, e in contacts.items():
        if e == email:
            return name
    return email

def format_attendees(attendees, contacts):
    if not attendees:
        return ''
    names = [get_display_name(email, contacts) for email in attendees]
    return ', '.join(names)

# ============================================================
#  РАБОТА С CALDAV
# ============================================================

def get_calendars():
    try:
        log("Подключение к CalDAV...")
        client = DAVClient(url=CALDAV_URL, username=CALDAV_USERNAME, password=CALDAV_APP_PASSWORD)
        principal = client.principal()
        calendars = principal.calendars()
        log(f"Найдено {len(calendars)} календарей.")
        return calendars
    except Exception as e:
        logger.error(f"Ошибка подключения к CalDAV: {e}")
        log(f"ОШИБКА: {e}")
        return []

def get_events_for_day(calendars, date):
    events_list = []
    local_tz = TIMEZONE
    start_dt = local_tz.localize(datetime.datetime.combine(date, datetime.time(0, 0, 0)))
    end_dt = local_tz.localize(datetime.datetime.combine(date, datetime.time(23, 59, 59)))
    start_utc = start_dt.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end_dt.astimezone(pytz.UTC).replace(tzinfo=None)
    for cal in calendars:
        try:
            events = cal.date_search(start=start_utc, end=end_utc, expand=True)
            for event_data in events:
                try:
                    cal_obj = Calendar.from_ical(event_data.data)
                    for component in cal_obj.walk():
                        if component.name == "VEVENT":
                            dtstart = component.get('dtstart').dt
                            dtend = component.get('dtend').dt
                            uid = str(component.get('uid', ''))
                            if isinstance(dtstart, datetime.datetime) and isinstance(dtend, datetime.datetime):
                                if dtstart.tzinfo is not None:
                                    dtstart = dtstart.astimezone(local_tz)
                                else:
                                    dtstart = local_tz.localize(dtstart)
                                if dtend.tzinfo is not None:
                                    dtend = dtend.astimezone(local_tz)
                                else:
                                    dtend = local_tz.localize(dtend)
                                summary = component.get('summary', 'Без названия')
                                description = component.get('description', '')
                                attendees = []
                                for attendee in component.get('attendee', []):
                                    if isinstance(attendee, str) and attendee.startswith('mailto:'):
                                        attendees.append(attendee[7:])
                                events_list.append({
                                    'uid': uid,
                                    'summary': str(summary),
                                    'description': str(description) if description else '',
                                    'start': dtstart,
                                    'end': dtend,
                                    'attendees': attendees
                                })
                except Exception as e:
                    logger.error(f"Ошибка парсинга события: {e}")
        except Exception as e:
            logger.error(f"Ошибка получения событий из календаря: {e}")
    events_list.sort(key=lambda x: x['start'])
    return events_list

def find_free_slots(calendars, start_date, end_date, slot_duration=30):
    if not calendars:
        return []
    log(f"Поиск слотов с {start_date} по {end_date}...")
    local_tz = TIMEZONE
    start_local = local_tz.localize(start_date) if start_date.tzinfo is None else start_date
    end_local = local_tz.localize(end_date) if end_date.tzinfo is None else end_date
    start_utc = start_local.astimezone(pytz.UTC).replace(tzinfo=None)
    end_utc = end_local.astimezone(pytz.UTC).replace(tzinfo=None)
    all_events = []
    for idx, cal in enumerate(calendars):
        log(f"Запрос событий из календаря #{idx+1}...")
        try:
            events = cal.date_search(start=start_utc, end=end_utc, expand=True)
            all_events.extend(events)
            log(f"Получено {len(events)} событий.")
        except Exception as e:
            logger.error(f"Ошибка получения событий: {e}")
    busy_intervals = []
    for event_data in all_events:
        try:
            cal = Calendar.from_ical(event_data.data)
            for component in cal.walk():
                if component.name == "VEVENT":
                    dtstart = component.get('dtstart').dt
                    dtend = component.get('dtend').dt
                    if isinstance(dtstart, datetime.datetime) and isinstance(dtend, datetime.datetime):
                        if dtstart.tzinfo is not None:
                            dtstart = dtstart.astimezone(pytz.UTC).replace(tzinfo=None)
                        if dtend.tzinfo is not None:
                            dtend = dtend.astimezone(pytz.UTC).replace(tzinfo=None)
                        busy_intervals.append((dtstart, dtend))
        except Exception as e:
            logger.error(f"Ошибка парсинга события: {e}")
    busy_intervals.sort()
    free_slots_utc = []
    current = start_utc
    delta = datetime.timedelta(minutes=slot_duration)
    while current + delta <= end_utc:
        slot_end = current + delta
        is_free = True
        for b_start, b_end in busy_intervals:
            if not (slot_end <= b_start or current >= b_end):
                is_free = False
                break
        if is_free:
            free_slots_utc.append(current)
        current += delta
    free_slots_local = []
    for slot_utc in free_slots_utc:
        slot_aware = pytz.UTC.localize(slot_utc)
        slot_local = slot_aware.astimezone(local_tz)
        free_slots_local.append(slot_local.replace(tzinfo=None))
    return free_slots_local

def create_event(calendar, summary, start_time, end_time, attendees=None, description=""):
    try:
        local_tz = TIMEZONE
        start_local = local_tz.localize(start_time) if start_time.tzinfo is None else start_time
        end_local = local_tz.localize(end_time) if end_time.tzinfo is None else end_time
        start_utc = start_local.astimezone(pytz.UTC)
        end_utc = end_local.astimezone(pytz.UTC)
        event = Event()
        event.add('summary', summary)
        event.add('dtstart', start_utc)
        event.add('dtend', end_utc)
        if description:
            event.add('description', description)
        event.add('organizer', f'mailto:{CALDAV_USERNAME}')
        if attendees:
            for email in attendees:
                event.add('attendee', f'mailto:{email}', parameters={'partstat': 'NEEDS-ACTION'})
        calendar.save_event(event.to_ical())
        return True
    except Exception as e:
        logger.error(f"Ошибка создания события: {e}")
        return False

# ============================================================
#  ОТСЛЕЖИВАНИЕ ИЗМЕНЕНИЙ
# ============================================================

def event_hash(event):
    data = {
        'summary': event['summary'],
        'description': event['description'],
        'start': event['start'].isoformat(),
        'end': event['end'].isoformat(),
        'attendees': sorted(event['attendees'])
    }
    json_str = json.dumps(data, sort_keys=True)
    return hashlib.md5(json_str.encode()).hexdigest()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

async def check_calendar_changes(context: ContextTypes.DEFAULT_TYPE):
    users = context.bot_data.get('users', set())
    if not users:
        return
    calendars = get_calendars()
    if not calendars:
        return
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    events_today = get_events_for_day(calendars, today)
    events_tomorrow = get_events_for_day(calendars, tomorrow)
    all_events = events_today + events_tomorrow
    current_events = {ev['uid']: ev for ev in all_events if ev['uid']}
    state = load_state()
    saved_events = state.get('events', {})
    changes = []
    for uid, ev in current_events.items():
        if uid not in saved_events:
            changes.append(('new', ev))
        else:
            old_hash = saved_events[uid].get('hash')
            new_hash = event_hash(ev)
            if old_hash != new_hash:
                changes.append(('changed', ev, saved_events[uid]))
    for uid, old_ev in saved_events.items():
        if uid not in current_events:
            changes.append(('deleted', old_ev))
    if changes:
        contacts = load_contacts()
        for chat_id in users:
            await send_change_notifications(chat_id, changes, context, contacts)
    new_state = {}
    for uid, ev in current_events.items():
        new_state[uid] = {
            'hash': event_hash(ev),
            'summary': ev['summary'],
            'start': ev['start'].isoformat(),
            'end': ev['end'].isoformat(),
            'description': ev['description'],
            'attendees': ev['attendees']
        }
    save_state({'events': new_state})

async def send_change_notifications(chat_id, changes, context, contacts):
    messages = []
    for change in changes:
        if change[0] == 'new':
            ev = change[1]
            attendees_display = format_attendees(ev['attendees'], contacts)
            msg = f"🆕 *Новая встреча*\n{ev['summary']}\n🕒 {ev['start'].strftime('%H:%M')} – {ev['end'].strftime('%H:%M')}"
            if ev['description']:
                msg += f"\n📝 {ev['description']}"
            if attendees_display:
                msg += f"\n👥 Участники: {attendees_display}"
            messages.append(msg)
        elif change[0] == 'deleted':
            ev = change[1]
            attendees_display = format_attendees(ev['attendees'], contacts)
            msg = f"❌ *Отменена встреча*\n{ev['summary']} (была на {ev['start'].strftime('%d.%m %H:%M')})"
            if attendees_display:
                msg += f"\n👥 Участники: {attendees_display}"
            messages.append(msg)
        elif change[0] == 'changed':
            ev_new = change[1]
            ev_old = change[2]
            msg = f"🔄 *Изменена встреча*\n{ev_new['summary']}"
            old_start = datetime.datetime.fromisoformat(ev_old['start'])
            new_start = ev_new['start']
            old_end = datetime.datetime.fromisoformat(ev_old['end'])
            new_end = ev_new['end']
            if old_start != new_start or old_end != new_end:
                msg += f"\n⏰ Время: было {old_start.strftime('%H:%M')}–{old_end.strftime('%H:%M')} → стало {new_start.strftime('%H:%M')}–{new_end.strftime('%H:%M')}"
            if ev_old['description'] != ev_new['description']:
                msg += f"\n📝 Описание: {ev_new['description']}"
            if set(ev_old['attendees']) != set(ev_new['attendees']):
                attendees_display = format_attendees(ev_new['attendees'], contacts)
                if attendees_display:
                    msg += f"\n👥 Участники: {attendees_display}"
            messages.append(msg)
    if messages:
        try:
            await context.bot.send_message(chat_id=chat_id, text="\n\n".join(messages), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось отправить уведомления {chat_id}: {e}")

# ============================================================
#  ОБРАБОТЧИКИ МЕНЮ
# ============================================================

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "menu_add":
        await query.edit_message_text(
            "➕ *Добавление контакта*\n\n"
            "Введи команду вручную:\n"
            "`/addcontact Имя email`\n\n"
            "Например: `/addcontact Иванов Иван ivan@mail.ru`",
            parse_mode='Markdown'
        )
        await send_menu(chat_id, context)
    elif data == "menu_contacts":
        await show_contacts(chat_id, context)
    elif data == "menu_help":
        await query.edit_message_text(
            "🤖 *Помощь*\n\n"
            "Я помогаю управлять календарём.\n"
            "Доступные действия:\n"
            "• 📅 Найти слот и создать встречу\n"
            "• ➕ Добавить контакт в адресную книгу\n"
            "• 📇 Посмотреть контакты\n\n"
            "Контакты хранятся в файле `contacts.json`.",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard()
        )

# ============================================================
#  ОБЩИЕ ФУНКЦИИ
# ============================================================

async def start_find(chat_id, context):
    calendar, step = WMonthTelegramCalendar().build()
    await context.bot.send_message(
        chat_id=chat_id,
        text="Выбери месяц и день:",
        reply_markup=calendar
    )

async def show_contacts(chat_id, context):
    contacts = load_contacts()
    if not contacts:
        text = "📭 Адресная книга пуста."
    else:
        lines = ["📇 *Адресная книга*:"]
        for name, email in contacts.items():
            lines.append(f"• {name} → {email}")
        text = "\n".join(lines)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown', reply_markup=main_menu_keyboard())

# ============================================================
#  ОБРАБОТЧИКИ ДИАЛОГА
# ============================================================

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_find(update.effective_chat.id, context)
    return SELECTING_DATE

async def find_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start_find(query.message.chat_id, context)
    return SELECTING_DATE

async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    result, key, step = WMonthTelegramCalendar().process(query.data)
    chat_id = query.message.chat_id

    if not result and key:
        await query.edit_message_text("Выбери месяц и день:", reply_markup=key)
        return SELECTING_DATE
    elif result:
        context.user_data['selected_date'] = result
        await query.edit_message_text(f"Выбрана дата: {result.strftime('%d.%m.%Y')}\nИщу свободные слоты...")
        calendars = get_calendars()
        if not calendars:
            await query.edit_message_text("Не удалось подключиться к календарю. Проверьте настройки.")
            return ConversationHandler.END
        start_dt = datetime.datetime.combine(result, datetime.time(9, 0, 0))
        end_dt = datetime.datetime.combine(result, datetime.time(18, 0, 0))
        free_slots = find_free_slots(calendars, start_dt, end_dt, slot_duration=30)
        if not free_slots:
            await query.edit_message_text("На этот день нет свободных слотов с 9 до 18.")
            return ConversationHandler.END
        context.user_data['all_free_slots'] = free_slots
        context.user_data['calendars'] = calendars
        keyboard = [[InlineKeyboardButton(slot.strftime("%H:%M"), callback_data=f"time_{slot.isoformat()}")] for slot in free_slots]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выбери время начала встречи (доступны только свободные слоты):", reply_markup=reply_markup)
        return SELECTING_TIME
    else:
        calendar, _ = WMonthTelegramCalendar().build()
        await query.edit_message_text("Выбери месяц и день:", reply_markup=calendar)
        return SELECTING_DATE

async def time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("time_"):
        time_iso = data.replace("time_", "")
        try:
            selected_time = datetime.datetime.fromisoformat(time_iso)
        except:
            await query.edit_message_text("Ошибка формата времени.")
            return ConversationHandler.END
        context.user_data['selected_start'] = selected_time
        keyboard = [
            [InlineKeyboardButton("15 мин", callback_data="dur_15")],
            [InlineKeyboardButton("30 мин", callback_data="dur_30")],
            [InlineKeyboardButton("45 мин", callback_data="dur_45")],
            [InlineKeyboardButton("60 мин", callback_data="dur_60")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"Выбрано время {selected_time.strftime('%H:%M')}. Теперь выбери длительность:", reply_markup=reply_markup)
        return SELECTING_DURATION

async def duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("dur_"):
        duration_minutes = int(data.replace("dur_", ""))
        context.user_data['duration_minutes'] = duration_minutes
        await query.edit_message_text("Введите название встречи (обязательно):")
        return SELECTING_TITLE

async def title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Название не может быть пустым. Введите название:")
        return SELECTING_TITLE
    context.user_data['title'] = title
    await update.message.reply_text(
        "Введите описание встречи (необязательно).\n"
        "Если описание не нужно, отправьте слово 'пропустить'."
    )
    return SELECTING_DESCRIPTION

async def description_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == 'пропустить' or text == '':
        description = ''
    else:
        description = text
    context.user_data['description'] = description
    await update.message.reply_text(
        "Теперь укажи участников (имена или email).\n"
        "Можно использовать имена из адресной книги.\n"
        "Введи их через запятую.\n"
        "Если никого не нужно, отправь слово 'пропустить'."
    )
    return SELECTING_ATTENDEES

async def attendees_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    if text.lower() == 'пропустить' or text == '':
        attendees = []
    else:
        contacts = load_contacts()
        attendees = resolve_attendee_list(text, contacts)
        if not attendees:
            await update.message.reply_text(
                "Не найдено корректных имён или email-адресов. Попробуй ещё раз или отправь 'пропустить'."
            )
            return SELECTING_ATTENDEES
    context.user_data['attendees'] = attendees

    start_time = context.user_data.get('selected_start')
    duration_minutes = context.user_data.get('duration_minutes')
    title = context.user_data.get('title', 'Встреча из Telegram')
    description = context.user_data.get('description', '')
    calendars = context.user_data.get('calendars', [])
    if not start_time or not duration_minutes or not title:
        await update.message.reply_text("Ошибка: не хватает данных. Начни заново /find.")
        return ConversationHandler.END
    end_time = start_time + datetime.timedelta(minutes=duration_minutes)
    if calendars:
        my_calendar = calendars[0]
    else:
        await update.message.reply_text("Не удалось найти ваш календарь.")
        return ConversationHandler.END
    success = create_event(
        my_calendar,
        title,
        start_time,
        end_time,
        attendees=attendees,
        description=description
    )
    if success:
        contacts = load_contacts()
        attendees_display = format_attendees(attendees, contacts)
        msg = f"✅ Встреча '{title}' успешно создана!\nНачало: {start_time.strftime('%d.%m.%Y %H:%M')}\nДлительность: {duration_minutes} мин"
        if description:
            msg += f"\nОписание: {description}"
        if attendees_display:
            msg += f"\n👥 Участники: {attendees_display}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("❌ Не удалось создать встречу. Проверьте логи.")

    await send_menu(chat_id, context)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("Действие отменено.")
    await send_menu(chat_id, context)
    return ConversationHandler.END

# ============================================================
#  КОМАНДЫ ДЛЯ КОНТАКТОВ
# ============================================================

async def add_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /addcontact Имя email")
        return
    name = ' '.join(args[:-1])
    email = args[-1]
    if '@' not in email:
        await update.message.reply_text("Укажите корректный email")
        return
    contacts = load_contacts()
    contacts[name] = email
    save_contacts(contacts)
    await update.message.reply_text(f"✅ Контакт '{name}' с email {email} добавлен.")

async def list_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_contacts(update.effective_chat.id, context)

# ============================================================
#  РАСПИСАНИЕ И НАПОМИНАНИЯ
# ============================================================

async def send_daily_schedule(context: ContextTypes.DEFAULT_TYPE):
    users = context.bot_data.get('users', set())
    if not users:
        return
    calendars = get_calendars()
    if not calendars:
        return
    today = datetime.date.today()
    events = get_events_for_day(calendars, today)
    contacts = load_contacts()
    if not events:
        message = "📅 На сегодня встреч нет."
    else:
        lines = ["📅 *Расписание на сегодня:*"]
        for ev in events:
            start_str = ev['start'].strftime("%H:%M")
            end_str = ev['end'].strftime("%H:%M")
            attendees_display = format_attendees(ev['attendees'], contacts)
            line = f"• *{ev['summary']}*  ({start_str}–{end_str})"
            if ev['description']:
                line += f"\n  📝 {ev['description']}"
            if attendees_display:
                line += f"\n  👥 Участники: {attendees_display}"
            lines.append(line)
        message = "\n\n".join(lines)
    for chat_id in users:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не удалось отправить расписание {chat_id}: {e}")

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    log("=== Запущена проверка напоминаний ===")
    users = context.bot_data.get('users', set())
    if not users:
        log("Нет зарегистрированных пользователей. Напоминания не отправляются.")
        return
    if 'sent_reminders' not in context.bot_data:
        context.bot_data['sent_reminders'] = set()
    calendars = get_calendars()
    if not calendars:
        log("Не удалось получить календари для напоминаний.")
        return
    today = datetime.date.today()
    events = get_events_for_day(calendars, today)
    now = datetime.datetime.now(TIMEZONE)
    contacts = load_contacts()
    log(f"=== ПРОВЕРКА НАПОМИНАНИЙ: найдено {len(events)} событий, сейчас {now.strftime('%H:%M:%S')} ===")
    for ev in events:
        start = ev['start']
        if start <= now:
            continue
        delta = start - now
        minutes_left = delta.total_seconds() / 60
        if 29 <= minutes_left <= 31:
            uid = ev.get('uid', '')
            reminder_key = f"{uid}_{start.isoformat()}"
            if reminder_key not in context.bot_data['sent_reminders']:
                attendees_display = format_attendees(ev['attendees'], contacts)
                msg = f"⏰ *Напоминание!*\n\nВстреча *{ev['summary']}* начнётся через 30 минут.\n"
                msg += f"🕒 {start.strftime('%H:%M')} – {ev['end'].strftime('%H:%M')}"
                if ev['description']:
                    msg += f"\n📝 {ev['description']}"
                if attendees_display:
                    msg += f"\n👥 Участники: {attendees_display}"
                for chat_id in users:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                        log(f"✅ Напоминание отправлено {chat_id}")
                        context.bot_data['sent_reminders'].add(reminder_key)
                    except Exception as e:
                        logger.error(f"Не удалось отправить напоминание {chat_id}: {e}")

# ============================================================
#  ЗАПУСК
# ============================================================

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('find', find),
            CallbackQueryHandler(find_callback, pattern="^menu_find$")
        ],
        states={
            SELECTING_DATE: [CallbackQueryHandler(calendar_callback)],
            SELECTING_TIME: [CallbackQueryHandler(time_callback)],
            SELECTING_DURATION: [CallbackQueryHandler(duration_callback)],
            SELECTING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_input)],
            SELECTING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_input)],
            SELECTING_ATTENDEES: [MessageHandler(filters.TEXT & ~filters.COMMAND, attendees_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('addcontact', add_contact))
    application.add_handler(CommandHandler('contacts', list_contacts))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_(add|contacts|help)$"))
    application.add_handler(conv_handler)

    job_queue = application.job_queue
    if job_queue:
        moscow_tz = pytz.timezone('Europe/Moscow')
        job_queue.run_daily(send_daily_schedule, time=datetime.time(hour=9, minute=0, tzinfo=moscow_tz))
        job_queue.run_repeating(send_reminders, interval=60, first=10)
        job_queue.run_repeating(check_calendar_changes, interval=300, first=20)
        log("Планировщик запущен.")
    else:
        log("JobQueue не доступен — уведомления работать не будут")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if 'users' not in context.bot_data:
        context.bot_data['users'] = set()
    context.bot_data['users'].add(chat_id)
    log(f"Пользователь {chat_id} зарегистрирован")
    await send_menu(chat_id, context)

if __name__ == '__main__':
    main()
