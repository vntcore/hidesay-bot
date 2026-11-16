"""A bot for groups that keeps a tally of who writes the most.

    export HIDESAY_BOT_TOKEN="pgpbot_..."
    python examples/05_group_bot.py

Add the bot to a group from the app, then try /top, /members, /who @somebody
and /leave.

To count messages the bot has to read all of them, and a group never lets a
new bot do that: until an admin allows it from the bot's entry in the member
list, the bot only gets commands, mentions of it, replies to its messages and
presses of its buttons. So the first thing it does in a new group is say why it
wants to read everything, and what it keeps.
"""

from collections import Counter, defaultdict

from hidesay_bot import (
    Bot,
    ForbiddenError,
    NotFoundError,
    SkipHandler,
    UsernameNotFoundError,
    ValidationError,
)
from hidesay_bot import filters as f

bot = Bot()

# chat id -> user id -> messages written. In memory: a restart starts over
tally: defaultdict[str, Counter[str]] = defaultdict(Counter)
names: dict[str, str] = {}

WHY = (
    "Hello everyone! I keep a tally of who writes the most here, and /top shows it. "
    "For that I need to read every message, and a group doesn't let a new bot do that: "
    "an admin can allow it from my entry in the member list. "
    "I keep only how many messages each of you wrote, never what you wrote."
)


@bot.on_chat_member()
async def my_seat(change):
    if not change.chat.is_group:
        return
    if change.joined:
        # a new bot never reads everything: ask, and say why
        await bot.send_message(change.chat, WHY)
    elif change.reads_all_changed and change.reads_all:
        await bot.send_message(change.chat, "Thanks! From now on I count every message.")
    elif change.reads_all_changed:
        tally.pop(change.chat.id, None)
        await bot.send_message(change.chat, "Understood: I stopped counting and forgot the tally.")
    elif change.left:
        tally.pop(change.chat.id, None)


@bot.on_member_update(f.group)
async def newcomer(change):
    if change.joined:
        await bot.send_message(change.chat, f"Welcome, {change.user.mention}!")


@bot.on_message(f.group, priority=10)
async def count(message):
    # tried before every other handler: it counts, then lets them have the message
    chat = await bot.get_chat(message.chat)
    if chat.reads_all and message.from_user:
        tally[chat.id][message.from_user.id] += 1
        names[message.from_user.id] = message.from_user.mention
    raise SkipHandler


@bot.command("help", filters=(f.group,))
async def help_(message):
    await message.reply(
        "/top - who writes the most\n"
        "/members - who is here\n"
        "/who @name - look a handle up\n"
        "/leave - make me leave (admins only)"
    )


@bot.command("top", filters=(f.group,))
async def top(message):
    chat = await bot.get_chat(message.chat)
    if not chat.reads_all:
        await message.reply("I can't count yet: an admin has to let me read every message first.")
        return
    best = tally[chat.id].most_common(5)
    if not best:
        await message.reply("Nothing counted yet.")
        return
    lines = [f"{names.get(user_id, user_id)}: {written}" for user_id, written in best]
    await message.reply("Who wrote the most since I started counting:\n" + "\n".join(lines))


@bot.command("members", filters=(f.group,))
async def members(message):
    try:
        roster = await bot.get_chat_members(message.chat)
    except ForbiddenError:
        await message.reply("I am not allowed to see the member list here.")
        return
    listed = [m.user.mention for m in roster.members]
    await message.reply(f"{roster.member_count} member(s): " + ", ".join(listed[:50]))


@bot.command("who", filters=(f.group,))
async def who(message, command):
    # the one id no update ever hands over: the handle somebody typed
    if not command.args:
        await message.reply("Give me a handle: /who @anna")
        return
    try:
        found = await bot.resolve(command.args.split()[0])
    except ValidationError:
        await message.reply("That is not a handle.")
        return
    except UsernameNotFoundError:
        await message.reply("Nobody and no chat answers to that.")
        return

    if not found.is_user:
        card = found.chat
        await message.reply(f"{found.mention} is a {found.kind} - id {card.id}, {card.member_count} member(s).")
        return
    # a handle is not membership, so ask the roster before saying anything about it
    try:
        seat = await bot.get_chat_member(message.chat, found.id)
        where = f"here as {seat.status}"
    except NotFoundError:
        where = "not in this group"
    await message.reply(f"{found.mention} is user {found.id}, {where}.")


@bot.command("leave", filters=(f.group,))
async def leave(message):
    asker = await bot.get_chat_member(message.chat, message.from_user.id)
    if not asker.is_admin:
        await message.reply("Only an admin can send me away.")
        return
    await message.reply("Bye!")
    await bot.leave_chat(message.chat)


@bot.on_message(f.group & (f.mentioned | f.reply))
async def addressed(message):
    await message.reply(f"You called, {message.from_user.mention}? Try /help.")


@bot.on_deleted_messages()
async def deleted(event):
    print(f"{len(event.message_ids)} message(s) deleted in chat {event.chat.id}")


if __name__ == "__main__":
    bot.run()
