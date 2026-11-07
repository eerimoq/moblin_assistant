import re
import sys
import errno
import json
import random
import argparse
from hashlib import sha256
from base64 import b64encode
from base64 import b64decode
import asyncio
from aiohttp import web
from aiohttp import MultipartWriter
import aiohttp
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


__author__ = 'Erik Moqvist'
__version__ = '0.22.0'

DEFAULT_PORT = 2345
DEFAULT_PASSWORD = '1234'
API_VERSION = '0.1'
REQUEST_TIMEOUT = 30
RESULT_ERRORS = {
    'unknownRequest': 'request not supported by this Moblin version, try '
                      'updating it',
    'error': 'Moblin failed to perform the request'
}
COLORS = {
    'black': '000000',
    'blue': '0000ff',
    'cyan': '00ffff',
    'gray': '808080',
    'green': '008000',
    'magenta': 'ff00ff',
    'navy': '000080',
    'orange': 'ffa500',
    'purple': '800080',
    'red': 'ff0000',
    'white': 'ffffff',
    'yellow': 'ffff00'
}


def random_string():
    return random.randbytes(64).hex()


def rgb_color(value):
    color = COLORS.get(value.lower(), value)
    match = re.fullmatch(r'#?([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})',
                         color)

    if match is None:
        raise argparse.ArgumentTypeError(
            f"invalid color '{value}', expected RRGGBB or one of "
            f"{', '.join(COLORS)}")

    red, green, blue = [int(component, 16) for component in match.groups()]

    return {'red': red, 'green': green, 'blue': blue}


