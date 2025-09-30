# main.py -- Event Signup Bot z auto-wydarzeniami, schedulerem i opisami ról

import asyncio
import os
import re
import json
import logging
import sys
from typing import Dict, Any
import datetime

import discord
from discord.ext import commands, tasks
from aiohttp import web

# ---- Config / Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("event-bot")

TOKEN = os.getenv("DISCORD_TOKEN", None)
if not TOKEN or TOKEN == "TWOJ_TOKEN":
    log.error("❌ Brak prawidłowego tokena Discorda. Ustaw zmienną DISCORD_TOKEN.")
    sys.exit(1)

MAX_HOURS = 10
DATA_FILE = "events.json"

# ---- Intents & Bot ----
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory
events: Dict[str, Dict[str, list]] = {}
messages: Dict[int, Dict[str, Any]] = {}

# ---- Reaction Roles Config ----


ROLE_CONFIG = {
    "🎮": {"role": "Ranked games", "desc": "Spotkania rankingowe w klubie USMA"},
    "🎲": {"role": "Casual games", "desc": "Luźne gry bez presji"},
    "🎉": {"role": "Events", "desc": "Powiadomienia o wydarzeniach i spotkaniach typu konwenty, nauki dla osób z zewnątrz klubu"},
    "🏆": {"role": "Tournaments", "desc": "Turnieje organizowane przez USMA"},
    "🐉": {"role": "MCR", "desc": "Spotkania MCR (Mahjong Competition Rules)"}
}

# ---- Auto Events Settings ----
AUTO_EVENTS_CHANNEL_ID = 1420866324799946844 # <- ustaw ID kanału
AUTO_EVENTS = {
    # 2: ("Środa", ["18:00", "19:30"]),
    # 5: ("Sobota", ["16:00", "18:30"]),
}
AUTO_EVENTS_HOUR = 8  # godzina publikacji (UTC)

# ---- Advanced Scheduled Events ----
SCHEDULED_EVENTS = [
    # przykład: co tydzień w niedzielę o 8:00
    {
        "name": "Ranked Środa",
        "times": ["17:30", "19:00", "20:30", "22:00"],
        "start_date": "2025-09-28",  # YYYY-MM-DD
        "hour": 19,  # UTC
        "interval_days": 7
    },
    {
        "name": "Ranked Sobota",
        "times": ["16:00", "17:30", "19:00", "20:30", "22:00"],
        "start_date": "2025-09-28",  # YYYY-MM-DD
        "hour": 19,  # UTC
        "interval_days": 7
    }
]

# ---- Helpers ----
def emoji_to_index(emoji_str: str) -> int | None:
    if not emoji_str:
        return None
    if emoji_str == "🔟":
        return 9
    m = re.match(r"^(\d+)", emoji_str)
    if m:
        try:
            return int(m.group(1)) - 1
        except ValueError:
            return None
    return None

async def safe_get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return None
    return member

def weekday_from_name(name: str) -> int | None:
    """Zwraca numer dnia tygodnia na podstawie fragmentu w nazwie (pon=0 ... nd=6)."""
    name = name.lower()
    weekday_map = {
        "pon": 0, "wt": 1, "śr": 2, "sro": 2, "czw": 3,
        "pt": 4, "sob": 5, "nd": 6, "niedz": 6, "nied": 6
    }
    for key, num in weekday_map.items():
        if key in name:
            return num
    return None

def next_weekday_date(weekday: int) -> str:
    """Zwraca datę (YYYY-MM-DD) najbliższego wystąpienia dnia tygodnia."""
    today = datetime.date.today()
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    target_date = today + datetime.timedelta(days=days_ahead)
    return target_date.strftime("%Y-%m-%d")

# ---- Persistence ----
def _serialize_for_save():
    dump_messages = {}
    for mid, data in messages.items():
        dump_messages[str(mid)] = {
            "nazwa": data["nazwa"],
            "godziny": list(data["godziny"]),
            "channel_id": int(data["channel_id"]),
        }
    return {"events": events, "messages": dump_messages}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(_serialize_for_save(), f, ensure_ascii=False, indent=2)
        log.info("💾 Dane zapisane do %s", DATA_FILE)
    except Exception as e:
        log.exception("Błąd zapisu danych: %s", e)

