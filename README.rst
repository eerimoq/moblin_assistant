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
