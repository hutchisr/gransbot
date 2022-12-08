# A (Shitty) ChatGPT Bot for the fediverse

Will listen for keywords or authors in mentions and respond by passing a seed phrase & status content to ChatGPT

## Setup

Clone the repository and from the root run `poetry install` followed by `poetry run bot` or
install to your local system by running `pip3 install .` and running `bot`.

The script will walk you through the steps for logging into your instance and setting up.
You will need your ChatGPT session token, instructions for obtaining it can be found [here](https://github.com/acheong08/ChatGPT/wiki)

Settings will be saved as config.toml in the current directory.
Conversation ids will be saved to conversations.json on exit for resuming existing conversations.

## Limitations

* 2FA Not Supported
* HTTP requests are sync due to poor planning
* Will probably crash when things go wrong
