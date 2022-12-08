import json
from pathlib import Path
import logging
import datetime
import asyncio
import re
import html
import hashlib
import signal
import sys
from typing import Dict, List

import tomlkit
import requests
from requests import HTTPError
import websockets
import websockets.client
from revChatGPT.revChatGPT import Chatbot
import termios


logging.basicConfig(level=logging.INFO)


CONFIG_PATH = Path("config.toml")
CONVO_BACKUP = Path("conversations.json")

REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
SCOPES = ["read", "write", "follow", "push", "admin"]

conversations: Dict[str, str] = {}
bot_cache: Dict[str, Chatbot] = {}

def long_input(prompt=""):
    """Get around 4095 character limit in prompt"""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] = new[3] & ~termios.ICANON
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        line = input(prompt)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return line


def strip_html(string):
    # Remove HTML tags using a regular expression
    stripped = re.sub(r"<[^>]*>", "", string)

    # Decode HTML entities
    return html.unescape(stripped)


def strip_name(message, config):
    return re.sub(rf"@?{config['username']}(@{config['domain']})?", "", message)


def mentions_string(status, config):
    s = f"@{status['account']['acct']} "
    for mention in status["mentions"]:
        if mention["acct"] != config["username"]:
            s += f"@{mention['acct']} "
    return s


def setup():
    if not CONFIG_PATH.is_file():
        tomlkit.dump(
            {
                "domain": input("domain: "),
                "keywords": input("keywords space separated): ").split(" "),
                "authors": input("authors (space separated): ").split(" "),
                "phrase": input("Optional activation phrase with `{}` to denote conversation input: "),
            },
            open(CONFIG_PATH, "w"),
        )

    config = tomlkit.load(open(CONFIG_PATH))

    if not config.get("client_id") or not config.get("client_secret"):
        response = requests.post(
            f"https://{config['domain']}/api/v1/apps",
            data={
                "client_name": "gransbot",
                "scopes": " ".join(SCOPES),
                "redirect_uris": REDIRECT_URI,
            },
        )
        response.raise_for_status()
        data = response.json()
        logging.info("Client response: %r", data)
        config["client_id"] = data["client_id"]
        config["client_secret"] = data["client_secret"]
        tomlkit.dump(config, open(CONFIG_PATH, "w"))

    if not config.get("access_token") or not config.get("username"):
        username = input("username: ")
        password = input("password: ")
        response = requests.post(
            f"https://{config['domain']}/oauth/token",
            data={
                "username": username,
                "password": password,
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "grant_type": "password",
                "scopes": " ".join(SCOPES),
                "redirect_uris": REDIRECT_URI,
            },
        )
        response.raise_for_status()
        data = response.json()
        logging.info("Token response: %r", data)
        config["username"] = username
        config["access_token"] = data["access_token"]
        config["refresh_token"] = data["refresh_token"]
        config["token_expires"] = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=data["expires_in"]
        )
        config["cookies"] = dict(response.cookies)
        tomlkit.dump(config, open(CONFIG_PATH, "w"))

    if not config.get("chat_gpt", {}).get("session_token"):
        print(
            "Fedi auth is set up but you will need to configure ChatGPT auth manually: https://github.com/acheong08/ChatGPT/wiki\n"
        )
        session_token = long_input("session token: ")
        config["chat_gtp"] = {}
        config["chat_gtp"]["session_token"] = session_token
        tomlkit.dump(config, open(CONFIG_PATH, "w"))

    return config


async def listener(config):
    uri = f"wss://token:{config['access_token']}@{config['domain']}/api/v1/streaming/?access_token={config['access_token']}&stream=user"
    async with websockets.client.connect(
        uri,
    ) as websocket:
        while True:
            message = json.loads(await websocket.recv())
            if message["event"] == "update":
                status = json.loads(message["payload"])
                # print(message["payload"])
                for mention in status.get("mentions", []):
                    if mention["acct"] == config["username"]:
                        await reply(config, status)


async def reply(config, status):
    content = strip_name(strip_html(status["content"]), config)
    conversation_id = conversations.get(status.get("in_reply_to_id"))
    if (
        not conversation_id
        and not any(k in content for k in config["keywords"])
        and status["account"]["acct"] not in config["authors"]
    ):
        logging.info(
            f"Not existing conversation and no mention of keywords or configured authors, ignored message from {status['account']['acct']}"
        )
        return
    bot = bot_cache.get(conversation_id)
    if not bot:
        bot = Chatbot(config["chat_gpt"], conversation_id=conversation_id)
        bot_cache.update({bot.conversation_id: bot})
    while True:
        try:
            message = bot.get_chat_response(config["phrase"].format(content))["message"]
        except ValueError as err:
            logging.error("%s. Sleeping 30 sec...", err)
            await asyncio.sleep(60)
            continue
        break
    print(message)
    response = requests.post(
        f"https://{config['domain']}/api/v1/statuses",
        headers={
            "Authorization": f"Bearer {config['access_token']}",
            "Idempotency-Key": f"{hashlib.sha1(message.encode())}",
        },
        data={
            "status": mentions_string(status, config) + message.strip('"'),
            "in_reply_to_id": status["id"],
            "in_reply_to_account_id": status["account"]["id"],
        },
    )
    data = response.json()
    conversations[data["id"]] = bot.conversation_id


async def main():
    config = setup()
    if CONVO_BACKUP.is_file():
        convos = json.load(open(CONVO_BACKUP))
        conversations.update(convos)
    print("Waiting for statuses...")
    await listener(config)


def handle_sigint(signal, frame):
    json.dump(conversations, open(CONVO_BACKUP, "w"))
    print("Quitting!")
    exit(0)


signal.signal(signal.SIGINT, handle_sigint)

asyncio.run(main())
