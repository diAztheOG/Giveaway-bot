import asyncio
import csv
import io
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

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
PREDICTIONS_FILE = os.path.join(BASE_DIR, "predictions.json")


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


# Load existing data on startup
giveaways: Dict[str, Dict[str, str]] = load_json(DATA_FILE)
predictions: Dict[str, dict] = load_json(PREDICTIONS_FILE)


def load_winners_history() -> dict:
    return load_json(WINNERS_FILE)


def save_winners_history(history: dict):
    save_json(WINNERS_FILE, history)


def is_in_history(uid: str, cp_username: str, history: dict) -> bool:
    locked_discord = history.get("discord_ids", {})
    locked_usernames = history.get("usernames", {})
    return (
        str(uid) in locked_discord
        or cp_username.strip().lower() in locked_usernames
    )


def pick_prioritized_winners(
    pool: List[str],
    entries: Dict[str, dict],
    history: dict,
    count: int,
    already_picked_this_draw: Set[str],
) -> List[str]:
    """
    Select up to `count` winners from `pool` while:
    1. Avoiding users already picked in `already_picked_this_draw` whenever possible.
    2. Preferring users not in `winners_history` whenever possible.
    """
    if not pool or count <= 0:
        return []

    chosen: List[str] = []

    tiers = [
        # Tier 1: Not picked in this draw AND not in past winners history
        [
            uid
            for uid in pool
            if uid not in already_picked_this_draw
            and not is_in_history(uid, entries[uid]["username"], history)
        ],
        # Tier 2: Not picked in this draw, even if in past winners history
        [uid for uid in pool if uid not in already_picked_this_draw],
        # Tier 3: Fallback if pool is exhausted and we still need winners
        list(pool),
    ]

    for tier in tiers:
        candidates = [uid for uid in tier if uid not in chosen]
        needed = count - len(chosen)
        if needed <= 0:
            break
        if candidates:
            picks = random.sample(candidates, min(needed, len(candidates)))
            chosen.extend(picks)

    already_picked_this_draw.update(chosen)
    return chosen


