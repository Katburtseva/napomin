import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3), name="MSK")
MAX_REMINDER_TEXT_LENGTH = 280
DEFAULT_BOT_TOKEN = "8772042846:AAEpJQXSSVHnQrIZhloxrZKOj2MU847o4YI"
CALLBACK_CANCEL_PREFIX = "cancel:"
REMINDERS_KEY = "reminders"
COUNTER_KEY = "reminder_counter"


@dataclass(slots=True)
class Reminder:
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


def reminders_store(application: Application) -> dict[int, dict[int, Reminder]]:
    return application.bot_data.setdefault(REMINDERS_KEY, {})


def chat_reminders(application: Application, chat_id: int) -> dict[int, Reminder]:
    return reminders_store(application).setdefault(chat_id, {})


def next_reminder_id(application: Application) -> int:
    reminder_id = int(application.bot_data.get(COUNTER_KEY, 0)) + 1
    application.bot_data[COUNTER_KEY] = reminder_id
    return reminder_id


def save_reminder(application: Application, reminder: Reminder) -> None:
    chat_reminders(application, reminder.chat_id)[reminder.reminder_id] = reminder


def remove_reminder(application: Application, chat_id: int, reminder_id: int) -> Reminder | None:
    reminders = reminders_store(application).get(chat_id)
    if not reminders:
        return None

    reminder = reminders.pop(reminder_id, None)
    if not reminders:
        reminders_store(application).pop(chat_id, None)
    return reminder


def get_sorted_reminders(application: Application, chat_id: int) -> list[Reminder]:
    reminders = reminders_store(application).get(chat_id, {}).values()
    return sorted(reminders, key=lambda item: item.remind_at)


def remove_reminder_if_current_task(
    application: Application,
    chat_id: int,
    reminder_id: int,
) -> None:
    reminders = reminders_store(application).get(chat_id)
    if not reminders:
        return

    reminder = reminders.get(reminder_id)
    if reminder is None or reminder.task is not asyncio.current_task():
        return

    reminders.pop(reminder_id, None)
    if not reminders:
        reminders_store(application).pop(chat_id, None)


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


def build_cancel_keyboard(reminders: list[Reminder]) -> InlineKeyboardMarkup | None:
    if not reminders:
        return None

    rows = [
        [
            InlineKeyboardButton(
                text=f"Отменить #{reminder.reminder_id}",
                callback_data=f"{CALLBACK_CANCEL_PREFIX}{reminder.reminder_id}",
            )
        ]
        for reminder in reminders[:10]
    ]
    return InlineKeyboardMarkup(rows)


def parse_time(value: str) -> time | None:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def next_datetime_at(remind_time: time) -> datetime:
    now = datetime.now(MSK)
    remind_at = datetime.combine(now.date(), remind_time, tzinfo=MSK)
    if remind_at <= now:
        remind_at += timedelta(days=1)
    return remind_at


def format_reminder(reminder: Reminder) -> str:
    label = "ежедневно" if reminder.is_daily else "один раз"
    return (
        f"{reminder.reminder_id}. [{label}] "
        f"{reminder.remind_at.strftime('%d.%m %H:%M')} МСК - {reminder.text}"
    )


def parse_reminder_args(args: list[str]) -> tuple[str, str] | None:
    if len(args) < 2:
        return None
    return args[0], " ".join(args[1:]).strip()


def build_webhook_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_safe_secret_token(raw_value: str | None, token: str) -> str:
    candidate = raw_value or f"telegram-bot-{token.split(':', 1)[0]}"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", candidate).strip("-_")
    return (sanitized or "telegram-bot-secret")[:256]


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


