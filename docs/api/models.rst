Models
======

.. currentmodule:: hidesay_bot

The objects the library returns are **frozen dataclasses**: you can read them, compare them and
use them as dictionary keys, but not change them. Python names are in ``snake_case``; the JSON
names of the API are in ``camelCase`` (``from`` becomes ``from_user``).

Every model keeps the JSON it was made from in ``raw``, so a field that a newer server adds and
this version does not know is still there (``message.raw.get("someNewField")``). Each model has
a ``from_json()`` class method to build it from the API's JSON; a missing required field raises
:exc:`PayloadError`. Dates are :class:`~datetime.datetime` objects in UTC.

Enums
-----

The enums are ``str`` enums: a member equals its value (``ChatKind.GROUP == "group"``). A value
that this version does not know does not raise: it becomes a pseudo-member that still equals its
string.

.. autoclass:: ChatKind

.. autoclass:: ChatMode

.. autoclass:: BotStatus

.. autoclass:: SeatStatus

.. autoclass:: UpdateKind

.. autoclass:: HandleKind

Updates
-------

.. autoclass:: Update
   :members:

.. autoclass:: MessageBase
   :members:

.. autoclass:: Message
   :members:
   :show-inheritance:

.. autoclass:: SyncMessage
   :members:
   :show-inheritance:

.. autoclass:: Media
   :members:

.. autoclass:: Sticker
   :members:

.. autoclass:: Forward
   :members:

.. autoclass:: ForwardChat
   :members:

.. autoclass:: LinkCard
   :members:

.. autoclass:: CallbackQueryBase
   :members:

.. autoclass:: CallbackQuery
   :members:
   :show-inheritance:

.. autoclass:: SyncCallbackQuery
   :members:
   :show-inheritance:

.. autoclass:: DeletedMessages
   :members:

.. autoclass:: ReactionUpdate
   :members:

.. autoclass:: ReactionCount
   :members:

.. autoclass:: ChatMemberUpdate
   :members:

.. autoclass:: JoinRequest
   :members:

.. autoclass:: ChatUpdated
   :members:

Messages the bot sent
---------------------

.. autoclass:: SentMessageBase
   :members:

.. autoclass:: SentMessage
   :members:
   :show-inheritance:

.. autoclass:: SyncSentMessage
   :members:
   :show-inheritance:

.. autoclass:: MessageReactions
   :members:

.. autoclass:: Reaction
   :members:

.. autodata:: KEEP
   :no-value:

Files to send
-------------

:class:`InputFile` is what :meth:`Bot.send_media` takes; :class:`MediaIn` is how a file uploaded
with :meth:`AsyncClient.upload_file` is described in the ``media`` of a send into a readable
chat.

.. autoclass:: InputFile
   :members:

.. autoclass:: MediaIn
   :members:

.. autoclass:: UploadedFile
   :members:

Chats and people
----------------

.. autoclass:: ChatRef
   :members:

.. autoclass:: Chat
   :members:
   :show-inheritance:

.. autoclass:: User
   :members:

.. autoclass:: ChatPeer
   :members:
   :show-inheritance:

.. autoclass:: Seat
   :members:

.. autoclass:: Member
   :members:
   :show-inheritance:

.. autoclass:: AdminRights
   :members:

.. autoclass:: MemberPermissions
   :members:

.. autoclass:: ChatMembers
   :members:

.. autoclass:: ChatKeys
   :members:

.. autoclass:: UserKey
   :members:

.. autoclass:: Profile
   :members:
   :show-inheritance:

Running a group or channel
--------------------------

.. autoclass:: Invite
   :members:

.. autoclass:: Ban
   :members:

.. autoclass:: Picture
   :members:

.. autoclass:: Comments
   :members:

.. autoclass:: ChannelStats
   :members:

.. autoclass:: StatsWindow
   :members:

.. autoclass:: GrowthPoint
   :members:

.. autoclass:: PostViews
   :members:

Handles
-------

.. autoclass:: Resolved
   :members:

.. autoclass:: ChatCard
   :members:
   :show-inheritance:

The bot
-------

.. autoclass:: BotInfo
   :members:

.. autoclass:: Limits
   :members:

.. autoclass:: WebhookInfo
   :members:

.. autoclass:: WebhookSummary
   :members:

.. autoclass:: PublishedKey
   :members:

Commands
--------

.. autoclass:: Command
   :members:

.. autoclass:: CommandScope
   :members:
