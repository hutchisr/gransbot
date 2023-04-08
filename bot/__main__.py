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
from typing import Dict, List, Optional, Union
from urllib.parse import urlparse

import requests
import tomlkit
import websockets
import websockets.client
from requests import HTTPError
import openai
import tiktoken

logging.basicConfig(level=logging.INFO)


CONFIG_PATH = Path("config.toml")
"""Path to config file"""
CONVO_BACKUP = Path("conversations.json")
"""Path to conversation backup file"""

REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
"""Redirect URI for OAuth2"""
SCOPES = ["read", "write", "follow", "push", "admin"]
"""Scopes for OAuth2"""

DEFAULT_MODEL = "gpt-4"
"""Model to use for OpenAI API"""

MAX_LEN = 1024
"""Maximum length of a message"""

bot: Optional["Bot"] = None


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
    """Removes HTML tags and decodes HTML entities from a string."""
    # Remove HTML tags using a regular expression
    stripped = re.sub(r"<[^>]*>", "", string)

    # Decode HTML entities
    return html.unescape(stripped)


def strip_name(message, config):
    """Removes the bot's name from a message

    This is used to remove the bot's name from a message before sending it to the OpenAI API.

    Args:
        message (str): The message to strip the name from
        config (dict): The bot's config

    Returns:
        str: The message with the bot's name removed
    """
    return re.sub(rf"@?{config['username']}(@{config['domain']})?", "", message)


def mentions_string(status, config):
    """Returns a string of mentions for a status

    Args:
        status (dict): The status to get mentions for
        config (dict): The bot's config

    Returns:
        str: A string of mentions for the status
    """
    s = f"@{status['account']['acct']} "
    for mention in status["mentions"]:
        if mention["acct"] != config["username"]:
            s += f"@{mention['acct']} "
    return s


def num_tokens_from_messages(messages, model="gpt-3.5-turbo-0301"):
    """Returns the number of tokens used by a list of messages.

    Args:
        messages (list): A list of messages to count tokens for.
        model (str): The model to use for counting tokens.

    Returns:
        int: The number of tokens used by the messages.
    """
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    if model == "gpt-3.5-turbo-0301":  # note: future models may deviate from this
        num_tokens = 0
        for message in messages:
            num_tokens += (
                4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            )
            for key, value in message.items():
                num_tokens += len(encoding.encode(value))
                if key == "name":  # if there's a name, the role is omitted
                    num_tokens += -1  # role is always required and always 1 token
        num_tokens += 2  # every reply is primed with <im_start>assistant
        return num_tokens
    else:
        raise NotImplementedError(
            f"num_tokens_from_messages() is not presently implemented for model {model}. "
            "See https://github.com/openai/openai-python/blob/main/chatml.md "
            "for information on how messages are converted to tokens."
        )


class Bot:
    """The bot"""

    def __init__(self):
        if not CONFIG_PATH.is_file():
            domain = input("domain: ")
            keywords = input("keywords (space separated): ")
            authors = input("authors (space separated): ")
            domains = input("domains (space separated): ")
            model = input("model (default: gpt-4): ")
            assistant = input("Instructions: ")
            tomlkit.dump(
                {
                    "domain": domain,
                    "keywords": keywords.split(" ") if keywords else [],
                    "authors": authors.split(" ") if authors else [],
                    "domains": domains.split(" ") if domains else [],
                    "model": model if model else DEFAULT_MODEL,
                    "assistant": assistant
                    if assistant
                    else "You are a helpful assistant."
                    # "phrase": input(
                    #     "Optional activation phrase with `{}` to denote conversation input: "
                    # ),
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

        if not self.config.get("openapi_key"):
            self.config["openapi_key"] = input("OpenAI API key: ")

        openai.api_key = self.config["openapi_key"]

        if CONVO_BACKUP.is_file():
            self.conversations = json.load(open(CONVO_BACKUP))
        else:
            self.conversations = {}

        self.status_queue = asyncio.Queue(self.config.get("queue_size", 100))

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
        """Reads statuses from the queue and responds to them if they are addressed to the bot."""
        logging.info("Waiting for statuses to be loaded...")
        while True:
            message = await self.status_queue.get()
            event = message.get("event")
            status = message.get("status")
            if len(status) > MAX_LEN:
                logging.warning("Status message too long, ignoring")
                continue
            try:
                if (
                    event == "update"
                    and self.config["username"]
                    in [m["acct"] for m in status.get("mentions", [])]
                    and (
                        status.get("in_reply_to_id") in self.conversations
                        or any(
                            k in status["content"].lower()
                            for k in self.config.get("keywords", [])
                        )
                        or status["account"]["acct"] in self.config.get("authors", [])
                        or urlparse(status["account"]["url"]).netloc
                        in self.config.get("domains", [])
                    )
                ):
                    await self.reply(status)
                # elif (
                #     event == "notification"
                #     and status.get("emoji")
                #     and (
                #         status["account"]["acct"] in self.config.get("authors", [])
                #         or urlparse(status["account"]["url"]).netloc
                #         in self.config.get("domains", [])
                #     )
                # ):
                #     await self.reply_notification(status)
                else:
                    continue
                logging.info("Finished replying, wait 10 seconds...")
                await asyncio.sleep(10)
            except Exception:
                logging.exception("Something went wrong while replying")
                await asyncio.sleep(1)

    async def reply(self, status: dict) -> None:
        """Replies to a status with a message from the bot.

        Args:
            status (dict): The status to reply to.
        """
        content = strip_name(strip_html(status["content"]), self.config)
        messages = []
        if "assistant" in self.config:
            messages.append(
                {
                    "role": "system",
                    "content": f"Instructions: {self.config['assistant']}.",
                }
            )
        if status.get("in_reply_to_id") in self.conversations:
            messages.append(
                {
                    "role": "assistant",
                    "content": self.conversations[status["in_reply_to_id"]],
                }
            )
        messages.append({"role": "user", "content": content})
        else:
            try:
                r = openai.ChatCompletion.create(
                    model=self.config.get("model", DEFAULT_MODEL), messages=messages
                )
                message: str = r["choices"][0]["message"]["content"]  # type: ignore
            except openai.InvalidRequestError:
                logging.exception("Invalid request error")
                return
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
        self.conversations[data["id"]] = message

    # async def reply_notification(self, status):
    #     message = "average poast user"
    #     response = requests.post(
    #         f"https://{self.config['domain']}/api/v1/statuses",
    #         headers={
    #             "Authorization": f"Bearer {self.config['access_token']}",
    #             "Idempotency-Key": f"{hashlib.sha1(message.encode())}",
    #         },
    #         data={
    #             "status": f"@{status['account']['acct']} " + message.strip('"'),
    #             "in_reply_to_account_id": status["account"]["id"],
    #             "visibility": "direct",
    #         },
    #     )
    #     response.raise_for_status()


async def main():
    """Main function that runs the bot."""
    global bot
    bot = Bot()
    while True:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(bot.listen())
                tg.create_task(bot.read_status())
        except Exception:
            logging.exception(
                "Something went wrong while listening to stream. Will try to reconnect in 10 seconds."
            )
            await asyncio.sleep(10)


def handle_sigint(signal, frame):
    if not bot:
        exit(1)
    if bot.conversations:
        json.dump(bot.conversations, open(CONVO_BACKUP, "w"))
    tomlkit.dump(bot.config, open(CONFIG_PATH, "w"))
    print("Quitting!")
    exit(0)


signal.signal(signal.SIGINT, handle_sigint)

asyncio.run(main())
