# httprd: web-based remote desktop
# Copyright (C) 2022-2023  bitrate16
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

VERSION = '3.1'

import time
import json
import typing
import aiohttp
import aiohttp.web
import argparse
import PIL
import PIL.Image
import PIL.ImageGrab
import pyautogui
import asyncio

from datetime import datetime

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

# Failsafe disable
pyautogui.FAILSAFE = False

# Const config
DOWNSAMPLE = PIL.Image.NEAREST

# Input event types
INPUT_EVENT_MOUSE_MOVE   = 0
INPUT_EVENT_MOUSE_DOWN   = 1
INPUT_EVENT_MOUSE_UP     = 2
INPUT_EVENT_MOUSE_SCROLL = 3
INPUT_EVENT_KEY_DOWN     = 4
INPUT_EVENT_KEY_UP       = 5

# Args
args = {}

# Real resolution
real_width, real_height = 0, 0

# Webapp
app: aiohttp.web.Application

# Log request details
def log_request(request: aiohttp.web.Request):
	now = datetime.now()
	now = now.strftime("%d.%m.%Y-%H:%M:%S")
	print(f'[{ now }] { request.remote } { request.method } { request.path_qs }')


def decode_int8(data):
	return int.from_bytes(data[0:1], 'little')

def decode_int16(data):
	return int.from_bytes(data[0:2], 'little')

def decode_int24(data):
	return int.from_bytes(data[0:3], 'little')

def encode_int8(i):
	return int.to_bytes(i, 1, 'little')

def encode_int16(i):
	return int.to_bytes(i, 2, 'little')

def encode_int24(i):
	return int.to_bytes(i, 3, 'little')

def dump_bytes_dec(data):
	for i in range(len(data)):
		print(data[i], end=' ')
	print()


async def get__connect_ws(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
	"""
	Capture display stream and write it as JPEG stream via Websocket, so receive input
	"""

	log_request(request)

	# Check access
	if args.password != request.query.get('password', ''):
		raise aiohttp.web.HTTPUnauthorized()

	# Open socket
	ws = aiohttp.web.WebSocketResponse()
	await ws.prepare(request)

	# Frame buffer
	buffer = BytesIO()

	# Track pressed key state for future reset on disconnect
	state_keys = {}

	def release_keys():
		for k in state_keys.keys():
			if state_keys[k]:
				pyautogui.keyUp(k)

	def update_key_state(key, state):
		state_keys[key] = state

	# Read stream
	async def async_worker():

		try:
			# Write frames at desired framerate
			async for msg in ws:
				# Receive input data
				if msg.type == aiohttp.WSMsgType.BINARY:
					try:

						# Drop on invalid packet
						if len(msg.data) == 0:
							continue

						# Parse params
						packet_type = decode_int8(msg.data[0:1])
						payload = msg.data[1:]

						# Frame request
						if packet_type == 0x01:
							viewport_width = decode_int16(payload[0:2])
							viewport_height = decode_int16(payload[2:4])
							quality = decode_int8(payload[4:5])

							# Grab frame
							image = PIL.ImageGrab.grab()

							# Real dimensions
							real_width, real_height = image.width, image.height

							# Resize
							if image.width > viewport_width or image.height > viewport_height:
								image.thumbnail((viewport_width, viewport_height), DOWNSAMPLE)

							# Write header: frame response
							buffer.seek(0)
							buffer.write(encode_int8(0x02))
							buffer.write(encode_int16(real_width))
							buffer.write(encode_int16(real_height))

							# Write body
							image.save(fp=buffer, format='JPEG', quality=quality)
							buflen = buffer.tell()
							buffer.seek(0)
							mbytes = buffer.read(buflen)

							await ws.send_bytes(mbytes)

						# Input request
						if packet_type == 0x03:
							# Unpack events data
							data = json.loads(bytes.decode(payload, encoding='ascii'))

							# Iterate events
							for event in data:
								if event[0] == INPUT_EVENT_MOUSE_MOVE: # mouse position
									mouse_x = max(0, min(real_width, event[1]))
									mouse_y = max(0, min(real_height, event[2]))

									pyautogui.moveTo(mouse_x, mouse_y)
								elif event[0] == INPUT_EVENT_MOUSE_DOWN: # mouse down
									mouse_x = max(0, min(real_width, event[1]))
									mouse_y = max(0, min(real_height, event[2]))
									button = event[3]

									# Allow only left, middle, right
									if button < 0 or button > 2:
										continue

									pyautogui.mouseDown(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
								elif event[0] == INPUT_EVENT_MOUSE_UP: # mouse up
									mouse_x = max(0, min(real_width, event[1]))
									mouse_y = max(0, min(real_height, event[2]))
									button = event[3]

									# Allow only left, middle, right
									if button < 0 or button > 2:
										continue

									pyautogui.mouseUp(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
								elif event[0] == INPUT_EVENT_MOUSE_SCROLL: # mouse scroll
									mouse_x = max(0, min(real_width, event[1]))
									mouse_y = max(0, min(real_height, event[2]))
									dy = int(event[3])

									pyautogui.scroll(dy, mouse_x, mouse_y)
								elif event[0] == INPUT_EVENT_KEY_DOWN: # keypress
									keycode = event[1]

									pyautogui.keyDown(keycode)
									update_key_state(keycode, True)
								elif event[0] == INPUT_EVENT_KEY_UP: # keypress
									keycode = event[1]

									pyautogui.keyUp(keycode)
									update_key_state(keycode, False)
					except:
						import traceback
						traceback.print_exc()
				elif msg.type == aiohttp.WSMsgType.ERROR:
					print(f'ws connection closed with exception { ws.exception() }')
		except:
			import traceback
			traceback.print_exc()

	await async_worker()

	# Release stuck keys
	release_keys()

	return ws


# Encoded page hoes here
INDEX_CONTENT = None


# handler for /
async def get__root(request: aiohttp.web.Request):
	log_request(request)

	if INDEX_CONTENT is not None:
		return aiohttp.web.Response(body=INDEX_CONTENT, content_type='text/html', status=200, charset='utf-8')
	else:
		return aiohttp.web.FileResponse('index.html')


if __name__ == '__main__':
	# Args
	parser = argparse.ArgumentParser(description='Process some integers.')
	parser.add_argument('--port', type=int, default=7417, metavar='{1..65535}', choices=range(1, 65535), help='server port')
	parser.add_argument('--password', type=str, default=None, help='default password')
	args = parser.parse_args()

	# Post-process args
	if args.password is None:
		args.password = ''
	else:
		args.password = args.password.strip()

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/connect_ws', get__connect_ws)
	app.router.add_get('/', get__root)

	# Listen
	aiohttp.web.run_app(app=app, port=args.port)
