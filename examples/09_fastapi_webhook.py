"""A webhook bot inside a FastAPI app.

    pip install fastapi uvicorn
    export HIDESAY_BOT_TOKEN="pgpbot_..."
    export WEBHOOK_SECRET="$(python -c 'from hidesay_bot import generate_secret; print(generate_secret())')"
    export WEBHOOK_URL="https://bot.example.org/hidesay"
    uvicorn examples.09_fastapi_webhook:app --host 127.0.0.1 --port 8080

The server sends updates to WEBHOOK_URL, which must be a public https address
that reaches this app (a reverse proxy in front of uvicorn, usually). The app
registers that address and the secret with the server when it starts.

Run it from the project folder, so that `examples` can be imported.
"""

import os

from fastapi import FastAPI

from hidesay_bot import Bot, SQLiteStorage

bot = Bot(storage=SQLiteStorage(os.environ.get("BOT_DB", "webhook-bot.db")))


@bot.on_message()
async def echo(message):
    await message.reply(f"over the webhook: {message.text or '(no text)'}")


webhook = bot.create_webhook_app(secret=os.environ["WEBHOOK_SECRET"], url=os.environ.get("WEBHOOK_URL"))

app = FastAPI(lifespan=bot.lifespan)
# a route, not app.mount(): a mount answers /hidesay with a redirect to /hidesay/,
# and for the server a redirect is a failed delivery
app.add_route("/hidesay", webhook, methods=["POST"])


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": bot.me is not None}
