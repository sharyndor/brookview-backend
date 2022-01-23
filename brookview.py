from pyexpat.errors import messages
from queue import Queue
import select
import socket
from threading import Lock, Thread

from base64 import b64encode
from hashlib import sha1

from http.client import HTTPSConnection
import json

from os import path

from time import sleep, time

PORT = 80

class WebSocket:
    HANDSHAKE  = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: "
    )

    class Opcode:
        CONTINUATION = 0x0
        TEXT         = 0x1
        BINARY       = 0x2
        CLOSE        = 0x8
        PING         = 0x9
        PONG         = 0xA

    def __init__(self, sock : socket.socket):
        self.sock = sock
        self.buffer = bytes()
        self.should_continue = True
        self.messages = Queue()
        self.thread = Thread(target=self.run)
        self.squeue = Queue()
        self.rsock, self.ssock = socket.socketpair()

    def start(self):
        self.thread.start()

    def read_more(self):
        socks = [self.sock, self.rsock]
        rsocks, _, xsocks = select.select(socks, [], socks)

        if self.sock in rsocks:
            self.buffer += self.sock.recv(1024)

        if self.rsock in rsocks:
            _ = self.rsock.recv(1)
            while not self.squeue.empty():
                self.sock.sendall(self.squeue.get())

    def read_some(self, n):
        while len(self.buffer) < n:
            self.read_more()

        result, self.buffer = self.buffer[:n], self.buffer[n:]
        return result
    
    def read_until(self, ending):
        while ending not in self.buffer:
            self.read_more()

        n = self.buffer.find(ending) + len(ending)
        result, self.buffer = self.buffer[:n], self.buffer[n:]
        return result

    def run(self):
        try:
            self.send_handshake(self.read_until(bytes(b'\r\n\r\n')))
            while self.should_continue:
                opcode, message = self.read_message()
                self.handle_message(opcode, message)
        except Exception as e:
            self.should_continue = False
            self.sock.close()
            print(e)

        print('dying')

    def send_handshake(self, request):
        nonce = request.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
        static = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
        key = b64encode(sha1(nonce + static).digest())
        response = WebSocket.HANDSHAKE + key + b'\r\n\r\n'
        self.squeue.put(response)
        self.ssock.sendall(b'\1')
        return None

    def read_message(self):
        header = self.read_some(2)

        # Opcode / Flags - 1 Byte
        opcode = (header[0] >> 0) & 0b1111
        #fin    = (header[0] >> 7) & 0b1
        #rsv1   = (header[0] >> 6) & 0b1
        #rsv2   = (header[0] >> 5) & 0b1
        #rsv3   = (header[0] >> 4) & 0b1

        # Mask / Length - 1 Byte
        length = (header[1] >> 0) & 0b1111111
        mask   = (header[1] >> 7) & 0b1

        # Extended Length - 2 Bytes
        if length == 126:
            header += self.read_some(2)
            length = int.from_bytes(header[2:4], 'big')
        # Extended Length - 8 bytes
        elif length == 127:
            header += self.read_some(8)
            length = int.from_bytes(header[2:10], 'big')

        # Mask - 4 Bytes
        mask_bytes = self.read_some(4) if mask else b'\0\0\0\0'

        # Payload - ? Bytes
        payload = bytes([b ^ mask_bytes[i % 4] for i, b in enumerate(self.read_some(length))])
        return opcode, payload

    def send_message(self, opcode, payload):
        header = bytes()

        # Opcode / Flags
        header += bytes([0x80 + opcode])

        # Mask / Length
        plen = len(payload)
        if plen < 126:
            header += bytes([plen])
        elif plen < 2**16:
            header += bytes([126])
            header += plen.to_bytes(2, 'big')
        elif plen < 2**64:
            header += bytes([127])
            header += plen.to_bytes(8, 'big')
        else:
            raise Exception

        # Mask Omitted

        # Payload
        self.squeue.put(header + payload)
        self.ssock.sendall(b'\1')

    def handle_message(self, opcode, payload):
        if opcode == WebSocket.Opcode.TEXT:
            message = payload.decode()
            self.messages.put(message)
        elif opcode == WebSocket.Opcode.PING:
            self.send_message(WebSocket.Opcode.PONG, payload)
        elif opcode == WebSocket.Opcode.CLOSE:
            self.send_message(WebSocket.Opcode.CLOSE, payload[0:2])
            self.sock.close()
            self.should_continue = False

