import sys
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
from websockets.sync.client import connect


__author__ = 'Erik Moqvist'
__version__ = '0.18.0'

DEFAULT_PORT = 2345
API_VERSION = '0.1'


def random_string():
    return random.randbytes(64).hex()


class Assistant:

    def __init__(self, password):
        self.password = password
        self.challenge = ""
        self.salt = None
        self.identified = False
        self.streamer = None
        self.request_id = 0
        self.client_completions = {}
        self.preview_queues = []
        self.event_queues = []

    async def handle_streamer(self, request):
        self.streamer = web.WebSocketResponse(max_msg_size=0)
        await self.streamer.prepare(request)

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

        async for message in self.streamer:
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

        for client_completion in self.client_completions.values():
            client_completion.put_nowait(None)

        self.client_completions.clear()

        return self.streamer

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
            result = {'ok': {}}
        else:
            self.identified = False
            result = {'wrongPassword': {}}

        await self.send_to_streamer({
            'identified': {
                'result': result
            }
        })

        if self.identified and self.preview_queues:
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

    async def handle_client_request(self, client: web.WebSocketResponse, message):
        request_id = self.next_id()
        queue = asyncio.Queue()
        self.client_completions[request_id] = queue
        await self.send_request_to_streamer(request_id, message['data'])
        data = await queue.get()
        if data is not None:
            await client.send_str(json.dumps({
                'type': 'response',
                'data': data
            }))
        else:
            await client.close()

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
    web.run_app(app, port=args.port)


def make_client_request(port, data):
    with connect(f'ws://localhost:{port}/client', max_size=None) as server:
        server.send(json.dumps({
            'type': 'request',
            'data': data
        }))

        return json.loads(server.recv())['data']


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


def get_stream_id(port, name):
    settings = get_settings(port)

    for stream in settings['streams']:
        if stream['name'] == name:
            return stream['id']
    else:
        raise Exception(f'Unknown stream {name}')


def get_scene_id(port, name):
    settings = get_settings(port)

    for scene in settings['scenes']:
        if scene['name'] == name:
            return scene['id']
    else:
        raise Exception(f'Unknown scene {name}')


def get_mic_id(port, name):
    settings = get_settings(port)

    for mic in settings['mics']:
        if mic['name'] == name:
            return mic['id']
    else:
        raise Exception(f'Unknown mic {name}')


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
                'x': float(args.level)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--version',
                        action='version',
                        version=__version__,
                        help='Print version information and exit.')
    parser.add_argument('--port',
                        type=int,
                        default=DEFAULT_PORT,
                        help=f'HTTP server port (default: %(default)s).')

    subparsers = parser.add_subparsers(title='subcommands',
                                       dest='subcommand')
    subparsers.required = True

    subparser = subparsers.add_parser('run')
    subparser.add_argument('--password', required=True)
    subparser.set_defaults(func=do_run)

    subparser = subparsers.add_parser('events')
    subparser.set_defaults(func=do_events)

    subparser = subparsers.add_parser('get_settings')
    subparser.set_defaults(func=do_get_settings)

    subparser = subparsers.add_parser('import_settings')
    subparser.add_argument('file')
    subparser.set_defaults(func=do_import_settings)

    subparser = subparsers.add_parser('get_status')
    subparser.set_defaults(func=do_get_status)

    subparser = subparsers.add_parser('set_zoom')
    subparser.add_argument('level')
    subparser.set_defaults(func=do_set_zoom)

    subparser = subparsers.add_parser('set_stream')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_stream)

    subparser = subparsers.add_parser('set_scene')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_scene)

    subparser = subparsers.add_parser('set_talkback_mic')
    subparser.add_argument('name')
    subparser.set_defaults(func=do_set_talkback_mic)

    subparser = subparsers.add_parser('go_live')
    subparser.set_defaults(func=do_go_live)

    subparser = subparsers.add_parser('end')
    subparser.set_defaults(func=do_end)

    subparser = subparsers.add_parser('start_recording')
    subparser.set_defaults(func=do_start_recording)

    subparser = subparsers.add_parser('stop_recording')
    subparser.set_defaults(func=do_stop_recording)

    args = parser.parse_args()

    if args.debug:
        args.func(args)
    else:
        try:
            args.func(args)
        except BaseException as e:
            sys.exit('error: ' + str(e))


if __name__ == '__main__':
    main()
