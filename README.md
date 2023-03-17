# A (Shitty) ChatGPT Bot for the fediverse

Will listen for keywords or authors in mentions and respond by passing a seed phrase & status content to ChatGPT

## Setup

Clone the repository and from the root run `poetry install` followed by `poetry run bot` or
install to your local system by running `pip3 install .` and running `bot`.

The script will walk you through the steps for logging into your instance and setting up.

You will need a paid openai account since this bot now uses the official api/sdk.

Settings will be saved as config.toml in the current directory.

Add `assistant = "Your assistant phrase"` to config.toml to set a custom configuration for the bot's personality.

## Limitations

* 2FA Not Supported
* HTTP requests are sync due to poor planning
* Mastodon doesn't seem to work atm due to handling mentions differently in the streaming API, use pleroma instead.
* Does not currently save conversation history (TBA)
