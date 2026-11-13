import asyncio
import csv
import io
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Use Railway Volume path if configured via DATA_DIR env var, otherwise local folder
BASE_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(BASE_DIR, exist_ok=True)
DATA_FILE = os.path.join(BASE_DIR, "giveaways.json")
WINNERS_FILE = os.path.join(BASE_DIR, "winners_history.json")


# --- PERSISTENT STORAGE HELPERS ---
def load_json(file_path: str) -> dict:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_json(file_path: str, data: dict):
    for _ in range(3):
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            return
        except PermissionError:
            time.sleep(0.1)
        except Exception as e:
            print(f"Error saving to {file_path}: {e}")
            return


# Load existing giveaways on startup
giveaways: Dict[str, Dict[str, str]] = load_json(DATA_FILE)


def load_winners_history() -> dict:
    return load_json(WINNERS_FILE)


def save_winners_history(history: dict):
    save_json(WINNERS_FILE, history)


async def auto_close_giveaway(msg: discord.Message, duration_minutes: int):
    await asyncio.sleep(duration_minutes * 60)

    try:
        channel = msg.channel
        target_message = await channel.fetch_message(msg.id)

        if (
            target_message.embeds
            and target_message.embeds[0].footer.text == "🔒 Giveaway Closed"
        ):
            return

        disabled_view = discord.ui.View()
        disabled_btn = discord.ui.Button(
            label="Giveaway Closed 🔒",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        disabled_view.add_item(disabled_btn)

        embed = (
            target_message.embeds[0]
            if target_message.embeds
            else discord.Embed(title="Giveaway Ended")
        )
        embed.set_footer(text="🔒 Giveaway Closed")

        await target_message.edit(embed=embed, view=disabled_view)
    except Exception as e:
        print(f"Failed to auto-close giveaway {msg.id}: {e}")


class UsernameModal(discord.ui.Modal, title="Giveaway Entry"):
    cp_username = discord.ui.TextInput(
        label="CoinPoker Username",
        placeholder="e.g. coinbluffer69",
        required=True,
        max_length=50,
    )

    def __init__(self, message_id: str):
        super().__init__()
        self.message_id = str(message_id)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        entry_name = self.cp_username.value.strip()

        if self.message_id not in giveaways:
            giveaways[self.message_id] = {}

        giveaways[self.message_id][user_id] = entry_name
        save_json(DATA_FILE, giveaways)

        await interaction.response.send_message(
            f"✅ **Entry recorded!** Username: **{entry_name}**",
            ephemeral=True,
        )


class GiveawayView(discord.ui.View):
    def __init__(self, message_id: str = None):
        super().__init__(timeout=None)
        self.message_id = str(message_id) if message_id else None

    @discord.ui.button(
        label="Enter Giveaway 🎟️",
        style=discord.ButtonStyle.success,
        custom_id="enter_giveaway_btn",
    )
    async def enter_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        msg_id = str(interaction.message.id)
        user_id = str(interaction.user.id)

        # Check if already registered in this specific giveaway
        current_data = load_json(DATA_FILE)
        if msg_id in current_data and user_id in current_data[msg_id]:
            await interaction.response.send_message(
                "⚠️ **Only one entry per user allowed. You are already registered!**",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(UsernameModal(message_id=msg_id))


class GiveawayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(GiveawayView())
        await self.tree.sync()


bot = GiveawayBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(
    name="start_giveaway", description="Start a new custom giveaway"
)
@app_commands.describe(
    title="Giveaway or prize title (e.g. GOPC $500 Drop)",
    duration_minutes="Optional: Auto-close after X minutes (e.g. 60)",
    image_url="Optional: Direct image link to embed a banner",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def start_giveaway(
    interaction: discord.Interaction,
    title: str,
    duration_minutes: Optional[int] = None,
    image_url: Optional[str] = None,
):
    embed = discord.Embed(
        title=f"🎉 {title}",
        description="Click the button below to submit your **CoinPoker Username** and enter the giveaway!\n\n"
        "**Rules**\n"
        "• One entry per user\n"
        "• Double check your username spelling",
        color=discord.Color.gold(),
    )

    if image_url:
        embed.set_image(url=image_url.strip())

    if duration_minutes:
        embed.set_footer(text=f"⏱️ Ends in {duration_minutes} minute(s)")

    await interaction.response.send_message("Creating giveaway...", ephemeral=True)

    view = GiveawayView()
    msg = await interaction.channel.send(embed=embed, view=view)

    giveaways[str(msg.id)] = {}
    save_json(DATA_FILE, giveaways)

    if duration_minutes and duration_minutes > 0:
        bot.loop.create_task(auto_close_giveaway(msg, duration_minutes))


@bot.tree.command(
    name="stop_giveaway",
    description="End a giveaway early and disable the button",
)
@app_commands.describe(
    message_id="The ID of the giveaway message you want to lock"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def stop_giveaway(interaction: discord.Interaction, message_id: str):
    try:
        msg_id = message_id.strip()
        channel = interaction.channel
        target_message = await channel.fetch_message(int(msg_id))

        disabled_view = discord.ui.View()
        disabled_btn = discord.ui.Button(
            label="Giveaway Closed 🔒",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        disabled_view.add_item(disabled_btn)

        embed = (
            target_message.embeds[0]
            if target_message.embeds
            else discord.Embed(title="Giveaway Ended")
        )
        embed.set_footer(text="🔒 Giveaway Closed")

        await target_message.edit(embed=embed, view=disabled_view)

        await interaction.response.send_message(
            f"🔒 **Giveaway `{msg_id}` has been closed!** Entries are now locked.",
            ephemeral=True,
        )
    except Exception:
        await interaction.response.send_message(
            f"❌ Could not find message with ID `{message_id}` in this channel.",
            ephemeral=True,
        )


@bot.tree.command(
    name="draw_giveaway",
    description="Pick unique winners (excluding past winners) and export CSV",
)
@app_commands.describe(
    message_id="The ID of the giveaway message",
    winners_count="Number of winners to pick",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def draw_giveaway(
    interaction: discord.Interaction, message_id: str, winners_count: int = 1
):
    msg_id = message_id.strip()
    current_data = load_json(DATA_FILE)

    if msg_id not in current_data or not current_data[msg_id]:
        await interaction.response.send_message(
            "❌ No entries found for that giveaway message ID.", ephemeral=True
        )
        return

    giveaway_title = "Giveaway"
    try:
        target_msg = await interaction.channel.fetch_message(int(msg_id))
        if target_msg.embeds and target_msg.embeds[0].title:
            giveaway_title = target_msg.embeds[0].title.replace("🎉 ", "").strip()
    except Exception:
        pass

    entries = current_data[msg_id]

    # --- FILTER OUT PREVIOUS WINNERS ---
    history = load_winners_history()
    locked_discord = history.get("discord_ids", {})
    locked_usernames = history.get("usernames", {})

    eligible_user_ids = [
        uid
        for uid, cp_name in entries.items()
        if str(uid) not in locked_discord and cp_name.strip().lower() not in locked_usernames
    ]

    if not eligible_user_ids:
        await interaction.response.send_message(
            f"⚠️ **No eligible winners left!** All {len(entries)} participants in this giveaway have already won a previous giveaway.",
            ephemeral=True,
        )
        return

    actual_winners_count = min(winners_count, len(eligible_user_ids))
    winner_ids = random.sample(eligible_user_ids, actual_winners_count)

    # SORT WINNERS ALPHABETICALLY BY COINPOKER USERNAME (case-insensitive)
    winner_ids.sort(key=lambda uid: entries[uid].lower())

    # --- RECORD NEW WINNERS TO HISTORY ---
    if "discord_ids" not in history:
        history["discord_ids"] = {}
    if "usernames" not in history:
        history["usernames"] = {}

    win_timestamp = datetime.now(timezone.utc).isoformat()
    for uid in winner_ids:
        cp_user = entries[uid]
        history["discord_ids"][str(uid)] = {
            "username": cp_user,
            "giveaway_id": msg_id,
            "won_at": win_timestamp,
        }
        history["usernames"][cp_user.lower()] = str(uid)

    save_winners_history(history)
    # --------------------------------------

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Discord_ID", "CoinPoker_Username"])
    for uid in winner_ids:
        writer.writerow([str(uid), entries[uid]])

    csv_buffer.seek(0)
    csv_file = discord.File(
        fp=io.BytesIO(csv_buffer.getvalue().encode("utf-8")),
        filename=f"winners_{msg_id}.csv",
    )

    winner_lines = [f"<@{uid}> ({entries[uid]})" for uid in winner_ids]

    chunks = []
    current_chunk = []
    current_length = 0

    for line in winner_lines:
        if current_length + len(line) + 1 > 1800:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_length = len(line)
        else:
            current_chunk.append(line)
            current_length += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    first_message = (
        f"🎉 **{actual_winners_count} Winners Picked for: `{giveaway_title}`!** (Batch 1/{len(chunks)})\n\n"
        f"{chunks[0]}"
    )
    await interaction.response.send_message(first_message, file=csv_file)

    for idx, chunk in enumerate(chunks[1:], start=2):
        followup_message = (
            f"🎉 **Winners Continued (`{giveaway_title}`)** (Batch {idx}/{len(chunks)}):\n\n"
            f"{chunk}"
        )
        await interaction.followup.send(followup_message)


@bot.tree.command(
    name="check_winner",
    description="Check if a user has won a past giveaway",
)
@app_commands.describe(query="Discord ID or CoinPoker username to check")
@app_commands.checks.has_permissions(manage_messages=True)
async def check_winner(interaction: discord.Interaction, query: str):
    history = load_winners_history()
    discord_dict = history.get("discord_ids", {})
    username_dict = history.get("usernames", {})

    q = query.strip()
    q_lower = q.lower()

    found_record = None
    matched_key = None

    if q in discord_dict:
        found_record = discord_dict[q]
        matched_key = f"Discord ID: {q}"
    elif q_lower in username_dict:
        discord_id = username_dict[q_lower]
        found_record = discord_dict.get(discord_id)
        matched_key = f"Username: {q}"

    if found_record:
        await interaction.response.send_message(
            f"🔍 **Winner Found!**\n"
            f"• **Query Matched:** `{matched_key}`\n"
            f"• **CoinPoker Username:** `{found_record['username']}`\n"
            f"• **Giveaway ID:** `{found_record['giveaway_id']}`\n"
            f"• **Won At:** `{found_record['won_at']}`",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"✅ **No history found.** `{query}` has not won any recorded giveaways yet.",
            ephemeral=True,
        )


@bot.tree.command(
    name="reset_history",
    description="Wipe all past winner records (Use for a brand new season)",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def reset_history(interaction: discord.Interaction):
    save_winners_history({"discord_ids": {}, "usernames": {}})
    await interaction.response.send_message(
        "🗑️ **Winner history has been completely wiped!** All users are now eligible again.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⛔ **Permission Denied:** You don't have the required server permissions to run this command.",
            ephemeral=True,
        )
    else:
        print(f"Unhandled slash command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Something went wrong executing that command.",
                ephemeral=True,
            )


bot.run(TOKEN)