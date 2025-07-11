import asyncio
import os
import random
import discord
import aiohttp
from aiohttp import web
from colorama import Fore, Style
from dotenv import load_dotenv
load_dotenv()

ROBLOX_GROUP_API = "https://groups.roblox.com/v1/groups/{}"
ROBLOX_GROUP_PAGE = "https://www.roblox.com/groups/group.aspx?gid={}"
RATE_LIMIT = 5  # requests per second
running = False

semaphore = asyncio.Semaphore(RATE_LIMIT)
tasks = []

async def fetch_json(session, url):
    async with semaphore:
        async with session.get(url, timeout=4) as resp:
            resp.raise_for_status()
            return await resp.json()

async def is_group_available(session, group_id):
    try:
        group_data = await fetch_json(session, ROBLOX_GROUP_API.format(group_id))
        return (
            group_data.get("publicEntryAllowed")
            and group_data.get("owner") is None
            and not group_data.get("isLocked", True)
        )
    except Exception as e:
        print(f"{Fore.RED}Error checking group {group_id}: {e}{Style.RESET_ALL}")
        return False

async def notify_discord(webhook, group_id):
    embed = discord.Embed(
        title="Group Found!",
        description=f"[Click here to view the group]({ROBLOX_GROUP_PAGE.format(group_id)})",
        color=discord.Colour.green()
    )
    embed.set_footer(text="RoFinder | By: RXNationGaming")
    await webhook.send(embed=embed)

async def groupfinder_worker(webhook, session):
    while running:
        group_id = random.randint(1000000, 9999999)
        if await is_group_available(session, group_id):
            await notify_discord(webhook, group_id)
            print(f"{Fore.GREEN}[+] Hit: {group_id}{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}[-] Not available: {group_id}{Style.RESET_ALL}")
        await asyncio.sleep(1)

async def start_finder(webhook, session):
    global tasks, running
    if running:
        return "Already running"
    running = True
    tasks = [asyncio.create_task(groupfinder_worker(webhook, session)) for _ in range(5)]
    return "Finder started"

async def stop_finder():
    global running
    running = False
    await asyncio.sleep(1.5)  # wait for tasks to naturally end
    for task in tasks:
        task.cancel()
    return "Finder stopped"

# Web control interface
async def handle_start(request):
    return web.Response(text=await start_finder(request.app["webhook"], request.app["session"]))

async def handle_stop(request):
    return web.Response(text=await stop_finder())

async def handle_status(request):
    return web.Response(text=f"Running: {running}")

async def main():
    webhook_url = os.getenv("DISCORD_WEBHOOK")
    if not webhook_url:
        print("Please set DISCORD_WEBHOOK in your .env file.")
        return

    session = aiohttp.ClientSession()
    webhook = discord.Webhook.from_url(webhook_url, session=session)

    app = web.Application()
    app["webhook"] = webhook
    app["session"] = session

    app.router.add_get("/start", handle_start)
    app.router.add_get("/stop", handle_stop)
    app.router.add_get("/status", handle_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()

    print("Web UI running at http://0.0.0.0:8080")
    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())