def load_data():
    global events, messages
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events", {}) or {}
        raw_messages = data.get("messages", {}) or {}
        messages = {}
        for k, v in raw_messages.items():
            try:
                mid = int(k)
                messages[mid] = {
                    "nazwa": v.get("nazwa"),
                    "godziny": tuple(v.get("godziny", [])),
                    "channel_id": int(v.get("channel_id")),
                }
            except Exception:
                continue
        log.info("📂 Wczytano dane: %d wydarzeń, %d wiadomości.", len(events), len(messages))
    except Exception as e:
        log.exception("Błąd wczytywania danych: %s", e)
        events, messages = {}, {}

# ---- Cleanup ----
async def cleanup_old_events():
    now = datetime.datetime.utcnow()
    to_delete = []
    for mid, data in list(messages.items()):
        channel = bot.get_channel(data["channel_id"])
        if not channel:
            continue
        try:
            msg = await channel.fetch_message(mid)
        except Exception:
            continue
        age = now - msg.created_at.replace(tzinfo=None)
        if age.days >= 7:
            try:
                await msg.delete()
            except Exception:
                pass
            events.pop(data["nazwa"], None)
            to_delete.append(mid)
    for mid in to_delete:
        messages.pop(mid, None)
    if to_delete:
        save_data()

# ---- Commands ----
@bot.command(name="wydarzenie")
@commands.has_role("Stowarzyszenie")
async def wydarzenie(ctx: commands.Context, nazwa: str, *godziny: str):
    if not godziny:
        await ctx.send("❌ Podaj godziny.")
        return
    events[nazwa] = {g: [] for g in godziny}
    embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
    for i, godzina in enumerate(godziny, start=1):
        embed.add_field(name=f"{i}\u20e3 {godzina}", value="brak zapisanych", inline=True)
    msg = await ctx.send(embed=embed)
    for i in range(len(godziny)):
        await msg.add_reaction(f"{i+1}\u20e3")
    messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": msg.channel.id}
    save_data()

