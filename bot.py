import os
import json
import asyncio
from datetime import datetime
from supabase import create_client, Client
from discord import Intents, Embed, Color
from discord.ext import commands
from discord.app_commands import describe
from discord.http import HTTPClient
import httpx
from flask import Flask
import threading

# === ENV ===
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_PROXY = os.environ.get("DISCORD_PROXY")

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not set")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
if not DISCORD_CHANNEL_ID:
    raise ValueError("DISCORD_CHANNEL_ID must be set")
DISCORD_CHANNEL_ID = int(DISCORD_CHANNEL_ID)

print(f"[Proxy] Using proxy: {DISCORD_PROXY}")

# === Patch discord.py HTTP client with proxy ===
original_request = HTTPClient.request

async def proxied_request(self, route, **kwargs):
    if DISCORD_PROXY:
        kwargs['proxy'] = DISCORD_PROXY
    return await original_request(self, route, **kwargs)

HTTPClient.request = proxied_request
print("[Proxy] HTTPClient patched.")

# === Flask health check ===
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()

# === Supabase Client ===
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Discord Bot ===
intents = Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# === Helpers ===
async def send_discord_embed(title, description, fields, color=0x00FF00, thumbnail=None):
    if not DISCORD_CHANNEL_ID:
        return
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return
    embed = Embed(title=title, description=description, color=color, timestamp=datetime.now())
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    for field in fields:
        embed.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
    await channel.send(embed=embed)

# === Supabase Realtime Listener ===
async def handle_event(payload):
    if payload["event_type"] != "INSERT":
        return
    record = payload["new"]
    event_type = record.get("event_type")
    if event_type == "join":
        await send_discord_embed(
            title=f"🎉 {record.get('player_name')} joined",
            description="Player entered the server",
            fields=[
                {"name": "User ID", "value": str(record.get("user_id")), "inline": True},
                {"name": "Time", "value": record.get("created_at")[:19], "inline": True},
            ],
            color=0x00FF00
        )
    elif event_type == "leave":
        await send_discord_embed(
            title=f"👋 {record.get('player_name')} left",
            description="Player disconnected",
            fields=[
                {"name": "User ID", "value": str(record.get("user_id")), "inline": True},
                {"name": "Time", "value": record.get("created_at")[:19], "inline": True},
            ],
            color=0xFF0000
        )
    elif event_type == "round_end":
        top = record.get("top_killer") or "No one"
        kills = record.get("top_kills") or 0
        await send_discord_embed(
            title=f"🏁 Round {record.get('round_number')} Ended",
            description=f"Top killer: **{top}** with **{kills}** kills",
            fields=[
                {"name": "Round", "value": str(record.get("round_number")), "inline": True},
                {"name": "Top Killer", "value": top, "inline": True},
                {"name": "Kills", "value": str(kills), "inline": True},
            ],
            color=0xFFA500
        )
    elif event_type == "server_start":
        await send_discord_embed(
            title="🟢 Server Started",
            description="Game server is online",
            fields=[{"name": "Time", "value": record.get("created_at")[:19], "inline": True}],
            color=0x00AAFF
        )
    else:
        await send_discord_embed(
            title=f"📌 {event_type}",
            description=json.dumps(record, indent=2)[:1000],
            fields=[],
            color=0x808080
        )

@bot.event
async def on_ready():
    print(f"✅ Bot logged in as {bot.user}")

    # Self-ping to keep Render alive
    async def self_ping():
        url = "https://your-bot.onrender.com/health"  # Replace with actual URL
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    await client.get(url)
                print("🔄 Self-ping sent")
            except:
                pass
            await asyncio.sleep(300)
    bot.loop.create_task(self_ping())

    # Poll events table
    async def realtime_loop():
        last_id = 0
        while True:
            try:
                res = supabase.table("events").select("*").gt("id", last_id).order("id").execute()
                if res.data:
                    for row in res.data:
                        await handle_event({"event_type": "INSERT", "new": row})
                        last_id = max(last_id, row["id"])
            except Exception as e:
                print(f"Poll error: {e}")
            await asyncio.sleep(5)
    bot.loop.create_task(realtime_loop())

    await bot.tree.sync()
    print("✅ Commands synced")

# === Slash Commands ===
@bot.tree.command(name="status", description="Show all players and their status")
async def status(interaction):
    res = supabase.table("autofarm").select("*").execute()
    data = res.data
    if not data:
        await interaction.response.send_message("No players registered.")
        return
    embed = Embed(title="📊 AutoFarm Status", color=Color.blue())
    for p in data:
        embed.add_field(
            name=p.get("player_name", "Unknown"),
            value=(
                f"**Role:** {p.get('role', 'follow')}\n"
                f"**Round:** {p.get('rounds', 0)}\n"
                f"**Kills:** {p.get('kills', 0)}\n"
                f"**Active:** {'✅' if p.get('active') else '❌'}\n"
                f"**Updated:** {p.get('last_updated', '')[:16]}"
            ),
            inline=True
        )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="toggle", description="Enable/disable autofarm for a player")
@describe(player="Player name", state="on or off")
async def toggle(interaction, player: str, state: str):
    state_bool = state.lower() == "on"
    supabase.table("autofarm").update({"active": state_bool}).eq("player_name", player).execute()
    await interaction.response.send_message(f"✅ {player} set to {'ON' if state_bool else 'OFF'}")

@bot.tree.command(name="set", description="Send a command to a player")
@describe(player="Player name", command="stop | start | kick | round:N")
async def set_command(interaction, player: str, command: str):
    supabase.table("autofarm").update({"command": command}).eq("player_name", player).execute()
    await interaction.response.send_message(f"📨 Sent `{command}` to {player}")

@bot.tree.command(name="kick_all", description="Kick all registered players")
async def kick_all(interaction):
    supabase.table("autofarm").update({"command": "kick"}).neq("player_name", "").execute()
    await interaction.response.send_message("🔨 Sent kick command to all players.")

@bot.tree.command(name="reset", description="Reset rounds for all players")
async def reset(interaction):
    supabase.table("autofarm").update({"rounds": 0}).neq("player_name", "").execute()
    await interaction.response.send_message("🔄 Rounds reset for all active players")

@bot.tree.command(name="register", description="Register a player (add to DB if missing)")
@describe(player="Player name", role="main or alt")
async def register(interaction, player: str, role: str = "alt"):
    is_main = role.lower() == "main"
    data = {
        "player_name": player,
        "is_main": is_main,
        "role": role.lower(),
        "active": True,
        "rounds": 0,
        "kills": 0,
        "command": "",
        "last_updated": datetime.now().isoformat()
    }
    supabase.table("autofarm").upsert(data, on_conflict="player_name").execute()
    await interaction.response.send_message(f"✅ Registered {player} as {role}")

# === Run ===
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