def find_key_like(o, s):
    result = []
    if type(o) is dict:
        for k, v in o.items():
            if s in k:
                result.append(v)
            else:
                result += find_key_like(v, s)
    elif type(o) is list:
        for n in o:
            result += find_key_like(n, s)
    return result

def json_search(d, t, *kargs):
    if len(kargs) == 0:
        return d if type(d) is t else t()
    else:
        arg = kargs[0]
        if type(arg) is str and type(d) is dict and arg in d:
            return json_search(d[arg], t, *kargs[1:])
        elif type(arg) is int and type(d) is list and arg < len(d):
            return json_search(d[arg], t, *kargs[1:])
        elif callable(arg) and type(d) is list:
            return arg([json_search(n, t, *kargs[1:]) for n in d])
        else:
            return t()

class SocketHandler:
    sockets : list[WebSocket]

    def __init__(self, port):
        self.sockets = []
        self.lock = Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('', port))

        self.history = {}
        if path.exists('history.json'):
            with open('history.json', 'rb') as f:
                if data := f.read():
                    self.history = json.loads(data.decode())

    def start(self):
        Thread(target=self.accept_all).start()

        backlogs : dict[WebSocket, Queue[str]] = {}

        while True:
            didWork = False
            didUpdate = False
            with self.lock:
                self.sockets = [sock for sock in self.sockets if sock.should_continue]
                backlogs = { sock : backlog for sock, backlog in backlogs.items() if sock.should_continue}
                for sock in self.sockets:
                    while not sock.messages.empty():
                        request = json.loads(sock.messages.get())
                        for group in request.values():
                            for item in group.items():
                                if sock not in backlogs:
                                    backlogs[sock] = Queue()
                                backlogs[sock].put(item)
            
            for sock, backlog in backlogs.items():
                if not backlog.empty():
                    item = backlog.get()
                    name, typeVal = item
                    hist_lookup = '='.join(typeVal)
                    response = ''
                    lastUpdateTime = 0
                    if hist_lookup in self.history:
                        lastUpdateTime, response = self.history[hist_lookup]
                    if not response or (time() - lastUpdateTime) > 300:
                        if response := self.process(item):
                            didUpdate = True
                            self.history[hist_lookup] = time(), response
                            sock.send_message(WebSocket.Opcode.TEXT, response.encode())
                            sleep(1)
                    else:
                        sock.send_message(WebSocket.Opcode.TEXT, response.encode())

                    didWork = True
                    break

            if didUpdate:
                with open('history.json', 'wb') as f:
                    f.write(json.dumps(self.history).encode())

            if not didWork:
                sleep(1)

    def accept_all(self):
        self.sock.listen(5)
        while True:
            sock, _ = self.sock.accept()
            websock = WebSocket(sock)
            websock.start()
            with self.lock:
                self.sockets.append(websock)

    def respond_to_message(self, message):
        command, argument = message.split('=')
        if command == 'yt-channel':
            self.process_yt_channel(argument)


    def process(self, item) -> str:
        name, (channelType, channel) = item

        response = ''

        # Get the channel data
        if channelType == 'yt-channel':
            conn = HTTPSConnection('www.youtube.com')
            conn.request('GET', '/channel/' + channel)
            response = conn.getresponse()
            data = response.read()

            # Find the data
            start = 'var ytInitialData = '.encode()
            end = ';</script>'.encode()
            data = data[data.find(start) + len(start):]
            data = data[:data.find(end)]

            # Convert to JSON
            jdata = json.loads(data)
            # with open('out.json', 'wb') as f:
            #     f.write(json.dumps(jdata, indent=4).encode())

            # Search for videos
            videos = find_key_like(jdata, 'videoRenderer') + find_key_like(jdata, 'gridVideoRenderer')

            message = { 
                'name' : name,
                'id' : channel,
                'videos' : [],
            }

            for video in videos:
                id = json_search(video, str, 'videoId')
                title = json_search(video, str, 'title', 'simpleText') or json_search(video, str, 'title', 'runs', 0, 'text')
                startTime = json_search(video, str, 'upcomingEventData', 'startTime')
                viewText = json_search(video, str, 'viewCountText', 'runs', ''.join, 'text') or json_search(video, str, 'viewCountText', 'simpleText')

                ids = [v['id'] for v in message['videos']]
                if id not in ids and ('watching' in viewText or 'waiting' in viewText):
                    message['videos'].append({
                        'id' : id,
                        'startTime' : startTime,
                        'viewText' : viewText,
                        'title' : title,
                    })
            response = json.dumps(message)
        return response

if __name__ == '__main__':
    handler = SocketHandler(PORT)
    handler.start()