@bot.command(name="rolemsg")
@commands.has_permissions(administrator=True)
async def rolemsg(ctx: commands.Context):
    embed = discord.Embed(
        title="🎭 Wybierz swoje role",
        description="Kliknij emoji, aby przypisać lub usunąć rolę.",
        color=discord.Color.blue()
    )
    for emoji, cfg in ROLE_CONFIG.items():
        embed.add_field(
            name=f"{emoji} {cfg['role']}",
            value=cfg["desc"],
            inline=False
        )

    # Szukaj istniejącej wiadomości z nazwą 'rolemsg'
    existing_msg_id = None
    for mid, data in messages.items():
        if data.get("nazwa") == "rolemsg":
            existing_msg_id = mid
            break

    if existing_msg_id:
        channel = bot.get_channel(messages[existing_msg_id]["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(existing_msg_id)
                await msg.edit(embed=embed)
                await ctx.send("✅ Zaktualizowano istniejącą wiadomość z rolami.")
            except Exception as e:
                await ctx.send(f"⚠️ Nie udało się edytować wiadomości: {e}")
    else:
        msg = await ctx.send(embed=embed)
        for emoji in ROLE_CONFIG.keys():
            await msg.add_reaction(emoji)
        messages[msg.id] = {"nazwa": "rolemsg", "godziny": (), "channel_id": msg.channel.id}
        save_data()
        await ctx.send("✅ Utworzono nową wiadomość z rolami.")

@bot.command(name="john")
async def john(ctx: commands.Context):
    await ctx.send("Żyję, niech kamienie prowadzą Cię do wygranej")

@bot.command(name="autoevent")
@commands.has_permissions(administrator=True)
async def force_auto_event(ctx: commands.Context, *, arg: str = None):
    """
    Ręczne wywołanie auto_events lub scheduled_events.
    Możesz podać nazwę dnia ("środa", "sobota") albo nazwę wydarzenia z SCHEDULED_EVENTS.
    """
    await cleanup_old_events()
    channel = bot.get_channel(AUTO_EVENTS_CHANNEL_ID)
    if not channel:
        await ctx.send("❌ Nie znaleziono kanału do auto_events.")
        return

    created = False

    # 1️⃣ Sprawdź AUTO_EVENTS po nazwie dnia
    if arg:
        weekday = weekday_from_name(arg)
        if weekday is not None and weekday in AUTO_EVENTS:
            base_name, godziny = AUTO_EVENTS[weekday]
            nazwa = f"{base_name} – {next_weekday_date(weekday)}"
            events[nazwa] = {g: [] for g in godziny}
            embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
            for i, g in enumerate(godziny, start=1):
                embed.add_field(name=f"{i}\u20e3 {g}", value="brak zapisanych", inline=True)
            msg = await channel.send(embed=embed)
            for i in range(len(godziny)):
                await msg.add_reaction(f"{i+1}\u20e3")
            messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": channel.id}
            save_data()
            await ctx.send(f"✅ Utworzono testowe auto-wydarzenie: {nazwa}")
            created = True

    # 2️⃣ Sprawdź SCHEDULED_EVENTS po nazwie
    if not created and arg:
        for sched in SCHEDULED_EVENTS:
            if arg.lower() in sched["name"].lower():
                base_name, godziny = sched["name"], sched["times"]
                weekday = weekday_from_name(base_name)
                if weekday is not None:
                    nazwa = f"{base_name} – {next_weekday_date(weekday)}"
                else:
                    nazwa = base_name
                events[nazwa] = {g: [] for g in godziny}
                embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
                for i, g in enumerate(godziny, start=1):
                    embed.add_field(name=f"{i}\u20e3 {g}", value="brak zapisanych", inline=True)
                msg = await channel.send(embed=embed)
                for i in range(len(godziny)):
                    await msg.add_reaction(f"{i+1}\u20e3")
                messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": channel.id}
                save_data()
                await ctx.send(f"✅ Utworzono testowe scheduled-wydarzenie: {nazwa}")
                created = True
                break

    # 3️⃣ Jeśli nie podano argumentu – bieżący dzień z AUTO_EVENTS
    if not created and not arg:
        now = datetime.datetime.utcnow()
        if now.weekday() in AUTO_EVENTS:
            base_name, godziny = AUTO_EVENTS[now.weekday()]
            nazwa = f"{base_name} – {next_weekday_date(now.weekday())}"
            events[nazwa] = {g: [] for g in godziny}
            embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
            for i, g in enumerate(godziny, start=1):
                embed.add_field(name=f"{i}\u20e3 {g}", value="brak zapisanych", inline=True)
            msg = await channel.send(embed=embed)
            for i in range(len(godziny)):
                await msg.add_reaction(f"{i+1}\u20e3")
            messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": channel.id}
            save_data()
            await ctx.send(f"✅ Utworzono testowe wydarzenie (dzisiejszy dzień): {nazwa}")
            created = True

    if not created:
        await ctx.send("❌ Nie znalazłem wydarzenia do utworzenia. Sprawdź nazwę dnia lub wydarzenia.")


# ---- Event updating ----
async def update_event_message(message_id: int):
    if message_id not in messages:
        return
    entry = messages[message_id]
    nazwa, godziny, channel_id = entry["nazwa"], entry["godziny"], entry["channel_id"]
    channel = bot.get_channel(channel_id)
    msg = await channel.fetch_message(message_id)

    embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
    for i, godzina in enumerate(godziny, start=1):
        osoby = events.get(nazwa, {}).get(godzina, [])
        if osoby:
            num = len(osoby)
            pelne = num // 4
            brak = 4 - (num % 4) if num % 4 != 0 else 0
            info = f"🪑 Stoły: {pelne} (pełne)" if brak == 0 else f"🪑 Stoły: {pelne}, ❗ Brakuje {brak} do stołu"
            value = f"{'\n'.join(osoby)}\n{info}"
        else:
            value = "brak zapisanych"
        embed.add_field(name=f"{i}\u20e3 {godzina}", value=value, inline=True)

    await msg.edit(embed=embed)

# ---- Reaction add/remove ----
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild and str(payload.emoji) in ROLE_CONFIG:
        member = await safe_get_member(guild, payload.user_id)
        role = discord.utils.get(guild.roles, name=ROLE_CONFIG[str(payload.emoji)]["role"])
        if member and role:
            await member.add_roles(role)
            return
    if payload.message_id in messages:
        entry = messages[payload.message_id]
        index = emoji_to_index(str(payload.emoji))
        if index is not None and 0 <= index < len(entry["godziny"]):
            godzina = entry["godziny"][index]
            member = await safe_get_member(guild, payload.user_id)
            if member:
                display = member.display_name
                if display not in events[entry["nazwa"]][godzina]:
                    events[entry["nazwa"]][godzina].append(display)
                    save_data()
                    await update_event_message(payload.message_id)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild and str(payload.emoji) in ROLE_CONFIG:
        member = await safe_get_member(guild, payload.user_id)
        role = discord.utils.get(guild.roles, name=ROLE_CONFIG[str(payload.emoji)]["role"])
        if member and role:
            await member.remove_roles(role)
            return
    if payload.message_id in messages:
        entry = messages[payload.message_id]
        index = emoji_to_index(str(payload.emoji))
        if index is not None and 0 <= index < len(entry["godziny"]):
            godzina = entry["godziny"][index]
            member = await safe_get_member(guild, payload.user_id)
            if member:
                display = member.display_name
                if display in events[entry["nazwa"]][godzina]:
                    events[entry["nazwa"]][godzina].remove(display)
                    save_data()
                    await update_event_message(payload.message_id)

# ---- Auto events loop ----
@tasks.loop(minutes=60)
async def auto_events():
    await cleanup_old_events()
    now = datetime.datetime.utcnow()

    # Proste tygodniowe AUTO_EVENTS
    if now.hour == AUTO_EVENTS_HOUR and now.weekday() in AUTO_EVENTS:
        channel = bot.get_channel(AUTO_EVENTS_CHANNEL_ID)
        base_name, godziny = AUTO_EVENTS[now.weekday()]
        nazwa = f"{base_name} – {next_weekday_date(now.weekday())}"
        events[nazwa] = {g: [] for g in godziny}
        embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
        for i, g in enumerate(godziny, start=1):
            embed.add_field(name=f"{i}\u20e3 {g}", value="brak zapisanych", inline=True)
        msg = await channel.send(embed=embed)
        for i in range(len(godziny)):
            await msg.add_reaction(f"{i+1}\u20e3")
        messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": channel.id}
        save_data()

    # Zaawansowany scheduler
    for sched in SCHEDULED_EVENTS:
        start_date = datetime.datetime.strptime(sched["start_date"], "%Y-%m-%d")
        delta_days = (now.date() - start_date.date()).days
        if delta_days >= 0 and delta_days % sched["interval_days"] == 0 and now.hour == sched["hour"]:
            channel = bot.get_channel(AUTO_EVENTS_CHANNEL_ID)
            base_name, godziny = sched["name"], sched["times"]
            weekday = start_date.weekday()
            nazwa = f"{base_name} – {next_weekday_date(weekday)}"
            events[nazwa] = {g: [] for g in godziny}
            embed = discord.Embed(title=f"📅 Wydarzenie: {nazwa}", description="Kliknij emoji, aby się zapisać.")
            for i, g in enumerate(godziny, start=1):
                embed.add_field(name=f"{i}\u20e3 {g}", value="brak zapisanych", inline=True)
            msg = await channel.send(embed=embed)
            for i in range(len(godziny)):
                await msg.add_reaction(f"{i+1}\u20e3")
            messages[msg.id] = {"nazwa": nazwa, "godziny": tuple(godziny), "channel_id": channel.id}
            save_data()

# ---- Keep-alive ----
async def handle_ping(request):
    return web.Response(text="pong")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()

# ---- Startup ----
@bot.event
async def on_ready():
    log.info("Zalogowano jako %s", bot.user)
    if not auto_events.is_running():
        auto_events.start()

async def _main():
    load_data()
    asyncio.create_task(start_webserver())
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(_main())
