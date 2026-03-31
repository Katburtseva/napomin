import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3), name="MSK")
MAX_REMINDER_TEXT_LENGTH = 280
REMINDERS_KEY = "reminders"
REMINDER_COUNTER_KEY = "reminder_counter"
DEFAULT_BOT_TOKEN = "8772042846:AAEpJQXSSVHnQrIZhloxrZKOj2MU847o4YI"
CALLBACK_CANCEL_PREFIX = "cancel:"


@dataclass(slots=True)
class ReminderRecord:
    reminder_id: int
    chat_id: int
    text: str
    remind_at: datetime
    is_daily: bool
    task: asyncio.Task[None]


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def build_webhook_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_safe_secret_token(raw_value: str | None, token: str) -> str:
    candidate = raw_value or f"telegram-bot-{token.split(':', 1)[0]}"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", candidate)
    sanitized = sanitized.strip("-_")
    if not sanitized:
        sanitized = "telegram-bot-secret"
    return sanitized[:256]


def get_reminder_store(application: Application) -> dict[int, dict[int, ReminderRecord]]:
    return application.bot_data.setdefault(REMINDERS_KEY, {})


def next_reminder_id(application: Application) -> int:
    current = int(application.bot_data.get(REMINDER_COUNTER_KEY, 0)) + 1
    application.bot_data[REMINDER_COUNTER_KEY] = current
    return current


def register_reminder(application: Application, reminder: ReminderRecord) -> None:
    store = get_reminder_store(application)
    chat_reminders = store.setdefault(reminder.chat_id, {})
    chat_reminders[reminder.reminder_id] = reminder


def pop_reminder(application: Application, chat_id: int, reminder_id: int) -> ReminderRecord | None:
    store = get_reminder_store(application)
    chat_reminders = store.get(chat_id)
    if not chat_reminders:
        return None

    reminder = chat_reminders.pop(reminder_id, None)
    if not chat_reminders:
        store.pop(chat_id, None)
    return reminder


def list_reminders(application: Application, chat_id: int) -> list[ReminderRecord]:
    store = get_reminder_store(application)
    chat_reminders = store.get(chat_id, {})
    return sorted(chat_reminders.values(), key=lambda item: item.remind_at)


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["/remind 19:30 выключить чайник"],
            ["/daily 08:00 выпить воду"],
            ["/list", "/help"],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери команду или введи свою",
    )


def build_cancel_keyboard(reminders: list[ReminderRecord]) -> InlineKeyboardMarkup | None:
    if not reminders:
        return None

    buttons = [
        [
            InlineKeyboardButton(
                text=f"Отменить #{reminder.reminder_id}",
                callback_data=f"{CALLBACK_CANCEL_PREFIX}{reminder.reminder_id}",
            )
        ]
        for reminder in reminders[:10]
    ]
    return InlineKeyboardMarkup(buttons)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("remind", remind))
    application.add_handler(CommandHandler("daily", daily))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=r"^cancel:\d+$"))
    return application


def create_stop_event() -> asyncio.Event:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop_event.set())

    return stop_event


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Привет! Я бот-напоминалка.\n\n"
        "Я помогаю не забывать важное и работаю по московскому времени.\n\n"
        "Что можно сделать:\n"
        "• /remind <ЧЧ:ММ> <текст> - разовое напоминание\n"
        "• /daily <ЧЧ:ММ> <текст> - повтор каждый день\n"
        "• /list - посмотреть свои напоминания\n"
        "• /cancel <id> - отменить по номеру\n\n"
        "Ниже есть быстрые кнопки, можно нажимать их.",
        reply_markup=build_main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Подсказка по командам:\n\n"
        "• /remind 21:15 проверить духовку\n"
        "• /daily 09:00 зарядка\n"
        "• /list\n"
        "• /cancel 3\n\n"
        "После /list я покажу активные напоминания, а если получится, дам кнопки для быстрой отмены.",
        reply_markup=build_main_keyboard(),
    )


