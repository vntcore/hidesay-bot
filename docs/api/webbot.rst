Web-Bots
========

.. currentmodule:: hidesay_bot

Checking the ``initData`` a bot's Web-Bot page sends to its server, so the server knows who
the user is. The guide is :doc:`../guides/web-bot`.

.. autofunction:: validate_web_bot_data

.. autofunction:: parse_web_bot_data

.. autoclass:: WebBotData
   :members:

.. autoclass:: WebBotUser
   :members:

.. autodata:: hidesay_bot.webbot.WEB_BOT_DATA_MAX_AGE

.. autofunction:: hidesay_bot.webbot.check_string
