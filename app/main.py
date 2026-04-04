# app/main.py
from __future__ import annotations

import logging
from typing import Any

from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

from config import BOT_TOKEN, BOT_WORKERS, UPDATE_CHECK_FIRST_DELAY_SECONDS, UPDATE_CHECK_INTERVAL_SECONDS
from routers.callback_router import on_callback
from handlers.user import start_cmd, version_cmd, whoami_cmd, getkey_cmd
from handlers import admin as admin_handlers
from services.alerts import alert_monitor_job
from services.backups import auto_backup_job
from services.traffic_usage import collect_traffic_job
from services.updates import auto_check_job

logging.getLogger("xray").setLevel(logging.INFO)
logging.getLogger("awg").setLevel(logging.INFO)

def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )


def on_error(update: object, context: Any) -> None:
    logging.getLogger("bot").exception("Unhandled error: %s", context.error)


def main() -> None:
    setup_logging()
    if not BOT_TOKEN:
        logging.getLogger("bot").warning("BOT_TOKEN is empty. Set it in app/config.py")

    updater = Updater(BOT_TOKEN, use_context=True, workers=BOT_WORKERS)
    dp = updater.dispatcher

    dp.add_error_handler(on_error)

    updater.job_queue.run_repeating(collect_traffic_job, interval=3600, first=300, name="collect_traffic_job")
    updater.job_queue.run_repeating(auto_backup_job, interval=900, first=600, name="auto_backup_job")
    updater.job_queue.run_repeating(alert_monitor_job, interval=60, first=180, name="alert_monitor_job")
    updater.job_queue.run_repeating(
        auto_check_job,
        interval=UPDATE_CHECK_INTERVAL_SECONDS,
        first=UPDATE_CHECK_FIRST_DELAY_SECONDS,
        name="auto_check_updates_job",
    )

    # user commands
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("whoami", whoami_cmd))
    dp.add_handler(CommandHandler("getkey", getkey_cmd))
    dp.add_handler(CommandHandler("version", version_cmd))

    # admin commands
    dp.add_handler(CommandHandler("add", admin_handlers.add_cmd))
    dp.add_handler(CommandHandler("del", admin_handlers.del_cmd))
    dp.add_handler(CommandHandler("list", admin_handlers.list_cmd))
    dp.add_handler(CommandHandler("servers", admin_handlers.servers_cmd))
    dp.add_handler(CommandHandler("addserver", admin_handlers.addserver_cmd))
    dp.add_handler(CommandHandler("serverwizard", admin_handlers.serverwizard_cmd))
    dp.add_handler(CommandHandler("serverconfig", admin_handlers.serverconfig_cmd))
    dp.add_handler(CommandHandler("setserverfield", admin_handlers.setserverfield_cmd))
    dp.add_handler(CommandHandler("syncnodeenv", admin_handlers.syncnodeenv_cmd))
    dp.add_handler(CommandHandler("probeserver", admin_handlers.probeserver_cmd))
    dp.add_handler(CommandHandler("bootstrapserver", admin_handlers.bootstrapserver_cmd))
    dp.add_handler(CommandHandler("collecttraffic", admin_handlers.collecttraffic_cmd))
    dp.add_handler(CommandHandler("diag", admin_handlers.diag_cmd))
    dp.add_handler(CommandHandler("setxrayserver", admin_handlers.setxrayserver_cmd))
    dp.add_handler(CommandHandler("syncxrayserver", admin_handlers.syncxrayserver_cmd))
    dp.add_handler(CommandHandler("sshkey", admin_handlers.sshkey_cmd))

    dp.add_handler(CommandHandler("createcfg", admin_handlers.createcfg_cmd))
    dp.add_handler(CommandHandler("changecfg", admin_handlers.changecfg_cmd))

    # inline callbacks (single router)
    dp.add_handler(CallbackQueryHandler(on_callback))

    # text input for inline wizards (admin only)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_handlers.admin_text_router))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