async def send_reminder(
    reminder_id: int,
    chat_id: int,
    delay_seconds: int,
    text: str,
    remind_at: datetime,
    is_daily: bool,
    application: Application,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        await application.bot.send_message(chat_id=chat_id, text=f"Напоминание: {text}")
        if is_daily:
            next_remind_at = remind_at + timedelta(days=1)
            next_delay_seconds = max(
                0, int((next_remind_at - datetime.now(MSK)).total_seconds())
            )
            task = application.create_task(
                send_reminder(
                    reminder_id=reminder_id,
                    chat_id=chat_id,
                    delay_seconds=next_delay_seconds,
                    text=text,
                    remind_at=next_remind_at,
                    is_daily=True,
                    application=application,
                )
            )
            register_reminder(
                application,
                ReminderRecord(
                    reminder_id=reminder_id,
                    chat_id=chat_id,
                    text=text,
                    remind_at=next_remind_at,
                    is_daily=True,
                    task=task,
                ),
            )
            return
    except asyncio.CancelledError:
        logger.info("Reminder %s for chat %s was cancelled", reminder_id, chat_id)
        raise
    except Exception:
        logger.exception("Failed to deliver reminder %s for chat %s", reminder_id, chat_id)
    finally:
        pop_reminder(application, chat_id, reminder_id)


def parse_reminder_input(args: list[str]) -> tuple[str, str] | None:
    if len(args) < 2:
        return None
    time_raw = args[0]
    reminder_text = " ".join(args[1:]).strip()
    return time_raw, reminder_text


async def create_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_daily: bool,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    parsed = parse_reminder_input(context.args)
    if parsed is None:
        command_example = "/daily 08:00 выпить воду" if is_daily else "/remind 08:30 вынести мусор"
        await update.message.reply_text(
            "Нужно указать время и текст.\n"
            f"Пример: {command_example}"
        )
        return

    time_raw, reminder_text = parsed

    if not reminder_text:
        await update.message.reply_text("Текст напоминания не должен быть пустым.")
        return

    if len(reminder_text) > MAX_REMINDER_TEXT_LENGTH:
        await update.message.reply_text(
            f"Текст напоминания слишком длинный. Максимум: {MAX_REMINDER_TEXT_LENGTH} символов."
        )
        return

    try:
        remind_time = datetime.strptime(time_raw, "%H:%M").time()
    except ValueError:
        await update.message.reply_text(
            "Время нужно указать в формате ЧЧ:ММ, например 19:30."
        )
        return

    now_msk = datetime.now(MSK)
    remind_at = datetime.combine(now_msk.date(), remind_time, tzinfo=MSK)
    if remind_at <= now_msk:
        remind_at += timedelta(days=1)

    delay_seconds = max(0, int((remind_at - now_msk).total_seconds()))
    reminder_id = next_reminder_id(context.application)
    task = context.application.create_task(
        send_reminder(
            reminder_id=reminder_id,
            chat_id=update.effective_chat.id,
            delay_seconds=delay_seconds,
            text=reminder_text,
            remind_at=remind_at,
            is_daily=is_daily,
            application=context.application,
        )
    )

    register_reminder(
        context.application,
        ReminderRecord(
            reminder_id=reminder_id,
            chat_id=update.effective_chat.id,
            text=reminder_text,
            remind_at=remind_at,
            is_daily=is_daily,
            task=task,
        ),
    )

    reminder_kind = "каждый день" if is_daily else "один раз"
    await update.message.reply_text(
        f"Готово. Напоминание поставлено: {reminder_kind}.\n"
        f"Время: {remind_at.strftime('%H:%M')} МСК "
        f"({remind_at.strftime('%d.%m.%Y')}).\n"
        f"ID: {reminder_id}\n"
        f"Текст: {reminder_text}",
        reply_markup=build_main_keyboard(),
    )


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_reminder(update, context, is_daily=False)


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_reminder(update, context, is_daily=True)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    reminders = list_reminders(context.application, update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("Активных напоминаний пока нет.")
        return

    lines = ["Активные напоминания:"]
    for reminder in reminders:
        label = "ежедневно" if reminder.is_daily else "один раз"
        lines.append(
            f"{reminder.reminder_id}. [{label}] {reminder.remind_at.strftime('%d.%m %H:%M')} МСК - {reminder.text}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=build_cancel_keyboard(reminders),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Укажи ID напоминания для отмены.\n"
            "Пример: /cancel 3"
        )
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID напоминания должен быть числом.")
        return

    reminder = pop_reminder(context.application, update.effective_chat.id, reminder_id)
    if reminder is None:
        await update.message.reply_text(f"Напоминание с ID {reminder_id} не найдено.")
        return

    reminder.task.cancel()
    await update.message.reply_text(
        f"Напоминание {reminder_id} отменено.\nТекст: {reminder.text}",
        reply_markup=build_main_keyboard(),
    )


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None or query.message.chat is None or query.data is None:
        return

    await query.answer()

    try:
        reminder_id = int(query.data.removeprefix(CALLBACK_CANCEL_PREFIX))
    except ValueError:
        await query.answer("Не удалось распознать ID", show_alert=True)
        return

    reminder = pop_reminder(context.application, query.message.chat.id, reminder_id)
    if reminder is None:
        await query.answer("Напоминание уже удалено", show_alert=True)
        return

    reminder.task.cancel()
    await query.edit_message_text(
        f"Напоминание {reminder_id} отменено.\nТекст: {reminder.text}"
    )


async def cancel_all_reminders(application: Application) -> None:
    store = get_reminder_store(application)
    tasks: list[asyncio.Task[None]] = []

    for chat_reminders in store.values():
        for reminder in chat_reminders.values():
            reminder.task.cancel()
            tasks.append(reminder.task)

    store.clear()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_polling(application: Application) -> None:
    logger.info("Bot is running in polling mode")
    stop_event = create_stop_event()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        await stop_event.wait()
    finally:
        await cancel_all_reminders(application)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def run_webhook(
    application: Application,
    port: int,
    webhook_path: str,
    webhook_url: str,
    secret_token: str | None,
) -> None:
    logger.info("Bot is running in webhook mode on port %s", port)
    logger.info("Webhook URL: %s", webhook_url)
    stop_event = create_stop_event()

    await application.initialize()
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        secret_token=secret_token,
        drop_pending_updates=True,
    )

    try:
        await stop_event.wait()
    finally:
        await cancel_all_reminders(application)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def main() -> None:
    token = get_env("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Добавь токен в переменные окружения Render или в код."
        )

    application = build_application(token)

    webhook_base_url = get_env("WEBHOOK_URL") or get_env("RENDER_EXTERNAL_URL")
    if webhook_base_url:
        port = int(get_env("PORT", "10000"))
        webhook_path = get_env("TELEGRAM_WEBHOOK_PATH", "telegram")
        secret_token = build_safe_secret_token(get_env("TELEGRAM_SECRET_TOKEN"), token)
        webhook_url = build_webhook_url(webhook_base_url, webhook_path)
        await run_webhook(application, port, webhook_path, webhook_url, secret_token)
        return

    await run_polling(application)


if __name__ == "__main__":
    asyncio.run(main())
