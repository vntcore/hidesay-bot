API reference
=============

.. currentmodule:: hidesay_bot

These pages are generated from the docstrings in the code. Almost everything public is
importable from ``hidesay_bot``:

.. code-block:: python

   from hidesay_bot import Bot, Button, InlineKeyboard, filters as f

A few names live only in their own module. What these pages document in
``hidesay_bot.crypto``, ``hidesay_bot.filters``, ``hidesay_bot.router``,
``hidesay_bot.webhook``, ``hidesay_bot.webbot`` and ``hidesay_bot.errors`` is public as well.
Names that start with ``_`` (modules and attributes) are internal and can change in any
version. The same documentation is available without a browser: ``help(hidesay_bot.Bot)`` in
Python, or ``python -m pydoc hidesay_bot.Bot`` in a terminal.

Where to find what
------------------

=================================  ===================================================================
Page                               Contents
=================================  ===================================================================
:doc:`bot`                         :class:`Bot` and :class:`SyncBot`: running a bot, sending,
                                   encryption helpers, hooks, failed updates
:doc:`client`                      :class:`AsyncClient` and :class:`Client`: one method per
                                   operation of the Bot API; :class:`RetryPolicy`,
                                   :class:`RateLimiter`, :class:`Secret`
:doc:`models`                      the objects the API returns and handlers receive, and
                                   :class:`InputFile`, a file to send
:doc:`keyboard`                    :class:`Button`, :class:`InlineKeyboard`, :class:`KeyboardBuilder`
:doc:`routing`                     :class:`Router`, :class:`SkipHandler`, :class:`ParsedCommand`,
                                   :class:`~hidesay_bot.router.Handler`,
                                   :class:`~hidesay_bot.router.ErrorHandler`, the ``filters``
                                   module and :class:`~hidesay_bot.filters.FilterContext`
:doc:`crypto`                      :class:`BotKey`, :class:`PublicKey`, :class:`Decrypted`,
                                   :class:`Verification`, :class:`SignatureStatus`,
                                   :func:`format_fingerprint`
:doc:`storage`                     :class:`MemoryStorage`, :class:`SQLiteStorage`, writing your own,
                                   :class:`FailedUpdate`
:doc:`webhook`                     :class:`WebhookApp`, :class:`~hidesay_bot.webhook.WebhookResult`,
                                   :func:`generate_secret`,
                                   :data:`~hidesay_bot.webhook.SECRET_HEADER`
:doc:`webbot`                      :func:`validate_web_bot_data`, :func:`parse_web_bot_data`,
                                   :class:`WebBotData`, :class:`WebBotUser`,
                                   :data:`~hidesay_bot.webbot.WEB_BOT_DATA_MAX_AGE`,
                                   :func:`~hidesay_bot.webbot.check_string`
:doc:`errors`                      every exception, with the server's error codes
=================================  ===================================================================

Conventions
-----------

- **Async and sync.** Every async class has a sync twin with the same methods: on the sync
  one, call them without ``await`` (:doc:`../guides/async-and-sync`).
- **Keyword arguments.** Options are keyword-only; only the main values (the chat, the text,
  the IDs) are positional.
- **Chats.** Bot methods take a chat ID or any chat object (a :class:`Chat`, a
  :class:`ChatRef`, ``message.chat``).
- **Validation.** Input is checked against the server's rules before anything is sent: a
  :exc:`ValidationError` means nothing left the process.
- **Errors.** Each method lists the errors specific to it. Any call that reaches the server can
  also raise the errors listed in :ref:`common-errors`.
- **Models are read-only** and keep the server's JSON in ``raw``.