class Assistant:

    def __init__(self, password):
        self.password = password
        self.challenge = ""
        self.salt = None
        self.identified = False
        self.wrong_password = False
        self.streamer = None
        self.request_id = 0
        self.client_completions = {}
        self.preview_queues = []
        self.event_queues = []

    async def handle_streamer(self, request):
        streamer = web.WebSocketResponse(max_msg_size=0)
        await streamer.prepare(request)

        self.streamer = streamer
        self.challenge = random_string()
        self.salt = random_string()
        self.identified = False
        await self.send_to_streamer({
            'hello': {
                'apiVersion': API_VERSION,
                'authentication': {
                    'challenge': self.challenge,
                    'salt': self.salt
                }
            }
        })

        async for message in streamer:
            if message.type == aiohttp.WSMsgType.TEXT:
                message = json.loads(message.data)

                for kind, data in message.items():
                    if kind == 'identify':
                        await self.handle_identify(data['authentication'])
                    elif kind == 'event':
                        await self.handle_event(data['data'])
                    elif kind == 'response':
                        await self.handle_response(data)
                    elif kind == 'preview':
                        await self.handle_preview(data['preview'])
                    elif kind == 'ping':
                        await self.handle_ping()
                    else:
                        print('Unknown message', message)
            else:
                print('Ignoring', message)

        if self.streamer is streamer:
            self.streamer = None
            self.identified = False

        for client_completion in self.client_completions.values():
            client_completion.put_nowait(None)

        self.client_completions.clear()

        return streamer

    async def send_to_streamer(self, message):
        if self.streamer is None:
            raise Exception('Streamer not connected')

        await self.streamer.send_str(json.dumps(message))

    async def send_request_to_streamer(self, request_id, data):
        await self.send_to_streamer({
            'request': {
                'id': request_id,
                'data': data
            }
        })

    def next_id(self):
        self.request_id += 1

        return self.request_id

    def hash_password(self):
        concatenated = self.password + self.salt
        hash = b64encode(sha256(
            concatenated.encode('utf-8')).digest()).decode('utf-8')
        concatenated = hash + self.challenge

        return b64encode(sha256(
            concatenated.encode('utf-8')).digest()).decode('utf-8')

    async def handle_identify(self, authentication):
        if self.identified:
            result = {'alreadyIdentified': {}}
        elif authentication == self.hash_password():
            self.identified = True
            self.wrong_password = False
            result = {'ok': {}}
        else:
            self.wrong_password = True
            result = {'wrongPassword': {}}

        await self.send_to_streamer({
            'identified': {
                'result': result
            }
        })

        if self.wrong_password:
            print('Streamer sent wrong password')
            await self.streamer.close()
        elif self.identified and self.preview_queues:
            await self.send_preview_request({'startPreview': {}})

    async def handle_event(self, data):
        if not self.identified:
            return

        for queue in self.event_queues:
            queue.put_nowait(data)

    async def handle_response(self, data):
        if not self.identified:
            return

        try:
            request_id = data['id']
            queue = self.client_completions.pop(request_id)
            await queue.put(data)
        except Exception:
            pass

    async def handle_preview(self, preview):
        if not self.identified:
            return

        preview = b64decode(preview)

        for queue in self.preview_queues:
            queue.put_nowait(preview)

    async def handle_ping(self):
        await self.send_to_streamer({
            'pong': {}
        })

    async def handle_client(self, request):
        client = web.WebSocketResponse(max_msg_size=0)
        await client.prepare(request)

        async for message in client:
            try:
                if message.type == aiohttp.WSMsgType.TEXT:
                    message = json.loads(message.data)

                    if message['type'] == 'request':
                        await self.handle_client_request(client, message)
                    else:
                        print('Not supported', message)
                else:
                    print('Ignoring', message)
            except Exception as e:
                print(e)
                break

        return client

    async def send_error_to_client(self, client, message):
        await client.send_str(json.dumps({
            'type': 'error',
            'message': message
        }))

    async def handle_client_request(self, client: web.WebSocketResponse, message):
        if not self.identified:
            if self.wrong_password:
                error = 'the streamer tried to connect with the wrong password'
            else:
                error = 'no streamer connected to the server'

            await self.send_error_to_client(client, error)

            return

        request_id = self.next_id()
        queue = asyncio.Queue()
        self.client_completions[request_id] = queue

        try:
            await self.send_request_to_streamer(request_id, message['data'])
        except ConnectionError:
            del self.client_completions[request_id]
            queue.put_nowait(None)

        data = await queue.get()

        if data is not None:
            await client.send_str(json.dumps({
                'type': 'response',
                'data': data
            }))
        else:
            await self.send_error_to_client(
                client,
                'the streamer disconnected before responding')

    async def handle_events(self, request):
        events = web.WebSocketResponse(max_msg_size=0)
        await events.prepare(request)

        queue = asyncio.Queue()
        self.event_queues.append(queue)
        sender = asyncio.create_task(self.send_events(events, queue))

        try:
            async for message in events:
                print('Ignoring', message)
        finally:
            sender.cancel()
            self.event_queues.remove(queue)

        return events

    async def send_events(self, events, queue):
        while True:
            data = await queue.get()

            try:
                await events.send_str(json.dumps(data))
            except Exception:
                break

    async def handle_preview_mjpeg(self, request):
        response = web.StreamResponse()
        response.headers["content-type"] = "multipart/x-mixed-replace;boundary=image-boundary"
        await response.prepare(request)

        queue = asyncio.Queue()

        try:
            await self.add_preview_reader(queue)

            while True:
                image = await queue.get()

                with MultipartWriter("image/jpeg", boundary="image-boundary") as mpwriter:
                    mpwriter.append(image, {"content-type": "image/jpeg"})

                    try:
                        await mpwriter.write(response, close_boundary=False)
                        await response.write(b"\r\n")
                    except (ConnectionResetError, ConnectionAbortedError):
                        break
        finally:
            await self.remove_preview_reader(queue)

        return response

    async def add_preview_reader(self, queue):
        self.preview_queues.append(queue)

        if len(self.preview_queues) == 1:
            await self.send_preview_request({'startPreview': {}})

    async def remove_preview_reader(self, queue):
        self.preview_queues.remove(queue)

        if len(self.preview_queues) == 0:
            await self.send_preview_request({'stopPreview': {}})

    async def send_preview_request(self, data):
        if not self.identified:
            return

        try:
            await self.send_request_to_streamer(self.next_id(), data)
        except Exception:
            pass


