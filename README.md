# wecom-bot

A callback bot for WeChat Work (企业微信). It receives encrypted messages, decrypts them with the platform's AES scheme, and replies through the WeCom API.

## How it works

- `wecom_crypto.py` implements the WeCom callback crypto: SHA1 signatures and the AES-256-CBC message codec.
- `bot.py` runs the message loop, access-token handling, and the shared-secret-guarded endpoints.
- `af_fetch.py` and `ps_fetch.py` are helper scripts for pulling reference data.

## Configuration

Copy `.env.example` to `.env` and fill in the values. All WeCom credentials and the DeepSeek key are read from the environment, never hardcoded.
