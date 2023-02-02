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

VERSION = '2.5'

import time
import json
import typing
import aiohttp
import aiohttp.web
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


# Defaults
DEFAULT_PASSWORD = ""
DEFAULT_QUALITY = 75
DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 512
DEFAULT_FPS = 20
DEFAULT_IPS = 5
DEFAULT_CONFIG_FILE = 'httprd-config.json'
DEFAULT_SERVER_PORT = 7417

MIN_VIEWPORT_DIM = 16
MAX_VIEWPORT_DIM = 2048
DOWNSAMPLE = PIL.Image.LANCZOS

# Event types
INPUT_EVENT_MOUSE_MOVE   = 0
INPUT_EVENT_MOUSE_DOWN   = 1
INPUT_EVENT_MOUSE_UP     = 2
INPUT_EVENT_MOUSE_SCROLL = 3


# Config
# * password: str ("" means no password, default: "")
# * quality: int[1-100] (default: 75)
# * width: int - viewport
# * height: int - viewport
# * port: int[1-65535]
# * fps: int[1-60]
# * ips: int[1-60]
config = {}

# Real resolution
real_width, real_height = 0, 0
viewbox_width, viewbox_height = 0, 0

# Webapp
app: aiohttp.web.Application

# Log request details
def log_request(request: aiohttp.web.Request):
	now = datetime.now()
	now = now.strftime("%d.%m.%Y-%H:%M:%S")
	print(f'[{ now }] { request.remote } { request.method } { request.path_qs }')

def get_config_default(key: str):
	"""
	Get default value for the given key
	"""

	if key == 'password':
		return DEFAULT_PASSWORD
	elif key == 'quality':
		return DEFAULT_QUALITY
	elif key == 'width':
		return DEFAULT_WIDTH
	elif key == 'height':
		return DEFAULT_HEIGHT
	elif key == 'server_port':
		return DEFAULT_SERVER_PORT
	elif key == 'fps':
		return DEFAULT_FPS
	elif key == 'ips':
		return DEFAULT_IPS
	else:
		raise ValueError('invalid key')

def set_config_value(key: str, value):
	"""
	Set value for the given key with respect to None (and invalid value) as default value
	"""

	if key == 'password':
		if value is None or value == '':
			config[key] = get_config_default(key)
		else:
			config[key] = value
	elif key == 'quality':
		try:
			config[key] = max(1, min(100, int(value)))
		except:
			config[key] = get_config_default(key)
	elif key == 'server_port':
		try:
			config[key] = max(1, min(65535, int(value)))
		except:
			config[key] = get_config_default(key)
	elif key == 'fps':
		try:
			config[key] = max(1, min(60, int(value)))
		except:
			config[key] = get_config_default(key)
	elif key == 'ips':
		try:
			config[key] = max(1, min(60, int(value)))
		except:
			config[key] = get_config_default(key)
	elif key == 'width':
		try:
			config[key] = max(MIN_VIEWPORT_DIM, min(MAX_VIEWPORT_DIM, int(value)))
		except:
			config[key] = get_config_default(key)
	elif key == 'height':
		try:
			config[key] = max(MIN_VIEWPORT_DIM, min(MAX_VIEWPORT_DIM, int(value)))
		except:
			config[key] = get_config_default(key)
	else:
		raise ValueError('invalid key')

	with open(DEFAULT_CONFIG_FILE, 'w', encoding='utf-8') as f:
		json.dump(config, f)


def capture_screen_buffer(buffer: BytesIO) -> typing.Tuple[BytesIO, int]:
	"""
	Capture current screen state and reprocess based on current config
	"""

	image = PIL.ImageGrab.grab()

	# Update real dimensions
	global real_width, real_height
	real_width, real_height = image.size

	if image.width > config['width'] or image.height > config['height']:
		image.thumbnail((config['width'], config['height']), DOWNSAMPLE)

	# Update viewbox dimensions
	global viewbox_width, viewbox_height
	viewbox_width, viewbox_height = image.size

	buffer.seek(0)
	image.save(fp=buffer, format='JPEG', quality=config['quality'])
	buflen = buffer.tell()
	buffer.seek(0)
	return buflen


