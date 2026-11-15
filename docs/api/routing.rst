Routers and filters
===================

.. currentmodule:: hidesay_bot

:class:`Bot` and :class:`SyncBot` are routers themselves: everything on this page works on a
bot and on a :class:`Router`. The guide is :doc:`../guides/events`.

Router
------

.. autoclass:: Router
   :members:

.. autoexception:: SkipHandler

Commands
--------

.. autoclass:: ParsedCommand
   :members:

Reading the handlers back
-------------------------

:meth:`Router.iter_handlers` and the other readers give back these records. The dispatcher uses
them; in tests they show what a router holds.

.. autoclass:: hidesay_bot.router.Handler
   :members:

.. autoclass:: hidesay_bot.router.ErrorHandler
   :members:

Filters
-------

.. automodule:: hidesay_bot.filters
   :members:
   :member-order: bysource
   :exclude-members: ParsedCommand

.. autoclass:: hidesay_bot.filters.FilterContext
   :members:

.. py:type:: FilterFunc
   :module: hidesay_bot.filters
   :canonical: Callable[..., bool | dict[str, Any] | None | Awaitable[bool | dict[str, Any] | None]]

   What a :class:`~hidesay_bot.filters.Filter` wraps: a function that takes the event, or the event
   and a :class:`~hidesay_bot.filters.FilterContext`, and answers ``True``, ``False``, ``None`` or a
   ``dict``. An ``async`` one answers with an awaitable.
