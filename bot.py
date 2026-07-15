import os
import logging
import datetime
import time
import re
import sys
from caldav import DAVClient
from icalendar import Calendar, Event
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)
from telegram_bot_calendar import DetailedTelegramCalendar
import pytz

# ============================================================
#  НАСТРОЙКИ — считываются из переменных окружения
#  На хостинге (Bothost) создайте переменные:
#    TELEGRAM_TOKEN, CALDAV_USERNAME, CALDAV_APP_PASSWORD
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME")
CALDAV_APP_PASSWORD = os.environ.get("CALDAV_APP_PASSWORD")
CALDAV_URL = "https://caldav.yandex.ru/calendars/retail.4.32%40vkusvill.ru/events-31428694/"   # можно заменить на https://caldav.yandex.team/ при необходимости

# Проверяем, что все необходимые переменные заданы
if not all([TELEGRAM_TOKEN, CALDAV_USERNAME, CALDAV_APP_PASSWORD]):
    raise ValueError("Не все переменные окружения заданы! Проверьте: TELEGRAM_TOKEN, CALDAV_USERNAME, CALDAV_APP_PASSWORD")

# Ваша временная зона (для Москвы — Europe/Moscow)
TIMEZONE = pytz.timezone('Europe/Moscow')

# ============================================================
#  ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    sys.stdout.flush()

# ============================================================
#  СОСТОЯНИЯ ДИАЛОГА
# ============================================================
SELECTING_DATE, SELECTING_TIME, SELECTING_DURATION, SELECTING_TITLE, SELECTING_DESCRIPTION, SELECTING_ATTENDEES = range(6)

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
    """Возвращает список событий на указанную дату."""
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
#  ОБРАБОТЧИКИ КОМАНД
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if 'users' not in context.bot_data:
        context.bot_data['users'] = set()
    context.bot_data['users'].add(chat_id)
    log(f"Пользователь {chat_id} зарегистрирован для уведомлений")

    await update.message.reply_text(
        "Привет! Я помогу найти свободное время в твоём Яндекс.Календаре.\n"
        "Используй команду /find, чтобы начать поиск.\n"
        "Каждое утро в 9:00 я буду присылать твоё расписание на сегодня.\n"
        "За 30 минут до встречи я пришлю напоминание."
    )

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    calendar, step = DetailedTelegramCalendar().build()
    await update.message.reply_text(
        "Выбери дату:",
        reply_markup=calendar
    )
    return SELECTING_DATE

async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    result, key, step = DetailedTelegramCalendar().process(query.data)
    if not result and key:
        await query.edit_message_text(
            "Выбери дату:",
            reply_markup=key
        )
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

        keyboard = []
        for slot in free_slots:
            time_str = slot.strftime("%H:%M")
            keyboard.append([InlineKeyboardButton(time_str, callback_data=f"time_{slot.isoformat()}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Выбери время начала встречи (доступны только свободные слоты):",
            reply_markup=reply_markup
        )
        return SELECTING_TIME

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
        await query.edit_message_text(
            f"Выбрано время {selected_time.strftime('%H:%M')}. Теперь выбери длительность:",
            reply_markup=reply_markup
        )
        return SELECTING_DURATION

async def duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("dur_"):
        duration_minutes = int(data.replace("dur_", ""))
        context.user_data['duration_minutes'] = duration_minutes
        await query.edit_message_text(
            "Введите название встречи (обязательно):"
        )
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
        "Теперь укажи email-адреса коллег, которых хочешь пригласить.\n"
        "Введи их через запятую или пробел.\n"
        "Если никого не нужно, отправь слово 'пропустить'."
    )
    return SELECTING_ATTENDEES

async def attendees_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == 'пропустить' or text == '':
        attendees = []
    else:
        raw = re.split(r'[,\s]+', text)
        attendees = [email.strip() for email in raw if email.strip() and '@' in email]
        if not attendees:
            await update.message.reply_text(
                "Не найдено корректных email-адресов. Попробуй ещё раз или отправь 'пропустить'."
            )
            return SELECTING_ATTENDEES

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
        msg = f"✅ Встреча '{title}' успешно создана!\nНачало: {start_time.strftime('%d.%m.%Y %H:%M')}\nДлительность: {duration_minutes} мин"
        if description:
            msg += f"\nОписание: {description}"
        if attendees:
            msg += f"\nПриглашены: {', '.join(attendees)}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("❌ Не удалось создать встречу. Проверьте логи.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# ============================================================
#  РАСПИСАНИЕ И НАПОМИНАНИЯ
# ============================================================

async def send_daily_schedule(context: ContextTypes.DEFAULT_TYPE):
    users = context.bot_data.get('users', set())
    if not users:
        log("Нет пользователей для расписания.")
        return

    calendars = get_calendars()
    if not calendars:
        log("Не удалось получить календари для расписания.")
        return

    today = datetime.date.today()
    events = get_events_for_day(calendars, today)

    if not events:
        message = "📅 На сегодня встреч нет."
    else:
        lines = ["📅 *Расписание на сегодня:*"]
        for ev in events:
            start_str = ev['start'].strftime("%H:%M")
            end_str = ev['end'].strftime("%H:%M")
            line = f"• *{ev['summary']}*  ({start_str}–{end_str})"
            if ev['description']:
                line += f"\n  📝 {ev['description']}"
            if ev['attendees']:
                line += f"\n  👥 Участники: {', '.join(ev['attendees'])}"
            lines.append(line)
        message = "\n\n".join(lines)

    for chat_id in users:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
            log(f"Расписание отправлено {chat_id}")
        except Exception as e:
            logger.error(f"Не удалось отправить расписание {chat_id}: {e}")

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    users = context.bot_data.get('users', set())
    if not users:
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

    for ev in events:
        start = ev['start']
        if start <= now:
            continue
        delta = start - now
        if 29 * 60 <= delta.total_seconds() <= 31 * 60:
            uid = ev.get('uid', '')
            reminder_key = f"{uid}_{start.isoformat()}"
            if reminder_key not in context.bot_data['sent_reminders']:
                msg = f"⏰ *Напоминание!*\n\nВстреча *{ev['summary']}* начнётся через 30 минут.\n"
                msg += f"🕒 {start.strftime('%H:%M')} – {ev['end'].strftime('%H:%M')}"
                if ev['description']:
                    msg += f"\n📝 {ev['description']}"
                if ev['attendees']:
                    msg += f"\n👥 Участники: {', '.join(ev['attendees'])}"

                for chat_id in users:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                        log(f"Напоминание отправлено {chat_id} для встречи {ev['summary']}")
                        context.bot_data['sent_reminders'].add(reminder_key)
                    except Exception as e:
                        logger.error(f"Не удалось отправить напоминание {chat_id}: {e}")

# ============================================================
#  ЗАПУСК БОТА
# ============================================================

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('find', find)],
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
    application.add_handler(conv_handler)

    job_queue = application.job_queue
    if job_queue:
        moscow_tz = pytz.timezone('Europe/Moscow')
        job_queue.run_daily(
            send_daily_schedule,
            time=datetime.time(hour=9, minute=0, tzinfo=moscow_tz)
        )
        job_queue.run_repeating(send_reminders, interval=60, first=10)
        log("Планировщик запущен: расписание в 9:00, напоминания каждую минуту.")
    else:
        log("JobQueue не доступен")

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()