async def get__config(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
	"""
	Configuration endpoint, requires password for each request. If password is
	not set, all requests are accepted. If password does not match, rejects
	request.

	query:
		action:
			set: (Can be used to update config properties and reset them to defaults with None)
				key: str
				value: str
			get:
				key: str
			keys
		password: str
	"""

	log_request(request)

	# Check access
	query__password = config.get('password', DEFAULT_PASSWORD)
	if query__password != DEFAULT_PASSWORD and query__password != request.query.get('password', None):
		return aiohttp.web.json_response({
			'status': 'error',
			'message': 'invalid password'
		})

	# Route on action
	query__action = request.query.get('action', None)
	if query__action == 'get':
		return aiohttp.web.json_response({
			'status': 'result',
			'config': config
		})
	elif query__action == 'set':
		query__key = request.query.get('key', None)
		if query__key not in config:
			return aiohttp.web.json_response({
				'status': 'error',
				'message': 'key does not exist'
			})
		query__value = request.query.get('value', None)
		set_config_value(query__key, query__value)
		return aiohttp.web.json_response({
			'status': 'result',
			'value': config.get(query__key, None)
		})
	elif query__action == 'keys':
		return aiohttp.web.json_response({
			'status': 'result',
			'keys': list(config.keys())
		})
	else:
		return aiohttp.web.json_response({
			'status': 'error',
			'message': 'invalid action'
		})

async def get__connect_ws(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
	"""
	Capture display stream and write it as JPEG stream via Websocket, so receive input
	"""

	log_request(request)

	# Check access
	query__password = config.get('password', DEFAULT_PASSWORD)
	if query__password != DEFAULT_PASSWORD and query__password != request.query.get('password', None):
		raise aiohttp.web.HTTPUnauthorized()

	# Open socket
	ws = aiohttp.web.WebSocketResponse()
	await ws.prepare(request)

	# Bytes buffer
	buffer = BytesIO()

	last_frame_ack = 1

	# Write stream
	async def write_stream():

		# last frame timestamp to track receive or timeout for resend
		last_frame_sent = 0

		# Write frames at desired framerate
		while not ws.closed:

			# Throttle & wait for next frame reception
			if last_frame_ack >= last_frame_sent or (time.time() - last_frame_sent) > 10.0 / config['fps']:
				# Send frame as is
				buflen = capture_screen_buffer(buffer)
				mbytes = buffer.read(buflen)
				buffer.seek(0)

				t = time.time()
				await ws.send_bytes(mbytes)
				last_frame_sent = time.time()

				# Wait next frame
				await asyncio.sleep(1.0 / config['fps'] - (last_frame_sent - t))
			else:
				await asyncio.sleep(0.5 / config['fps'])

	# Read stream
	async def read_stream():

		# last ACK timestamp to track receive or timeout for resend
		nonlocal last_frame_ack

		# Write frames at desired framerate
		async for msg in ws:
			# Receive input data
			if msg.type == aiohttp.WSMsgType.TEXT:
				try:

					# Frame ACK
					if msg.data == 'FA':
						last_frame_ack = time.time()
						continue

					# Input data
					data = json.loads(msg.data)
					for event in data:
						if event[0] == INPUT_EVENT_MOUSE_MOVE: # mouse position
							mouse_x = max(0, min(config['width'], event[1]))
							mouse_y = max(0, min(config['height'], event[2]))

							# Remap to real resolution
							mouse_x *= real_width / viewbox_width
							mouse_y *= real_height / viewbox_height

							pyautogui.moveTo(mouse_x, mouse_y)
						elif event[0] == INPUT_EVENT_MOUSE_DOWN: # mouse down
							mouse_x = max(0, min(config['width'], event[1]))
							mouse_y = max(0, min(config['height'], event[2]))
							button = event[3]

							# Allow only left, middle, right
							if button < 0 or button > 2:
								continue

							# Remap to real resolution
							mouse_x *= real_width / viewbox_width
							mouse_y *= real_height / viewbox_height

							pyautogui.mouseDown(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
						elif event[0] == INPUT_EVENT_MOUSE_UP: # mouse up
							mouse_x = max(0, min(config['width'], event[1]))
							mouse_y = max(0, min(config['height'], event[2]))
							button = event[3]

							# Allow only left, middle, right
							if button < 0 or button > 2:
								continue

							# Remap to real resolution
							mouse_x *= real_width / viewbox_width
							mouse_y *= real_height / viewbox_height

							pyautogui.mouseUp(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
						elif event[0] == INPUT_EVENT_MOUSE_SCROLL: # mouse scroll
							mouse_x = max(0, min(config['width'], event[1]))
							mouse_y = max(0, min(config['height'], event[2]))
							dy = int(event[3])

							# Remap to real resolution
							mouse_x *= real_width / viewbox_width
							mouse_y *= real_height / viewbox_height

							pyautogui.scroll(dy, mouse_x, mouse_y)
				except:
					import traceback
					traceback.print_exc()
			elif msg.type == aiohttp.WSMsgType.ERROR:
				print(f'ws connection closed with exception { ws.exception() }')


	await asyncio.gather(write_stream(), read_stream())

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
	print(f'httprd version { VERSION } (C) bitrate16 2022-2023 GNU GPL v3')

	# Load initial config
	try:
		with open(DEFAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
			config = json.load(f)
	except:
		print(f'{ DEFAULT_CONFIG_FILE } missing, using default')

		# Generate default config
		set_config_value('password', None)
		set_config_value('quality', None)
		set_config_value('width', None)
		set_config_value('height', None)
		set_config_value('server_port', None)
		set_config_value('fps', None)
		set_config_value('ips', None)

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/config', get__config)
	app.router.add_get('/connect_ws', get__connect_ws)
	app.router.add_get('/', get__root)

	# Grab real resolution
	real_width, real_height = PIL.ImageGrab.grab().size

	aiohttp.web.run_app(app=app, port=config['server_port'])
