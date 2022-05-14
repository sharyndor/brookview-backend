#!/usr/bin/python

from queue import Queue
import select
import socket
from threading import Lock, Thread

from base64 import b64encode
from hashlib import sha1

from http.client import HTTPSConnection
import json

from os import path, _exit

from time import sleep, time

PORT = 8080
VERSION = [0, 3, 2] # vMajor.Minor.Patch

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
    self.backlog = []

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

    print('A socket has been closed. Disconnected?')

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
  sockets : list[WebSocket] = []

  def __init__(self, port):
    self.sockets = []
    self.lock = Lock()

    self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.bind(('', port))

    self.history = {}
    if path.exists('history.json'):
      with open('history.json', 'rb') as f:
        if data := f.read():
          self.history = json.loads(data.decode())

  def start(self):
    Thread(target=self.accept_all).start()

    # Run forever
    while True:
      with self.lock:
        # Get rid of any sockets which have stopped
        self.sockets = [sock for sock in self.sockets if sock.should_continue]

        # For each socket, convert each received message to JSON and put it in the backlog
        for sock in self.sockets:
          while not sock.messages.empty():
            message = json.loads(sock.messages.get())

            if 'restart' in message and message['restart'] == True:
              self.restart()

            if 'autoUpdate' in message and message['autoUpdate'] == True:
              self.autoUpdate()

            if 'messageType' in message:
              if message['messageType'] == 'version':
                sock.send_message(WebSocket.Opcode.TEXT, json.dumps({
                  'messageType': 'version',
                  'version' : VERSION,
                }).encode())
              elif message['messageType'] == 'streamers':
                requests = []
                if 'version' in message and message['version'] == VERSION:
                  requests = message['requests'] if 'requests' in message else []
                else:
                  print('Bad version')

                # Add any requests that aren't already known
                for request in requests.values():
                  foundRequest = False
                  for item in sock.backlog:
                    if item['name'] == request['name']:
                      foundRequest = True
                      break
                  if not foundRequest:
                    sock.backlog.append(request)

      
      # Check whether the first item of each socket's backlog needs work
      for sock in self.sockets:
        if len(sock.backlog) > 0:
          request = sock.backlog[0]
          sock.backlog = sock.backlog[1:]
          
          hist_lookup = request['name']

          lastUpdateTime, message = 0, str()
          if hist_lookup in self.history:
            lastUpdateTime, message = self.history[hist_lookup]

          # Consider data to be out of date if version differs or processed outside of the last 5 minutes
          goodVersion = 'version' in message and message['version'] == VERSION
          if not message or not goodVersion or (time() - lastUpdateTime) > (5 * 60):
            message = self.process(request)
            if message := self.process(request):
              self.history[hist_lookup] = time(), message
              with open('history.json', 'wb') as f:
                f.write(json.dumps(self.history).encode())
                
              # A web query was made, sleep to throttle queries
              sleep(1)
          
          sock.send_message(WebSocket.Opcode.TEXT, json.dumps(message).encode())

      # If no work is remaining, pause the thread to keep it from spinning
      remainingWork = 0
      for sock in self.sockets:
        remainingWork += len(sock.backlog)
      
      if remainingWork == 0:
        sleep(1)

  def accept_all(self):
    try:
      self.sock.listen(5)
      while True:
        sock, _ = self.sock.accept()
        websock = WebSocket(sock)
        websock.start()
        with self.lock:
          self.sockets.append(websock)
    except:
      print('Accept socket died, I hope this is an auto-update...')

  def processYT_Channel(self, channel):
    # Fetch the data from youtube
    keepTrying = True
    while keepTrying:
      try:
        conn = HTTPSConnection('www.youtube.com')
        conn.request('GET', '/channel/' + channel)
        response = conn.getresponse()
        data = response.read()
        keepTrying = False
      except:
        sleep(1)
        pass

    # Find where the page info is located
    start = 'var ytInitialData = '.encode()
    end = ';</script>'.encode()
    data = data[data.find(start) + len(start):]
    data = data[:data.find(end)]

    # Convert to JSON
    try:
      jdata = json.loads(data)
    except:
      return []

    # Collect the videos
    videos = []

    # If the channel is live, the data will be in a videoRenderer
    for video in find_key_like(jdata, 'videoRenderer'):
      # Channels may have multiple live streams, filter out promoted videos to find them
      viewText = json_search(video, str, 'viewCountText', 'runs', ''.join, 'text') or json_search(video, str, 'viewCountText', 'simpleText')
      if 'watching' in viewText or 'waiting' in viewText:
        videos.append({
          'type' : 'yt-video',
          'value' : json_search(video, str, 'videoId'),
          'title' : json_search(video, str, 'title', 'simpleText') or json_search(video, str, 'title', 'runs', 0, 'text'),
          'status' : 'live' if 'watching' in viewText else 'upcoming',
          'startTime' : 0 if 'watching' in viewText else json_search(video, str, 'upcomingEventData', 'startTime'),
        })
    
    # If the channel is not live, the data will be in a shelfRenderer
    shelves = find_key_like(jdata, 'shelfRenderer')
    for shelf in shelves:
      # Search for the shelf with the right title
      if json_search(shelf, str, 'title', 'runs', 0, 'text') == 'Upcoming live streams':
        # Channels may have multiple upcoming streams, add each
        for video in find_key_like(shelf, 'gridVideoRenderer'):
          viewText = json_search(video, str, 'viewCountText', 'runs', ''.join, 'text') or json_search(video, str, 'viewCountText', 'simpleText')
          videos.append({
            'type' : 'yt-video',
            'value' : json_search(video, str, 'videoId'),
            'title' : json_search(video, str, 'title', 'simpleText') or json_search(video, str, 'title', 'runs', 0, 'text'),
            'status' : 'live' if 'watching' in viewText else 'upcoming',
            'startTime' : 0 if 'watching' in viewText else json_search(video, str, 'upcomingEventData', 'startTime'),
          })
    
    return videos

  def processTTV_Channel(self, channel):
    # Fetch the data from Twitch
    keepTrying = True
    while keepTrying:
      try:
        conn = HTTPSConnection('www.twitch.tv')
        conn.request('GET', channel)
        response = conn.getresponse()
        data = response.read()
        keepTrying = False
      except:
        sleep(1)
        pass

    # Stream data is contained in this tag
    start = '<script type="application/ld+json">'.encode()
    end = '</script>'.encode()

    # If the channel isn't live, this will be missing
    if start not in data:
      return []

    # Trim off everything before the tag
    data = data[data.find(start) + len(start):]

    # Look for the closing tag (it should always be present)
    if end not in data:
      return []

    # Trim off everything after the tag
    data = data[:data.find(end)]

    # Convert to JSON
    # Twitch uses a list for some reason, so take the first element
    try:
      jdata = json.loads(data)[0]
    except:
      return []

    return [{
      'type' : 'ttv-channel',
      'value' : channel,
      'title' : jdata['description'],
      'startTime' : 0,
      'status' : 'live',
    }]

  def process(self, request) -> str:
    message = { 
      'name' : request['name'],
      'videos' : [],
      'version' : VERSION,
      'messageType' : 'update'
    }

    if 'streams' not in request:
      return message

    for stream in request['streams']:
      dataType = stream['type']
      dataValue = stream['value']

      if dataType == 'yt-channel':
        message['videos'] += self.processYT_Channel(dataValue)
      elif dataType == 'ttv-channel':
        message['videos'] += self.processTTV_Channel(dataValue)

    return message

  def restart(self):
    # Close everything down
    self.sock.close()
    for sock in self.sockets:
      sock.sock.close()
    
    # Allow time for sockets/threads to close
    sleep(1)
    print('Goodbye: v' + '.'.join([str(n) for n in VERSION]))
    sleep(1)

    # Kill the process and allow the start script to reboots the server
    _exit(0)

  def autoUpdate(self):
    # Download the new file
    try:
      conn = HTTPSConnection('raw.githubusercontent.com')
      conn.request('GET', '/sharyndor/brookview-backend/main/brookview.py')
      response = conn.getresponse()
      with open(__file__, 'wb') as f:
        f.write(response.read())
    except:
      print('Something failed while trying to download the update!')
      return

    self.restart()

if __name__ == '__main__':
  print('Hello: v' + '.'.join([str(n) for n in VERSION]))
  handler = SocketHandler(PORT)
  handler.start()
