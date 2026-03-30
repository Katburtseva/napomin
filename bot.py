import asyncio
import logging
import os
import re
import signal
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3), name="MSK")


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


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("remind", remind))
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
        "Команда:\n"
        "/remind <ЧЧ:ММ> <текст>\n\n"
        "Пример:\n"
        "/remind 19:30 выключить чайник\n\n"
        "Время указывается по Москве."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Я умею ставить напоминания на конкретное время по Москве.\n\n"
        "Использование:\n"
        "/remind <ЧЧ:ММ> <текст>\n\n"
        "Пример:\n"
        "/remind 21:15 проверить духовку"
    )


async def send_reminder(
    chat_id: int,
    delay_seconds: int,
    text: str,
    application: Application,
) -> None:
    await asyncio.sleep(delay_seconds)
    await application.bot.send_message(chat_id=chat_id, text=f"Напоминание: {text}")


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Нужно указать время и текст.\n"
            "Пример: /remind 08:30 вынести мусор"
        )
        return

    time_raw = context.args[0]
    reminder_text = " ".join(context.args[1:]).strip()

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

    delay_seconds = int((remind_at - now_msk).total_seconds())

    context.application.create_task(
        send_reminder(
            chat_id=update.effective_chat.id,
            delay_seconds=delay_seconds,
            text=reminder_text,
            application=context.application,
        )
    )

    await update.message.reply_text(
        f"Ок, напомню в {remind_at.strftime('%H:%M')} МСК "
        f"({remind_at.strftime('%d.%m.%Y')}).\n"
        f"Текст: {reminder_text}"
    )


async def run_polling(application: Application) -> None:
    logger.info("Bot is running in polling mode")
    stop_event = create_stop_event()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        await stop_event.wait()
    finally:
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
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def main() -> None:
    token = get_env("TELEGRAM_BOT_TOKEN") or "8772042846:AAEpJQXSSVHnQrIZhloxrZKOj2MU847o4YI"
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
