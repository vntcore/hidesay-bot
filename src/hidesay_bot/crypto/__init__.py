"""OpenPGP for bots: the bot's key, other people's keys, sealing and opening.

Most bots never use this package directly: `~hidesay_bot.Bot` seals and opens
messages on its own. It is here for the keys (making, loading, saving,
exporting the public part) and for doing the OpenPGP part by hand. It has
four public names, all also importable from ``hidesay_bot``:

- `~hidesay_bot.BotKey`: the bot's own key pair, with the private key.
- `~hidesay_bot.PublicKey`: somebody's public key, to seal to and to check
  signatures against.
- `~hidesay_bot.Decrypted`: an opened message, with the result of its signature
  check.
- `~hidesay_bot.format_fingerprint`: a fingerprint in groups of four.

Two engines do the work: Sequoia PGP (``pysequoia``) seals and opens
messages, and rPGP (``rpgp-py``) makes keys and checks signatures. Nobody has
audited this code independently. The Cryptography page of the documentation
(``docs/CRYPTOGRAPHY.md``) says what the bot does with OpenPGP and which risks
remain.

Example:
    Make the bot's key once, and save it sealed with a passphrase::

        import os
        from hidesay_bot.crypto import BotKey

        key = BotKey.generate("Weather", username="weatherbot")
        key.save("bot-key.asc", passphrase=os.environ["BOT_KEY_PASSPHRASE"])
"""

from .keys import BotKey, Decrypted, PublicKey, format_fingerprint

__all__ = ["BotKey", "Decrypted", "PublicKey", "format_fingerprint"]
