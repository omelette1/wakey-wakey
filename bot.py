import os
import re
import asyncio

import discord
import dns.asyncresolver
from discord import app_commands
from dotenv import load_dotenv
from mcstatus import JavaServer


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MC_HOST = os.getenv("MINECRAFT_HOST", "").strip()
MC_PORT = int(os.getenv("MINECRAFT_PORT", "43960"))
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

CHECK_INTERVAL = 5
REQUEST_TIMEOUT = 3
CONFIRM_CHECKS = 1


# ============================================================
# DISCORD SETUP
# ============================================================

intents = discord.Intents.default()

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

was_online = None
monitor_task = None


# ============================================================
# STATUS HELPERS
# ============================================================

def clean_version(version):
    """Remove Minecraft formatting codes."""
    return re.sub(r"§.", "", version).strip()


def classify_version(version):
    """
    True  = online
    False = explicitly offline
    None  = starting/loading/unknown
    """
    version = clean_version(version).lower()

    if any(word in version for word in (
        "starting",
        "loading",
        "preparing",
        "queue",
        "waiting",
    )):
        return None

    if version.startswith("●") or "offline" in version:
        return False

    return True


async def tcp_is_reachable(host, port):
    writer = None

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=3,
        )
        return True

    except (OSError, asyncio.TimeoutError):
        return False

    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def get_server_status():
    """
    Check the Minecraft server.

    Returns:
        True  -> online
        False -> explicitly confirmed offline
        None  -> unknown / starting / unreachable
    """
    if not MC_HOST:
        print("[ERROR] MINECRAFT_HOST is missing.")
        return None

    resolver = dns.asyncresolver.Resolver()
    resolver.cache = None
    resolver.timeout = 2
    resolver.lifetime = 3

    endpoints = []
    saw_offline = False
    saw_unknown = False
    saw_starting = False

    srv_name = f"_minecraft._tcp.{MC_HOST}"

    try:
        records = await resolver.resolve(srv_name, "SRV")
        records = sorted(records, key=lambda record: record.priority)

        for record in records:
            endpoint = (
                str(record.target).rstrip("."),
                int(record.port),
            )

            if endpoint not in endpoints:
                endpoints.append(endpoint)

            print(
                f"[DNS SRV] {srv_name} -> "
                f"{endpoint[0]}:{endpoint[1]}"
            )

    except Exception as error:
        print(f"[DNS SRV] Lookup failed: {error}")

    # Keep the configured endpoint as a fallback.
    fallback = (MC_HOST, MC_PORT)

    if fallback not in endpoints:
        endpoints.append(fallback)

    for host, port in endpoints:
        try:
            server = JavaServer(
                host,
                port,
                timeout=REQUEST_TIMEOUT,
            )

            status = await server.async_status()
            version = clean_version(status.version.name)
            result = classify_version(version)

            if result is True:
                print(
                    f"[ONLINE] {host}:{port} | "
                    f"Version: {version} | "
                    f"Players: {status.players.online}/"
                    f"{status.players.max} | "
                    f"Latency: {status.latency:.0f} ms"
                )
                return True

            if result is False:
                print(f"[OFFLINE] {host}:{port}: {version}")
                saw_offline = True

            else:
                print(
                    f"[STARTING/UNKNOWN] "
                    f"{host}:{port}: {version}"
                )
                saw_starting = True

        except Exception as error:
            print(
                f"[PING FAILED] {host}:{port} | "
                f"{type(error).__name__}: {error}"
            )

            if await tcp_is_reachable(host, port):
                print(
                    f"[TCP REACHABLE] {host}:{port} | "
                    "Status ping failed; possible online state."
                )
                return True

            saw_unknown = True

    if saw_starting:
        print("[STATUS] Aternos is still starting/loading.")
        return None


    if saw_offline and not saw_unknown:
        print("[STATUS] OFFLINE confirmed by Aternos.")
        return False

    print("[STATUS] UNKNOWN / UNREACHABLE.")
    return None


# ============================================================
# INITIAL STATUS
# ============================================================

