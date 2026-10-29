import io
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
import discord
from discord import app_commands
from bot.config import Settings
from bot.discord_gateway import DiscordGuildGateway, snapshot_from_member
from bot.logging_config import AUDIT_LOGGER_NAME, audit_event
from bot.permissions import check_interaction
from bot.reporting import (format_detailed_report,format_inspection,format_summary,has_details,truncate,)
from bot.sync import FatalSyncError, SyncBusyError, SyncReport, SyncService

logger = logging.getLogger("bot.commands")
_audit = logging.getLogger(AUDIT_LOGGER_NAME)

BUSY_MESSAGE = "Please wait to finish and try again."
GENERIC_ERROR_MESSAGE = (
    "Something went wrong (No changes)"
    "check the bot's log or ask admin."
)

GatewayFactory = Callable[[discord.Guild], object]
ReportNotifier = Callable[[SyncReport], Awaitable[None]]

class CommandHandlers:
    
    def __init__(
        self,
        settings: Settings,
        service: SyncService,
        *,
        gateway_factory: GatewayFactory = DiscordGuildGateway,
        notifier: Optional[ReportNotifier] = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._gateway_factory = gateway_factory
        self._notifier = notifier

    async def _authorize(self, interaction, command: str) -> bool:
        result = check_interaction(interaction, self._settings.guild_id, self._settings.hr_role_ids)
        if result.allowed:
            return True
        user = getattr(interaction, "user", None)
        audit_event(
            _audit, "command_denied",
            command=command, user=f"{getattr(user, 'name', '?')} ({getattr(user, 'id', '?')})",
            guild=getattr(getattr(interaction, "guild", None), "id", None), outcome="denied",
        )
        await interaction.response.send_message(result.message, ephemeral=True)
        return False

    @staticmethod
    def _initiator(interaction) -> str:
        user = interaction.user
        return f"{user.name} ({user.id})"

    async def sync_roles(self, interaction, dry_run: bool = False) -> None:
       
        await self._run_sync(interaction, dry_run=dry_run, command="sync-roles")

    async def preview_sync(self, interaction) -> None:
       
        await self._run_sync(interaction, dry_run=True, command="preview-sync")

    async def _run_sync(self, interaction, *, dry_run: bool, command: str) -> None:
        if not await self._authorize(interaction, command):
            return
        if self._service.is_running:
            await interaction.response.send_message(BUSY_MESSAGE, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send("Reading the spreadsheet(no changes will be applied)"
            if dry_run
            else "Reading the spreadsheet and synchronizing roles.",
            ephemeral=True,
        )

        try:
            gateway = self._gateway_factory(interaction.guild)
            report = await self._service.run(
                gateway, dry_run=dry_run, initiated_by=self._initiator(interaction)
            )
        except SyncBusyError:
            await interaction.followup.send(BUSY_MESSAGE, ephemeral=True)
            return
        except FatalSyncError as exc:
            lines = "\n".join(f"• {p}" for p in exc.problems)
            await interaction.followup.send(
                truncate(f"Synchronization canceled (no roles changed)\n{lines}"),
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception("error  %s", command)
            await interaction.followup.send(GENERIC_ERROR_MESSAGE, ephemeral=True)
            return

        await self._send_report(interaction, report)
        if self._notifier is not None:
            try:
                await self._notifier(report)
            except Exception:
                logger.warning("Could not post to the sync log channel", exc_info=True)

    async def _send_report(self, interaction, report: SyncReport) -> None:
        kwargs = {"content": format_summary(report), "ephemeral": True}
        if has_details(report):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            prefix = "dry-run-report" if report.dry_run else "sync-report"
            data = format_detailed_report(report).encode("utf-8")
            kwargs["file"] = discord.File(io.BytesIO(data), filename=f"{prefix}-{stamp}.txt")
        try:
            await interaction.followup.send(**kwargs)
        except discord.HTTPException:
            logger.warning("error sending report", exc_info=True)

    async def check_member(self, interaction, member) -> None:
        
        if not await self._authorize(interaction, "check-member"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            gateway = self._gateway_factory(interaction.guild)
            inspection = await self._service.inspect_member(gateway, snapshot_from_member(member))
        except FatalSyncError as exc:
            lines = "\n".join(f"• {p}" for p in exc.problems)
            await interaction.followup.send(truncate(f"Couldnt check the member.\n{lines}"), ephemeral=True)
            return
        except Exception:
            logger.exception("Unexpected error during check-member")
            await interaction.followup.send(GENERIC_ERROR_MESSAGE, ephemeral=True)
            return
        await interaction.followup.send(format_inspection(inspection), ephemeral=True)


def register_commands(
    tree: app_commands.CommandTree, handlers: CommandHandlers, guild: discord.abc.Snowflake
) -> None:

    @tree.command(
        name="sync-roles",
        description="Synchronize managed roles with the Google Sheet (HR only).",
        guild=guild,
    )
    @app_commands.describe(dry_run="Only show what would change, dont apply.")
    @app_commands.guild_only()
    async def sync_roles(interaction: discord.Interaction, dry_run: bool = False):
        await handlers.sync_roles(interaction, dry_run)

    @tree.command(
        name="preview-sync",
        description="Show role changes from the Google Sheet (HR only).",
        guild=guild,
    )
    @app_commands.guild_only()
    async def preview_sync(interaction: discord.Interaction):
        await handlers.preview_sync(interaction)

    @tree.command(
        name="check-member",
        description="Compare one member's roles with the Google Sheet (HR only, read-only).",
        guild=guild,
    )
    @app_commands.describe(member="The member to check.")
    @app_commands.guild_only()
    async def check_member(interaction: discord.Interaction, member: discord.Member):
        await handlers.check_member(interaction, member)