async def schedule_delivery(
    reminder_id: int,
    chat_id: int,
    remind_at: datetime,
    text: str,
    is_daily: bool,
    application: Application,
) -> None:
    try:
        delay_seconds = max(0, int((remind_at - datetime.now(MSK)).total_seconds()))
        await asyncio.sleep(delay_seconds)
        await application.bot.send_message(chat_id=chat_id, text=f"Напоминание: {text}")

        if is_daily:
            next_remind_at = remind_at + timedelta(days=1)
            task = application.create_task(
                schedule_delivery(
                    reminder_id=reminder_id,
                    chat_id=chat_id,
                    remind_at=next_remind_at,
                    text=text,
                    is_daily=True,
                    application=application,
                )
            )
            save_reminder(
                application,
                Reminder(
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
        remove_reminder_if_current_task(application, chat_id, reminder_id)


async def create_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    is_daily: bool,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    parsed = parse_reminder_args(context.args)
    if parsed is None:
        example = "/daily 08:00 выпить воду" if is_daily else "/remind 08:30 вынести мусор"
        await update.message.reply_text(f"Нужно указать время и текст.\nПример: {example}")
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

    remind_time = parse_time(time_raw)
    if remind_time is None:
        await update.message.reply_text("Время нужно указать в формате ЧЧ:ММ, например 19:30.")
        return

    remind_at = next_datetime_at(remind_time)
    reminder_id = next_reminder_id(context.application)
    task = context.application.create_task(
        schedule_delivery(
            reminder_id=reminder_id,
            chat_id=update.effective_chat.id,
            remind_at=remind_at,
            text=reminder_text,
            is_daily=is_daily,
            application=context.application,
        )
    )
    save_reminder(
        context.application,
        Reminder(
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
        f"Время: {remind_at.strftime('%H:%M')} МСК ({remind_at.strftime('%d.%m.%Y')}).\n"
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

    reminders = get_sorted_reminders(context.application, update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("Активных напоминаний пока нет.")
        return

    lines = ["Активные напоминания:"] + [format_reminder(reminder) for reminder in reminders]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=build_cancel_keyboard(reminders),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Укажи ID напоминания для отмены.\nПример: /cancel 3")
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID напоминания должен быть числом.")
        return

    reminder = remove_reminder(context.application, update.effective_chat.id, reminder_id)
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

    reminder = remove_reminder(context.application, query.message.chat.id, reminder_id)
    if reminder is None:
        await query.answer("Напоминание уже удалено", show_alert=True)
        return

    reminder.task.cancel()
    await query.edit_message_text(f"Напоминание {reminder_id} отменено.\nТекст: {reminder.text}")


async def cancel_all_reminders(application: Application) -> None:
    tasks = [
        reminder.task
        for reminders in reminders_store(application).values()
        for reminder in reminders.values()
    ]
    reminders_store(application).clear()

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def shutdown(application: Application) -> None:
    await cancel_all_reminders(application)
    await application.updater.stop()
    await application.stop()
    await application.shutdown()


async def run_application(
    application: Application,
    *,
    webhook_base_url: str | None,
    port: int,
    webhook_path: str,
    secret_token: str | None,
) -> None:
    stop_event = create_stop_event()
    await application.initialize()

    if webhook_base_url:
        webhook_url = build_webhook_url(webhook_base_url, webhook_path)
        logger.info("Bot is running in webhook mode on port %s", port)
        logger.info("Webhook URL: %s", webhook_url)
        await application.updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            drop_pending_updates=True,
        )
    else:
        logger.info("Bot is running in polling mode")
        await application.updater.start_polling(drop_pending_updates=True)

    await application.start()

    try:
        await stop_event.wait()
    finally:
        await shutdown(application)


async def main() -> None:
    token = get_env("TELEGRAM_BOT_TOKEN") or DEFAULT_BOT_TOKEN
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Добавь токен в переменные окружения Render или в код."
        )

    webhook_base_url = get_env("WEBHOOK_URL") or get_env("RENDER_EXTERNAL_URL")
    await run_application(
        build_application(token),
        webhook_base_url=webhook_base_url,
        port=int(get_env("PORT", "10000")),
        webhook_path=get_env("TELEGRAM_WEBHOOK_PATH", "telegram"),
        secret_token=build_safe_secret_token(get_env("TELEGRAM_SECRET_TOKEN"), token),
    )


if __name__ == "__main__":
    asyncio.run(main())