async def print_initial_server_status():
    print("\n" + "=" * 55)
    print("INITIAL MINECRAFT SERVER CHECK")
    print("=" * 55)
    print(f"Hostname: {MC_HOST}")
    print(f"Configured port: {MC_PORT}")

    result = await get_server_status()

    if result is True:
        print("Initial result: ONLINE")
    elif result is False:
        print("Initial result: OFFLINE")
    else:
        print("Initial result: UNKNOWN / STARTING")

    print("=" * 55 + "\n")
    return result


# ============================================================
# DISCORD NOTIFICATIONS
# ============================================================

async def get_notification_channel():
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except discord.HTTPException as error:
            print(f"[DISCORD ERROR] Cannot access channel: {error}")
            return None

    return channel


async def send_online_notification():
    channel = await get_notification_channel()

    if channel is None:
        return False

    try:
        await channel.send(
            "@everyone 🎮 Minecraft server is ON!",
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print("[NOTIFICATION] Online announcement sent.")
        return True

    except discord.HTTPException as error:
        print(f"[DISCORD ERROR] Online announcement failed: {error}")
        return False


async def send_offline_notification():
    channel = await get_notification_channel()

    if channel is None:
        return False

    try:
        await channel.send("🔴 Minecraft server is OFF.")
        print("[NOTIFICATION] Offline announcement sent.")
        return True

    except discord.HTTPException as error:
        print(f"[DISCORD ERROR] Offline announcement failed: {error}")
        return False


# ============================================================
# /STATUS COMMAND
# ============================================================

@tree.command(
    name="status",
    description="Check the Minecraft server status.",
)
async def status_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    result = await get_server_status()

    if result is True:
        message = "🟢 Minecraft server is ON!"
    elif result is False:
        message = "🔴 Minecraft server is OFF."
    else:
        message = "🟡 Minecraft server is STARTING or UNKNOWN."

    await interaction.followup.send(message, ephemeral=True)


# ============================================================
# BACKGROUND MONITOR
# ============================================================

async def check_minecraft():
    global was_online

    candidate_status = None
    candidate_count = 0

    while not client.is_closed():
        result = await get_server_status()

        if result is None:
            print(
                "[MONITOR] Unknown state; "
                "preserving last known state."
            )
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        if was_online is None:
            if result is True:
                if await send_online_notification():
                    was_online = True
            else:
                was_online = False
                print("[MONITOR] Offline baseline established.")

            await asyncio.sleep(CHECK_INTERVAL)
            continue

        # No transition.
        if result == was_online:
            candidate_status = None
            candidate_count = 0
            await asyncio.sleep(CHECK_INTERVAL)
            continue

        if candidate_status == result:
            candidate_count += 1
        else:
            candidate_status = result
            candidate_count = 1

        print(
            f"[CONFIRM] "
            f"{'ONLINE' if result else 'OFFLINE'} "
            f"{candidate_count}/{CONFIRM_CHECKS}"
        )

        if candidate_count >= CONFIRM_CHECKS:
            if result is True:
                sent = await send_online_notification()
            else:
                sent = await send_offline_notification()

            if sent:
                was_online = result
                candidate_status = None
                candidate_count = 0

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# STARTUP
# ============================================================

@client.event
async def on_ready():
    global monitor_task, was_online

    print(f"\nLogged in as {client.user}")

    try:
        synced = await tree.sync()
        print(f"[DISCORD] Synced {len(synced)} slash command(s).")

    except discord.HTTPException as error:
        print(f"[DISCORD ERROR] Command sync failed: {error}")

    if monitor_task is None or monitor_task.done():
        was_online = await print_initial_server_status()

        monitor_task = asyncio.create_task(check_minecraft())

        print("[MONITOR] Background monitoring started.\n")


async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is missing from .env")

    if not MC_HOST:
        raise ValueError("MINECRAFT_HOST is missing from .env")

    if CHANNEL_ID == 0:
        raise ValueError("DISCORD_CHANNEL_ID is missing from .env")

    async with client:
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
