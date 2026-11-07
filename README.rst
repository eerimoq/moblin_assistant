About
=====

A Moblin assistant in the terminal.

Installation
============

.. code-block:: python

    pip install moblin_assistant

Example usage
=============

Terminal 1. Start the server that a Moblin streamer can connect to.

.. code-block:: text

   $ moblin_assistant run --password my-password

Terminal 2. Control Moblin streamer.

.. code-block:: text

   $ moblin_assistant set_scene Back
   $ moblin_assistant go_live
   $ moblin_assistant end
   $ moblin_assistant set_zoom 2.5

Show a tally on the Moblin streamer. Colors are given as ``RRGGBB`` or
by name (black, blue, cyan, gray, green, magenta, navy, orange, purple,
red, white or yellow) and default to white text on red. Use an empty
message to only show the border.

.. code-block:: text

   $ moblin_assistant show_tally "ON AIR"
   $ moblin_assistant show_tally PREVIEW --background navy --foreground yellow
   $ moblin_assistant show_tally LIVE --background 8b0000
   $ moblin_assistant show_tally "" --background yellow
   $ moblin_assistant hide_tally

Print events from the Moblin streamer, one JSON object per line, until
interrupted.

.. code-block:: text

   $ moblin_assistant events
   {"log": {"entry": "..."}}

Open http://localhost:2345/preview in a web browser to see the preview.

Events websocket
================

Connect to ``ws://localhost:2345/events`` to receive the events the
Moblin streamer sends to the assistant. Any number of clients may be
connected at the same time, and each of them receives every event as a
text message on the form:

.. code-block:: text

   {"type": "event", "data": {"log": {"entry": "..."}}}