def do_run(args):
    app = web.Application()
    assistant = Assistant(args.password)
    app.add_routes([
        web.get('/', assistant.handle_streamer),
        web.get('/client', assistant.handle_client),
        web.get('/events', assistant.handle_events),
        web.get('/preview', assistant.handle_preview_mjpeg)
    ])

    try:
        web.run_app(app, port=args.port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise Exception(f'port {args.port} already in use')

        raise


def make_client_request(port, data):
    with connect(f'ws://localhost:{port}/client',
                 max_size=None,
                 close_timeout=1) as server:
        server.send(json.dumps({
            'type': 'request',
            'data': data
        }))

        try:
            response = json.loads(server.recv(timeout=REQUEST_TIMEOUT))
        except TimeoutError:
            raise Exception('timed out waiting for a response from the '
                            'streamer')
        except ConnectionClosed:
            raise Exception('the server closed the connection')

    if response['type'] == 'error':
        raise Exception(response['message'])

    data = response['data']
    result = next(iter(data['result']))

    if result != 'ok':
        raise Exception(RESULT_ERRORS.get(result,
                                          f'request failed with {result}'))

    return data


def do_events(args):
    with connect(f'ws://localhost:{args.port}/events', max_size=None) as server:
        for message in server:
            print(message, flush=True)


def get_settings(port):
    data = make_client_request(
        port,
        {
            'getSettings': {
            }
        })

    return data['data']['getSettings']['data']


def find_id(items, kind, name):
    for item in items:
        if item['name'] == name:
            return item['id']

    names = ', '.join(item['name'] for item in items)

    raise Exception(f"unknown {kind} '{name}', expected one of {names}")


def get_stream_id(port, name):
    return find_id(get_settings(port)['streams'], 'stream', name)


def get_scene_id(port, name):
    return find_id(get_settings(port)['scenes'], 'scene', name)


def get_mic_id(port, name):
    return find_id(get_settings(port)['mics'], 'mic', name)


def do_get_settings(args):
    print(json.dumps(get_settings(args.port), indent=4))


def do_import_settings(args):
    with open(args.file, 'rb') as fin:
        data = fin.read()

    make_client_request(
        args.port,
        {
            'importSettings': {
                'data': b64encode(data).decode('utf-8')
            }
        })


def do_get_status(args):
    data = make_client_request(
        args.port,
        {
            'getStatus': {
            }
        })

    print(json.dumps(data['data']['getStatus'], indent=4))


def do_set_zoom(args):
    make_client_request(
        args.port,
        {
            'setZoom': {
                'x': args.level
            }
        })


def do_set_stream(args):
    make_client_request(
        args.port,
        {
            'setStream': {
                'id': get_stream_id(args.port, args.name)
            }
        })


def do_set_scene(args):
    make_client_request(
        args.port,
        {
            'setScene': {
                'id': get_scene_id(args.port, args.name)
            }
        })


def do_set_talkback_mic(args):
    make_client_request(
        args.port,
        {
            'setTalkbackMic': {
                'id': get_mic_id(args.port, args.name)
            }
        })


def do_go_live(args):
    make_client_request(
        args.port,
        {
            'setLive': {
                'on': True
            }
        })


def do_end(args):
    make_client_request(
        args.port,
        {
            'setLive': {
                'on': False
            }
        })


def do_start_recording(args):
    make_client_request(
        args.port,
        {
            'setRecord': {
                'on': True
            }
        })


def do_stop_recording(args):
    make_client_request(
        args.port,
        {
            'setRecord': {
                'on': False
            }
        })


def do_show_tally(args):
    make_client_request(
        args.port,
        {
            'showTally': {
                'message': args.message,
                'background': args.background,
                'foreground': args.foreground
            }
        })


def do_hide_tally(args):
    make_client_request(
        args.port,
        {
            'hideTally': {
            }
        })


class HelpFormatter(argparse.HelpFormatter):

    def _format_action(self, action):
        parts = super()._format_action(action)

        # Do not show the subcommand metavar above the list of subcommands.
        if action.nargs == argparse.PARSER:
            parts = parts.split('\n', 1)[1]

        return parts


def main():
    parser = argparse.ArgumentParser(formatter_class=HelpFormatter)
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--version',
                        action='version',
                        version=__version__,
                        help='Print version information and exit.')
    parser.add_argument('-p', '--port',
                        type=int,
                        default=DEFAULT_PORT,
                        help=f'HTTP server port (default: %(default)s).')

    subparsers = parser.add_subparsers(title='subcommands',
                                       dest='subcommand',
                                       metavar='subcommand')

    subparser = subparsers.add_parser(
        'run',
        help='Start the server that a Moblin streamer connects to.')
    subparser.add_argument('-p', '--password',
                           default=DEFAULT_PASSWORD,
                           help='Server password (default: %(default)s).')
    subparser.set_defaults(func=do_run)

    subparser = subparsers.add_parser(
        'events',
        help='Print events from the streamer until interrupted.')
    subparser.set_defaults(func=do_events)

    subparser = subparsers.add_parser(
        'get_settings',
        help='Print the streamer settings.')
    subparser.set_defaults(func=do_get_settings)

    subparser = subparsers.add_parser(
        'import_settings',
        help='Import settings from given file.')
    subparser.add_argument('file')
    subparser.set_defaults(func=do_import_settings)

    subparser = subparsers.add_parser(
        'get_status',
        help='Print the streamer status.')
    subparser.set_defaults(func=do_get_status)

    subparser = subparsers.add_parser(
        'set_zoom',
        help='Set the zoom level.')
    subparser.add_argument('level', type=float)
    subparser.set_defaults(func=do_set_zoom)

    subparser = subparsers.add_parser(
        'set_stream',
        help='Select the stream with given name.')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_stream)

    subparser = subparsers.add_parser(
        'set_scene',
        help='Select the scene with given name.')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_scene)

    subparser = subparsers.add_parser(
        'set_talkback_mic',
        help='Select the talkback mic with given name.')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_talkback_mic)

    subparser = subparsers.add_parser(
        'go_live',
        help='Start streaming.')
    subparser.set_defaults(func=do_go_live)

    subparser = subparsers.add_parser(
        'end',
        help='Stop streaming.')
    subparser.set_defaults(func=do_end)

    subparser = subparsers.add_parser(
        'start_recording',
        help='Start recording.')
    subparser.set_defaults(func=do_start_recording)

    subparser = subparsers.add_parser(
        'stop_recording',
        help='Stop recording.')
    subparser.set_defaults(func=do_stop_recording)

    subparser = subparsers.add_parser(
        'show_tally',
        help='Show a tally.')
    subparser.add_argument('message',
                           help='Text to show. Empty for border only.')
    subparser.add_argument('-b', '--background',
                           type=rgb_color,
                           default='red',
                           help='Border and text background color as name or '
                           'RRGGBB (default: %(default)s).')
    subparser.add_argument('-f', '--foreground',
                           type=rgb_color,
                           default='white',
                           help='Text color as name or RRGGBB '
                           '(default: %(default)s).')
    subparser.set_defaults(func=do_show_tally)

    subparser = subparsers.add_parser(
        'hide_tally',
        help='Hide the tally.')
    subparser.set_defaults(func=do_hide_tally)

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help(sys.stderr)
        sys.exit(2)

    if args.debug:
        args.func(args)
    else:
        try:
            args.func(args)
        except KeyboardInterrupt:
            sys.exit(130)
        except ConnectionRefusedError:
            port = '' if args.port == DEFAULT_PORT else f'--port {args.port} '
            sys.exit(f'error: no server listening on port {args.port}, start '
                     f"it with 'moblin_assistant {port}run'")
        except BaseException as e:
            sys.exit('error: ' + str(e))


if __name__ == '__main__':
    main()
