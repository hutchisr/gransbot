import asyncio
import datetime
import hashlib
import html
import json
import logging
import re
import signal
import sys
import termios
from pathlib import Path
from typing import Dict, List, Union
from urllib.parse import urlparse

import requests
import tomlkit
import websockets
import websockets.client
from requests import HTTPError
from asyncChatGPT.asyncChatGPT import Chatbot

logging.basicConfig(level=logging.INFO)


CONFIG_PATH = Path("config.toml")
CONVO_BACKUP = Path("conversations.json")

REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
SCOPES = ["read", "write", "follow", "push", "admin"]

conversations: Dict[str, str] = {}
bot_cache: Dict[str, Chatbot] = {}

bot: Union["Bot", None] = None


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


class Bot:
    """Chat Bot"""

    def __init__(self):
        if not CONFIG_PATH.is_file():
            tomlkit.dump(
                {
                    "domain": input("domain: "),
                    "keywords": input("keywords space separated): ").split(" "),
                    "authors": input("authors (space separated): ").split(" "),
                    "phrase": input(
                        "Optional activation phrase with `{}` to denote conversation input: "
                    ),
                },
                open(CONFIG_PATH, "w"),
            )

        self.config = tomlkit.load(open(CONFIG_PATH))

        if not self.config.get("client_id") or not self.config.get("client_secret"):
            response = requests.post(
                f"https://{self.config['domain']}/api/v1/apps",
                data={
                    "client_name": "gransbot",
                    "scopes": " ".join(SCOPES),
                    "redirect_uris": REDIRECT_URI,
                },
            )
            response.raise_for_status()
            data = response.json()
            logging.info("Client response: %r", data)

            self.config["client_id"] = data["client_id"]
            self.config["client_secret"] = data["client_secret"]

            tomlkit.dump(self.config, open(CONFIG_PATH, "w"))

        if not self.config.get("access_token") or not self.config.get("username"):
            username = input("username: ")
            password = input("password: ")
            response = requests.post(
                f"https://{self.config['domain']}/oauth/token",
                data={
                    "username": username,
                    "password": password,
                    "client_id": self.config["client_id"],
                    "client_secret": self.config["client_secret"],
                    "grant_type": "password",
                    "scopes": " ".join(SCOPES),
                    "redirect_uris": REDIRECT_URI,
                },
            )
            response.raise_for_status()
            data = response.json()
            logging.info("Token response: %r", data)
            self.config["username"] = username
            self.config["access_token"] = data["access_token"]
            self.config["refresh_token"] = data["refresh_token"]
            self.config[
                "token_expires"
            ] = datetime.datetime.utcnow() + datetime.timedelta(
                seconds=data["expires_in"]
            )
            self.config["cookies"] = dict(response.cookies)
            tomlkit.dump(self.config, open(CONFIG_PATH, "w"))

        session_token = self.config.get("chat_gpt", {}).get("session_token")
        if not session_token:
            print(
                "Fedi auth is set up but you will need to configure ChatGPT auth manually: https://github.com/acheong08/ChatGPT/wiki\n"
            )
            session_token = long_input("session token: ")
            self.config["chat_gpt"] = {}
            self.config["chat_gpt"]["session_token"] = session_token
            tomlkit.dump(self.config, open(CONFIG_PATH, "w"))

        self.status_queue = asyncio.Queue(self.config.get("queue_size", 100))
        self.bot = Chatbot(self.config["chat_gpt"])
        if CONVO_BACKUP.is_file():
            self.conversations = json.load(open(CONVO_BACKUP))
        else:
            self.conversations = []
        if isinstance(self.conversations, dict):
            logging.info("Old conversations log, migrating")
            self.conversations = list(self.conversations.keys())

    async def listen(self):
        uri = f"wss://token:{self.config['access_token']}@{self.config['domain']}/api/v1/streaming/?access_token={self.config['access_token']}&stream=user"
        async with websockets.client.connect(
            uri,
        ) as websocket:
            logging.info("Connected to websocket, waiting for statuses.")
            while True:
                message = json.loads(await websocket.recv())
                status = json.loads(message["payload"])
                try:
                    self.status_queue.put_nowait(
                        {"event": message["event"], "status": status}
                    )
                except asyncio.QueueFull:
                    logging.error(
                        "Queue full, dropping status from %s", status["account"]["acct"]
                    )

    async def read_status(self):
        logging.info("Waiting for statuses to be loaded...")
        while True:
            message = await self.status_queue.get()
            event = message.get("event")
            status = message.get("status")
            try:
                if (
                    event == "update"
                    and self.config["username"]
                    in [m["acct"] for m in status.get("mentions", [])]
                    and (
                        status.get("in_reply_to_id") in self.conversations
                        or any(
                            k in status["content"]
                            for k in self.config.get("keywords", [])
                        )
                        or status["account"]["acct"] in self.config.get("authors", [])
                        or urlparse(status["account"]["url"]).netloc
                        in self.config.get("domains", [])
                    )
                ):
                    await self.reply(status)
                elif (
                    event == "notification"
                    and status.get("emoji")
                    and (
                        status["account"]["acct"] in self.config.get("authors", [])
                        or urlparse(status["account"]["url"]).netloc
                        in self.config.get("domains", [])
                    )
                ):
                    await self.reply_notification(status)
                else:
                    continue
                logging.info("Finished replying, wait 10 seconds...")
                await asyncio.sleep(10)
            except Exception:
                logging.exception("Something went wrong while replying")
                await asyncio.sleep(1)

    async def reply(self, status):
        content = strip_name(strip_html(status["content"]), self.config)
        message = (
            await self.bot.get_chat_response(self.config["phrase"].format(content))
        )["message"]
        print(message)
        response = requests.post(
            f"https://{self.config['domain']}/api/v1/statuses",
            headers={
                "Authorization": f"Bearer {self.config['access_token']}",
                "Idempotency-Key": f"{hashlib.sha1(message.encode())}",
            },
            data={
                "status": mentions_string(status, self.config) + message.strip('"'),
                "in_reply_to_id": status["id"],
                "in_reply_to_account_id": status["account"]["id"],
            },
        )
        response.raise_for_status()
        data = response.json()
        self.conversations.append(data["id"])

    async def reply_notification(self, status):
        message = (
            await self.bot.get_chat_response(
                self.config["phrase"].format(status.get("emoji", "👍"))
            )
        )["message"]
        print(message)
        response = requests.post(
            f"https://{self.config['domain']}/api/v1/statuses",
            headers={
                "Authorization": f"Bearer {self.config['access_token']}",
                "Idempotency-Key": f"{hashlib.sha1(message.encode())}",
            },
            data={
                "status": f"@{status['account']['acct']} " + message.strip('"'),
                "in_reply_to_account_id": status["account"]["id"],
                "visibility": "direct",
            },
        )
        response.raise_for_status()


async def main():
    global bot
    bot = Bot()
    while True:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(bot.listen())
                tg.create_task(bot.read_status())
        except Exception:
            logging.exception(
                "Something went wrong while listening to stream. Will try to reconnect in 60 seconds."
            )
            asyncio.sleep(60)


def handle_sigint(signal, frame):
    if bot:
        session_token = bot.bot.config.get("session_token")
        if session_token:
            bot.config["chat_gpt"]["session_token"] = session_token
            tomlkit.dump(bot.config, open(CONFIG_PATH, "w"))
        if bot.conversations:
            json.dump(bot.conversations, open(CONVO_BACKUP, "w"))
    print("Quitting!")
    exit(0)


signal.signal(signal.SIGINT, handle_sigint)

asyncio.run(main())
