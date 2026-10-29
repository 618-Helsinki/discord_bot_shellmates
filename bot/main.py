import logging
import sys
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from bot.commands import CommandHandlers, register_commands
from bot.config import ConfigError, Settings, load_settings
from bot.discord_gateway import DiscordGuildGateway, snapshot_from_member
from bot.logging_config import register_secret, setup_logging
from bot.reporting import format_log_channel_summary
from bot.sheets import SheetsClient
from bot.sync import FatalSyncError, SyncBusyError, SyncReport, SyncService

logger = logging.getLogger("bot.main")


class HRBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.service = SyncService(
            SheetsClient(settings.service_account_file, settings.sheet_id, settings.worksheet_name),
            settings.managed_roles,
            operation_delay=settings.operation_delay_seconds,
        )
        self.handlers = CommandHandlers(settings, self.service, notifier=self._post_log_summary)
        self._scheduled_loop: tasks.Loop | None = None
        self._commands_registered = False

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        if not self._commands_registered:
            register_commands(self.tree, self.handlers, guild)
            self.tree.on_error = self._on_command_error
            self._commands_registered = True
        try:
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d slash command(s) to guild %s", len(synced), guild.id)
        except discord.HTTPException:
            logger.exception("Failed. check the bot and guild ID")

        if self.settings.enable_scheduled_sync and self._scheduled_loop is None:
            loop = tasks.loop(minutes=self.settings.scheduled_sync_interval_minutes)(self._scheduled_sync)
            loop.before_loop(self.wait_until_ready)
            self._scheduled_loop = loop
            loop.start()
            logger.info("Scheduled sync enabled every %d minutes", self.settings.scheduled_sync_interval_minutes)

    async def on_ready(self) -> None:
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            logger.error("Connected, but the bot is not in guild %s. Invite it first.", self.settings.guild_id)
        else:
            logger.info("Ready as %s in guild '%s' (%d cached members)", self.user, guild.name, guild.member_count or 0)

    async def close(self) -> None:
        if self._scheduled_loop is not None:
            self._scheduled_loop.cancel()
        await super().close()

    async def _scheduled_sync(self) -> None:
        loop = self._scheduled_loop
        if loop is not None and loop.current_loop == 0:
            return
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            logger.error("Scheduled sync skipped: guild %s not available", self.settings.guild_id)
            return
        try:
            report = await self.service.run(DiscordGuildGateway(guild), dry_run=False, initiated_by="scheduled")
        except SyncBusyError:
            logger.info("Scheduled sync skipped: another sync is running")
            return
        except FatalSyncError as exc:
            logger.error("Scheduled sync aborted, no roles changed: %s", exc)
            return
        except Exception:
            logger.exception("Scheduled sync crashed")
            return
        logger.info("Scheduled sync done: +%d -%d, %d error(s)", report.roles_added, report.roles_removed, len(report.errors))
        await self._post_log_summary(report)

    async def on_member_join(self, member: discord.Member) -> None:
        if not self.settings.enable_join_sync or member.guild.id != self.settings.guild_id:
            return
        try:
            report = await self.service.sync_new_member(
                DiscordGuildGateway(member.guild), snapshot_from_member(member)
            )
            logger.info("Join sync for %s: +%d, %d error(s)", member.id, report.roles_added, len(report.errors))
        except FatalSyncError as exc:
            logger.error("Join sync aborted for %s: %s", member.id, exc)
        except Exception:
            logger.exception("Join sync crashed for %s", member.id)

    async def _post_log_summary(self, report: SyncReport) -> None:
        channel_id = self.settings.sync_log_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            logger.warning("SYNC_LOG_CHANNEL_ID %s not found or not visible to the bot", channel_id)
            return
        await channel.send(format_log_channel_summary(report))

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.error("Command error: %s", type(error).__name__, exc_info=error)
        message = "Command failed try again or contact an admin."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> int:
    load_dotenv()
    try:
        settings = load_settings()
        settings.ensure_credentials_file()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        print("\nFix your .env file (see .env.example) and try again.", file=sys.stderr)
        return 2

    register_secret(settings.discord_bot_token)
    setup_logging(settings.log_dir, settings.log_level)
    logger.info("Starting with configuration: %s", settings.sanitized_summary())
    for note in settings.warnings():
        logger.warning(note)

    bot = HRBot(settings)
    try:
        bot.run(settings.discord_bot_token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Discord rejected the bot token. Check DISCORD_BOT_TOKEN.")
        return 1
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "The Server Members Intent is not enabled. Enable it in the Discord Developer Portal "
            "(Bot -> Privileged Gateway Intents)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
