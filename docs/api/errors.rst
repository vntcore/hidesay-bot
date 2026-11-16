Exceptions
==========

.. currentmodule:: hidesay_bot

Every exception is importable from ``hidesay_bot`` and derives from :exc:`HidesayBotError`, so
``except HidesayBotError`` catches anything the library raises on purpose. The guide is
:doc:`../guides/errors`.

No exception ever contains the token, a passphrase or private key material.

Server error codes
------------------

Each error code of the Bot API has its own class. A code this version does not know is mapped by
its HTTP status (``400`` to :exc:`BadRequestError`, ``404`` to :exc:`NotFoundError`, ``5xx`` to
:exc:`ServerError`, and so on).

======================  ======  ===============================  ============================================
Code                    Status  Exception                        Meaning
======================  ======  ===============================  ============================================
``bad_request``         400     :exc:`BadRequestError`           the body or the query is wrong; ``message``
                                                                 names the field
``wrong_body_kind``     400     :exc:`WrongBodyKindError`        ``text`` into an encrypted chat or
                                                                 ``armored`` into a readable one
``bad_markup``          400     :exc:`BadMarkupError`            the buttons break a rule
``bad_webhook``         400     :exc:`BadWebhookError`           the webhook URL is not acceptable
``chat_is_open``        400     :exc:`ChatIsOpenError`           keys asked for a readable chat
``bad_media``           400     :exc:`BadMediaError`             the files of a send can't make a message
``chat_is_protected``   400     :exc:`ChatIsProtectedError`      a sealed message, or one into an encrypted
                                                                 chat: the bot forwards it itself
``too_large``           413     :exc:`PayloadTooLargeError`      a body over 256 KiB, a file over 50 MB, a
                                                                 picture over the server's limit
``unauthorized``        401     :exc:`AuthenticationError`       missing, revoked, replaced or invalid token
``bot_paused``          403     :exc:`BotPausedError`            the owner paused the bot
``forbidden``           403     :exc:`ForbiddenError`            no right to do this here
``blocked_by_user``     403     :exc:`BlockedByUserError`        the person blocked the bot
``user_gone``           403     :exc:`UserGoneError`             the person closed their account
``reads_all_needed``    403     :exc:`ReadsAllNeededError`       only a bot the room lets read every message
                                                                 may do this
``comments_closed``     403     :exc:`CommentsClosedError`       the channel or the post takes no comments
``not_found``           404     :exc:`EndpointNotFoundError`     no such path: usually a wrong ``api_url``
``chat_not_found``      404     :exc:`ChatNotFoundError`         no such chat, or the bot is not in it
``message_not_found``   404     :exc:`MessageNotFoundError`      no such message
``member_not_found``    404     :exc:`MemberNotFoundError`       not a member, or hidden from the bot
``user_not_found``      404     :exc:`UserNotFoundError`         no such person, or nobody the bot may look up
``username_not_found``  404     :exc:`UsernameNotFoundError`     nobody and no chat answers to that handle
``no_user_key``         404     :exc:`NoUserKeyError`            the person never published a key
``callback_not_found``  404     :exc:`CallbackNotFoundError`     no such button press
``sticker_not_found``   404     :exc:`StickerNotFoundError`      no such sticker, or not one the bot may send
``invite_not_found``    404     :exc:`InviteNotFoundError`       no such invite link in that chat
``request_not_found``   404     :exc:`JoinRequestNotFoundError`  nobody of that ID asked to join
``conflict``            409     :exc:`SendIdConflictError`       the ``sendId`` was used in another chat
``webhook_active``      409     :exc:`WebhookActiveError`        polling while a webhook is set
``callback_answered``   409     :exc:`CallbackAnsweredError`     the press was answered already
``chat_is_public``      409     :exc:`ChatIsPublicError`         a public chat has no invite links
``chat_full``           409     :exc:`ChatFullError`             the chat can't take anybody else
``callback_expired``    410     :exc:`CallbackExpiredError`      more than 60 seconds since the press
``rate_limited``        429     :exc:`RateLimitError`            too many API calls
``flood_wait``          429     :exc:`FloodWaitError`            too many messages into one chat, or overall; also too many handle lookups
``slow_mode``           429     :exc:`SlowModeError`             the group's slow mode
``internal``            500     :exc:`ServerError`               the server failed
``try_again``           503     :exc:`ServerError`               the server can't do it just now: try again
======================  ======  ===============================  ============================================

The base classes
----------------

.. autoexception:: HidesayBotError
   :show-inheritance:

.. autoexception:: ConfigurationError
   :show-inheritance:

.. autoexception:: ValidationError
   :members:
   :show-inheritance:

Answers from the server
-----------------------

.. autoexception:: APIError
   :members:
   :show-inheritance:

.. autoexception:: BadRequestError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: WrongBodyKindError
   :members:
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: BadMarkupError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: BadWebhookError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ChatIsOpenError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: BadMediaError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ChatIsProtectedError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: PayloadTooLargeError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: AuthenticationError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ForbiddenError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: BotPausedError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: BlockedByUserError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: UserGoneError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ReadsAllNeededError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: CommentsClosedError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: NotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: EndpointNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ChatNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: MessageNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: MemberNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: UserNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: UsernameNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: NoUserKeyError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: CallbackNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: StickerNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: InviteNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: JoinRequestNotFoundError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ConflictError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: SendIdConflictError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: WebhookActiveError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: CallbackAnsweredError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ChatIsPublicError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ChatFullError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: CallbackExpiredError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: RateLimitError
   :members:
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: FloodWaitError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: SlowModeError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: ServerError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: UnexpectedResponseError
   :show-inheritance:
   :class-doc-from: class

.. autoexception:: PayloadError
   :show-inheritance:

.. autofunction:: hidesay_bot.errors.error_from_envelope

The connection
--------------

.. autoexception:: NetworkError
   :show-inheritance:

.. autoexception:: RequestTimeoutError
   :show-inheritance:

OpenPGP
-------

.. autoexception:: CryptoError
   :show-inheritance:

.. autoexception:: InvalidKeyError
   :show-inheritance:

.. autoexception:: PassphraseError
   :show-inheritance:

.. autoexception:: EncryptionError
   :show-inheritance:

.. autoexception:: EncryptionRequiredError
   :show-inheritance:

.. autoexception:: DecryptionError
   :show-inheritance:

.. autoexception:: NotARecipientError
   :show-inheritance:

.. autoexception:: SignatureError
   :show-inheritance:

.. autoexception:: KeyChangedError
   :members:
   :show-inheritance:

.. autoexception:: MediaIntegrityError
   :members:
   :show-inheritance:

Webhooks, polling and storage
-----------------------------

.. autoexception:: WebhookError
   :members:
   :show-inheritance:

.. autoexception:: PollingError
   :show-inheritance:

.. autoexception:: StorageError
   :show-inheritance:

.. autoexception:: PollingConflictError
   :show-inheritance:

Web-Bots
--------

.. autoexception:: WebBotDataError
   :members:
   :show-inheritance:
