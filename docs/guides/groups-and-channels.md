# Groups and channels

A bot gets into a group or a channel one way: an admin who may add members adds it, and the
room is told. This guide covers what the bot reads there, how it asks to read more, and what it
can do in a room where it holds an admin badge.

## What the bot reads

In a private chat the bot gets everything the person writes. In a group or channel it gets only
what is meant for it:

- commands: `/start` reaches every bot of the group, `/start@yourbot` only that one, with any of
  the bot's handles;
- mentions: `@yourbot` by any of its handles, or its name picked from the list, which the app
  writes as `@[Weather](uB48213907)`;
- replies to one of its messages, and comments under one of its posts;
- presses of its buttons.

The room can let the bot read **every** message. That is the room's decision, made in the app
by an admin who may add bots, never the bot's or its owner's: it is off when the bot is added,
and nothing in the API turns it on. Every member sees it on the member list and above the
keyboard. When an admin changes it, the bot gets a `my_chat_member` update.

The same rule holds for everything the bot can fetch later: its history, the message a reply
quotes, the reactions it hears about. Only what it was handed, never what it wasn't, not even
after the room lets it read everything.

In an encrypted room the sender's phone applies the rule and seals the message to the bot as
well, so the bot needs a published key to get anything there. Attachments are sealed to the
room's key list, which has the bot in it only when it reads everything.

## Asking to read everything

When an admin adds the bot, it gets `my_chat_member` with the whole room, who added it, its own
seat and `reads_all` set to `False`. If reading every message is what the bot is for, that is
the moment to say so in the room: a short message on why, and what it does with what it reads.

```python
@bot.on_chat_member()
async def seat_changed(change):
    if change.chat.is_private:
        return
    if change.joined and not change.reads_all:
        await bot.send_message(
            change.chat,
            "Hi! I keep the meeting minutes, so I need to read every message here. "
            "An admin can allow it from my entry in the member list. "
            "I keep the minutes for 30 days and share them with nobody.",
        )
    elif change.reads_all_changed:
        state = "read everything here" if change.reads_all else "only get what is meant for me"
        await bot.send_message(change.chat, f"Got it: from now on I {state}.")
```

The bot's seat is also in `chat.me`: `chat.reads_all` says whether the room lets it read
everything, `chat.me.permissions` what it may do as a member, `chat.me.can` the rights of its
badge. `await bot.get_chats()` lists every group and channel the bot is in, each with its seat.

A bot that does not read everything can't mark the room read either (`bot.read_chat` raises
`ReadsAllNeededError`): it would show up as having read people it never saw.

## Who comes and goes

```python
@bot.on_member_update()
async def roster(change):
    if change.joined:
        await bot.send_message(change.chat, f"Welcome, {change.user.full_name}!")


@bot.on_join_request()
async def door(request):
    # somebody asked to come in through a link that needs approval
    await bot.decide_join_request(request.chat, request, approve=request.from_user.username is not None)


@bot.on_chat_updated()
async def room(update):
    print(update.chat.title, "changed:", update.changed)
```

`on_member_update` hears only about people the bot would see on the member list: where the list
is hidden, a bot that is not an admin hears about the admins only. Join requests reach only the
bots with the *add members* right.

## Running the room

With the badge for it, a bot does what an admin does, under the same rules the app applies, and
the room sees it the same way. Each call needs the right the app asks for it, and fails with
`ForbiddenError` without it.

```python
await bot.update_chat(group_id, title="Runners of Milan", slow_mode_seconds=30)
await bot.update_chat(group_id, permissions={"send_media": False})     # laid over what members may do
await bot.update_chat(group_id, description=None)                      # None clears it
await bot.set_chat_photo(group_id, "logo.png")

await bot.update_member(group_id, user_id, role="admin", admin_rights=["delete_messages", "pin_messages"])
await bot.update_member(group_id, user_id, restrictions=["send_messages"], restricted_until=tomorrow)
await bot.remove_member(group_id, user_id, ban=True)
await bot.unban(group_id, user_id)
await bot.purge_member(group_id, user_id)                              # everything they wrote

invite = await bot.create_invite(group_id, label="flyer", max_uses=100, needs_approval=True)
print(invite.url)
await bot.revoke_invite(group_id, invite)
```

In a channel there are the comment switches and the numbers:

```python
await bot.set_comments(channel_id, True)
await bot.set_post_comments(channel_id, post_id, comments_off=True)
stats = await bot.get_channel_stats(channel_id)
print(stats.subscribers, stats.average_views, [p.id for p in stats.top_posts])
```

Rights and permissions go by their names: the attributes of `AdminRights` and
`MemberPermissions` (`"ban_users"`, `"send_media"`), or the API's spelling (`"banUsers"`). A
value the server would refuse is refused before anything is sent, with `ValidationError`.

## What a bot cannot do, on purpose

- **Decide what it reads.** The room does. Nothing here changes `reads_all`, the bot's own seat
  included.
- **Add anybody.** It makes invite links and lets in people who asked, but never puts a person
  in a room.
- **Own a room.** What only the owner decides (the history, the public link, handing the room
  over) isn't the bot's.
- **Read what it wasn't handed**, history from before it arrived included.
- **Edit other people's messages**, a channel's posts excepted, with the right to edit them.