async def auto_close_message(
    msg: discord.Message, duration_minutes: int, closed_label: str = "Closed 🔒"
):
    await asyncio.sleep(duration_minutes * 60)

    try:
        channel = msg.channel
        target_message = await channel.fetch_message(msg.id)

        if (
            target_message.embeds
            and target_message.embeds[0].footer.text
            and "Closed" in target_message.embeds[0].footer.text
        ):
            return

        disabled_view = discord.ui.View()
        disabled_btn = discord.ui.Button(
            label=closed_label,
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        disabled_view.add_item(disabled_btn)

        embed = (
            target_message.embeds[0]
            if target_message.embeds
            else discord.Embed(title="Event Ended")
        )
        embed.set_footer(text=f"🔒 {closed_label}")

        await target_message.edit(embed=embed, view=disabled_view)
    except Exception as e:
        print(f"Failed to auto-close message {msg.id}: {e}")


# ==========================================
# 1. STANDARD GIVEAWAY VIEWS & MODALS
# ==========================================
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

        current_data = load_json(DATA_FILE)
        if msg_id in current_data and user_id in current_data[msg_id]:
            await interaction.response.send_message(
                "⚠️ **Only one entry per user allowed. You are already registered!**",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(UsernameModal(message_id=msg_id))


# ==========================================
# 2. WATCH PARTY PREDICTION VIEWS & MODALS
# ==========================================
class FirstOutSelect(discord.ui.Select):
    def __init__(self, players: List[str]):
        options = [discord.SelectOption(label=p, value=p) for p in players[:25]]
        super().__init__(
            placeholder="1️⃣ Select who drops out FIRST...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.first_out = self.values[0]
        await self.view.check_and_save(interaction)


class ChampionSelect(discord.ui.Select):
    def __init__(self, players: List[str]):
        options = [discord.SelectOption(label=p, value=p) for p in players[:25]]
        super().__init__(
            placeholder="2️⃣ Select who WINS the tournament...",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.champion = self.values[0]
        await self.view.check_and_save(interaction)


class PredictionDropdownView(discord.ui.View):
    def __init__(self, message_id: str, cp_username: str, players: List[str]):
        super().__init__(timeout=300)
        self.message_id = str(message_id)
        self.cp_username = cp_username
        self.first_out: Optional[str] = None
        self.champion: Optional[str] = None

        self.add_item(FirstOutSelect(players))
        self.add_item(ChampionSelect(players))

    async def check_and_save(self, interaction: discord.Interaction):
        if not self.first_out or not self.champion:
            status_lines = [
                f"**CoinPoker Username:** `{self.cp_username}`",
                f"• **1st Out:** {f'**{self.first_out}** ✅' if self.first_out else '*Waiting for selection...*'}",
                f"• **Winner:** {f'**{self.champion}** ✅' if self.champion else '*Waiting for selection...*'}",
            ]
            await interaction.response.edit_message(
                content="🎯 **Almost done! Make your second pick below:**\n\n"
                + "\n".join(status_lines),
                view=self,
            )
            return

        if self.first_out == self.champion:
            await interaction.response.edit_message(
                content="⚠️ **Invalid combo:** The same player can't be eliminated first AND win! Pick two different players below:",
                view=self,
            )
            return

        user_id = str(interaction.user.id)
        current_preds = load_json(PREDICTIONS_FILE)

        if self.message_id not in current_preds:
            current_preds[self.message_id] = {"players": [], "entries": {}}

        if user_id in current_preds[self.message_id]["entries"]:
            await interaction.response.edit_message(
                content="⚠️ **You already locked in your picks for this watch party!**",
                view=None,
            )
            return

        current_preds[self.message_id]["entries"][user_id] = {
            "username": self.cp_username,
            "first_out": self.first_out,
            "champion": self.champion,
        }
        save_json(PREDICTIONS_FILE, current_preds)
        predictions.update(current_preds)

        await interaction.response.edit_message(
            content=(
                f"✅ **Picks Locked In!**\n"
                f"• **CoinPoker Username:** `{self.cp_username}`\n"
                f"• **1st Final Table Out:** `{self.first_out}`\n"
                f"• **Tournament Winner:** `{self.champion}`\n\n"
                f"*Good luck watching the final table!*"
            ),
            view=None,
        )


class PredictionUsernameModal(discord.ui.Modal, title="Watch Party Picks — Step 1"):
    cp_username = discord.ui.TextInput(
        label="CoinPoker Username",
        placeholder="e.g. coinbluffer69",
        required=True,
        max_length=50,
    )

    def __init__(self, message_id: str, players: List[str]):
        super().__init__()
        self.message_id = str(message_id)
        self.players = players

    async def on_submit(self, interaction: discord.Interaction):
        entry_name = self.cp_username.value.strip()
        view = PredictionDropdownView(
            message_id=self.message_id,
            cp_username=entry_name,
            players=self.players,
        )
        await interaction.response.send_message(
            f"🎯 **Step 2 — Select your two picks from the dropdowns below** (Username: `{entry_name}`):",
            view=view,
            ephemeral=True,
        )


class PredictionLaunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit Picks 🎯",
        style=discord.ButtonStyle.primary,
        custom_id="submit_prediction_btn",
    )
    async def predict_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        msg_id = str(interaction.message.id)
        user_id = str(interaction.user.id)

        current_preds = load_json(PREDICTIONS_FILE)
        event_data = current_preds.get(msg_id)

        if not event_data or not event_data.get("players"):
            await interaction.response.send_message(
                "❌ This prediction event could not be found or has no players configured.",
                ephemeral=True,
            )
            return

        if user_id in event_data.get("entries", {}):
            existing = event_data["entries"][user_id]
            await interaction.response.send_message(
                f"⚠️ **You already submitted your picks!**\n"
                f"• **1st Out:** `{existing['first_out']}`\n"
                f"• **Winner:** `{existing['champion']}`",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            PredictionUsernameModal(
                message_id=msg_id, players=event_data["players"]
            )
        )


# ==========================================
# 3. BOT SETUP & SLASH COMMANDS
# ==========================================
class GiveawayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(GiveawayView())
        self.add_view(PredictionLaunchView())
        await self.tree.sync()


bot = GiveawayBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# --- STANDARD GIVEAWAY COMMANDS ---
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
        bot.loop.create_task(
            auto_close_message(msg, duration_minutes, "Giveaway Closed 🔒")
        )


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

    history = load_winners_history()
    locked_discord = history.get("discord_ids", {})
    locked_usernames = history.get("usernames", {})

    eligible_user_ids = [
        uid
        for uid, cp_name in entries.items()
        if str(uid) not in locked_discord
        and cp_name.strip().lower() not in locked_usernames
    ]

    if not eligible_user_ids:
        await interaction.response.send_message(
            f"⚠️ **No eligible winners left!** All {len(entries)} participants in this giveaway have already won a previous giveaway.",
            ephemeral=True,
        )
        return

    actual_winners_count = min(winners_count, len(eligible_user_ids))
    winner_ids = random.sample(eligible_user_ids, actual_winners_count)
    winner_ids.sort(key=lambda uid: entries[uid].lower())

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
    description="Check if a user has won a past giveaway or watch party",
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
            f"• **Event ID:** `{found_record['giveaway_id']}`\n"
            f"• **Won At:** `{found_record['won_at']}`",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"✅ **No history found.** `{query}` has not won any recorded events yet.",
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


# --- WATCH PARTY PREDICTION COMMANDS ---
@bot.tree.command(
    name="start_prediction",
    description="Start a Final Table Watch Party prediction game with dropdowns",
)
@app_commands.describe(
    title="Watch party title (e.g. Sunday Special Final Table Watch Party)",
    players="Comma-separated list of finalists (e.g. Player1, Player2, Player3, Player4, Player5, Player6, Player7)",
    duration_minutes="Optional: Auto-lock picks after X minutes",
    image_url="Optional: Banner image URL",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def start_prediction(
    interaction: discord.Interaction,
    title: str,
    players: str,
    duration_minutes: Optional[int] = None,
    image_url: Optional[str] = None,
):
    player_list = [p.strip() for p in players.split(",") if p.strip()]
    seen = set()
    unique_players = []
    for p in player_list:
        if p.lower() not in seen:
            seen.add(p.lower())
            unique_players.append(p)

    if len(unique_players) < 2 or len(unique_players) > 25:
        await interaction.response.send_message(
            "❌ Please provide between **2 and 25** unique player names separated by commas.",
            ephemeral=True,
        )
        return

    formatted_roster = "\n".join(f"• **{p}**" for p in unique_players)

    embed = discord.Embed(
        title=f"🎯 {title}",
        description=(
            "Click **Submit Picks 🎯** below to enter your CoinPoker username and select:\n"
            "1️⃣ **First player eliminated** from the final table\n"
            "2️⃣ **Tournament champion**\n\n"
            f"**Final Table Lineup:**\n{formatted_roster}\n\n"
            "**Rules:**\n"
            "• One submission per user\n"
            "• **2 winners** drawn from correct **1st Out** picks & **2 winners** drawn from correct **Champion** picks!"
        ),
        color=discord.Color.blurple(),
    )

    if image_url:
        embed.set_image(url=image_url.strip())

    if duration_minutes:
        embed.set_footer(text=f"⏱️ Picks lock in {duration_minutes} minute(s)")

    await interaction.response.send_message(
        "Starting watch party prediction...", ephemeral=True
    )

    view = PredictionLaunchView()
    msg = await interaction.channel.send(embed=embed, view=view)

    current_preds = load_json(PREDICTIONS_FILE)
    current_preds[str(msg.id)] = {
        "title": title,
        "players": unique_players,
        "entries": {},
    }
    save_json(PREDICTIONS_FILE, current_preds)
    predictions.update(current_preds)

    if duration_minutes and duration_minutes > 0:
        bot.loop.create_task(
            auto_close_message(msg, duration_minutes, "Picks Locked 🔒")
        )


@bot.tree.command(
    name="stop_prediction",
    description="Lock Watch Party picks so no new submissions can be made",
)
@app_commands.describe(message_id="The ID of the prediction message to lock")
@app_commands.checks.has_permissions(manage_messages=True)
async def stop_prediction(interaction: discord.Interaction, message_id: str):
    try:
        msg_id = message_id.strip()
        target_message = await interaction.channel.fetch_message(int(msg_id))

        disabled_view = discord.ui.View()
        disabled_btn = discord.ui.Button(
            label="Picks Locked 🔒",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        disabled_view.add_item(disabled_btn)

        embed = (
            target_message.embeds[0]
            if target_message.embeds
            else discord.Embed(title="Picks Locked")
        )
        embed.set_footer(text="🔒 Picks Locked")

        await target_message.edit(embed=embed, view=disabled_view)

        await interaction.response.send_message(
            f"🔒 **Watch party `{msg_id}` is now locked!**",
            ephemeral=True,
        )
    except Exception:
        await interaction.response.send_message(
            f"❌ Could not find message with ID `{message_id}` in this channel.",
            ephemeral=True,
        )


@bot.tree.command(
    name="prediction_stats",
    description="See how many people picked each player for 1st Out and Winner",
)
@app_commands.describe(message_id="The ID of the prediction message")
@app_commands.checks.has_permissions(manage_messages=True)
async def prediction_stats(interaction: discord.Interaction, message_id: str):
    msg_id = message_id.strip()
    current_preds = load_json(PREDICTIONS_FILE)
    event_data = current_preds.get(msg_id)

    if not event_data:
        await interaction.response.send_message(
            f"❌ Could not find watch party data for message ID `{msg_id}`.",
            ephemeral=True,
        )
        return

    players = event_data.get("players", [])
    entries = event_data.get("entries", {})

    first_counts = {p: 0 for p in players}
    champ_counts = {p: 0 for p in players}

    for data in entries.values():
        fo = data.get("first_out")
        ch = data.get("champion")
        if fo:
            first_counts[fo] = first_counts.get(fo, 0) + 1
        if ch:
            champ_counts[ch] = champ_counts.get(ch, 0) + 1

    sorted_first = sorted(
        first_counts.items(), key=lambda x: (-x[1], x[0].lower())
    )
    sorted_champ = sorted(
        champ_counts.items(), key=lambda x: (-x[1], x[0].lower())
    )

    first_lines = "\n".join(
        f"• **{player}:** `{count}`" for player, count in sorted_first
    )
    champ_lines = "\n".join(
        f"• **{player}:** `{count}`" for player, count in sorted_champ
    )

    title = event_data.get("title", "Watch Party")
    msg_text = (
        f"📊 **Pick Breakdown — `{title}`** (`{msg_id}`)\n"
        f"*Total Submissions: **{len(entries)}***\n\n"
        f"💀 **1st Out Picks:**\n{first_lines or '*None*'}\n\n"
        f"👑 **Champion Picks:**\n{champ_lines or '*None*'}"
    )

    await interaction.response.send_message(msg_text, ephemeral=True)


@bot.tree.command(
    name="draw_prediction",
    description="Draw distinct winners from correct Watch Party picks (default: 2 per category)",
)
@app_commands.describe(
    message_id="The ID of the prediction message",
    first_out="Exact name of the player eliminated FIRST",
    champion="Exact name of the tournament WINNER",
    winners_per_pick="Number of winners to pick per category (default: 2)",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def draw_prediction(
    interaction: discord.Interaction,
    message_id: str,
    first_out: str,
    champion: str,
    winners_per_pick: int = 2,
):
    msg_id = message_id.strip()
    current_preds = load_json(PREDICTIONS_FILE)
    event_data = current_preds.get(msg_id)

    if not event_data or not event_data.get("entries"):
        await interaction.response.send_message(
            "❌ No entries found for that prediction message ID.",
            ephemeral=True,
        )
        return

    players = event_data.get("players", [])
    matched_first = next(
        (p for p in players if p.lower() == first_out.strip().lower()), None
    )
    matched_champ = next(
        (p for p in players if p.lower() == champion.strip().lower()), None
    )

    if not matched_first or not matched_champ:
        await interaction.response.send_message(
            f"❌ Player name didn't match the event roster.\n"
            f"**Valid players:** `{', '.join(players)}`",
            ephemeral=True,
        )
        return

    entries = event_data["entries"]
    count = max(1, winners_per_pick)
    history = load_winners_history()
    picked_this_draw: Set[str] = set()

    # Pool 1: Correct 1st Elimination
    first_out_pool = [
        uid
        for uid, data in entries.items()
        if data["first_out"] == matched_first
    ]

    # Pool 2: Correct Tournament Champion
    champion_pool = [
        uid
        for uid, data in entries.items()
        if data["champion"] == matched_champ
    ]

    first_winner_ids = pick_prioritized_winners(
        pool=first_out_pool,
        entries=entries,
        history=history,
        count=count,
        already_picked_this_draw=picked_this_draw,
    )

    champ_winner_ids = pick_prioritized_winners(
        pool=champion_pool,
        entries=entries,
        history=history,
        count=count,
        already_picked_this_draw=picked_this_draw,
    )

    # Record all drawn watch party winners in winners_history.json
    all_drawn_ids = list(dict.fromkeys(first_winner_ids + champ_winner_ids))
    if all_drawn_ids:
        if "discord_ids" not in history:
            history["discord_ids"] = {}
        if "usernames" not in history:
            history["usernames"] = {}

        win_timestamp = datetime.now(timezone.utc).isoformat()
        for uid in all_drawn_ids:
            cp_user = entries[uid]["username"]
            history["discord_ids"][str(uid)] = {
                "username": cp_user,
                "giveaway_id": f"wp_{msg_id}",
                "won_at": win_timestamp,
            }
            history["usernames"][cp_user.lower()] = str(uid)

        save_winners_history(history)

    title = event_data.get("title", "Watch Party Prediction")

    # Build CSV for crediting prizes
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["Category", "Discord_ID", "CoinPoker_Username", "Pick"])
    for uid in first_winner_ids:
        writer.writerow([
            "1st Elimination",
            uid,
            entries[uid]["username"],
            matched_first,
        ])
    for uid in champ_winner_ids:
        writer.writerow([
            "Tournament Winner",
            uid,
            entries[uid]["username"],
            matched_champ,
        ])

    csv_buffer.seek(0)
    csv_file = discord.File(
        fp=io.BytesIO(csv_buffer.getvalue().encode("utf-8")),
        filename=f"watchparty_winners_{msg_id}.csv",
    )

    if first_winner_ids:
        first_mentions = "\n".join(
            f"• <@{uid}> (`{entries[uid]['username']}`)"
            for uid in first_winner_ids
        )
        first_winner_text = (
            f"{first_mentions}\n"
            f"*(Picked **{matched_first}** — {len(first_out_pool)} total guessed right)*"
        )
    else:
        first_winner_text = f"No one guessed **{matched_first}**!"

    if champ_winner_ids:
        champ_mentions = "\n".join(
            f"• <@{uid}> (`{entries[uid]['username']}`)"
            for uid in champ_winner_ids
        )
        champ_winner_text = (
            f"{champ_mentions}\n"
            f"*(Picked **{matched_champ}** — {len(champion_pool)} total guessed right)*"
        )
    else:
        champ_winner_text = f"No one guessed **{matched_champ}**!"

    result_msg = (
        f"🏆 **Watch Party Results — `{title}`!**\n"
        f"*(Total Entries: {len(entries)})*\n\n"
        f"💀 **First Out (`{matched_first}`):**\n{first_winner_text}\n\n"
        f"👑 **Tournament Champion (`{matched_champ}`):**\n{champ_winner_text}"
    )

    await interaction.response.send_message(result_msg, file=csv_file)


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