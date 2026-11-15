Keys and encryption
===================

.. currentmodule:: hidesay_bot

What the bot does with these is explained in :doc:`../CRYPTOGRAPHY`; the steps are in
:doc:`../guides/openpgp`. :class:`BotKey`, :class:`PublicKey`, :class:`Decrypted` and
:func:`format_fingerprint` are also importable from ``hidesay_bot.crypto``; :class:`Verification`
and :class:`SignatureStatus` only from ``hidesay_bot``.

.. autoclass:: BotKey
   :members:

.. autoclass:: PublicKey
   :members:

.. autoclass:: Decrypted
   :members:

.. autoclass:: Verification
   :members:

.. autoclass:: SignatureStatus

.. autofunction:: format_fingerprint
