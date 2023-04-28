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

# Defaults for client config
DEFAULTS = {
	'password': '',
	'quality': 75,
	'fps': 20,
	'ips': 5,
	'port': 7417
}

PROPS_KEYS = list(DEFAULTS.keys())

# Config
CONFIG_FILE = 'httprd-config.json'
MIN_VIEWPORT_DIM = 16
MAX_VIEWPORT_DIM = 2048
DOWNSAMPLE = PIL.Image.LANCZOS

# Props
props = {}

# Props getter
def get_prop(name: str):
	global props
	
	# Read if not exists
	if props is None:
		try:
			with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
				props = json.load(f)
		except:
			props = dict.copy(DEFAULTS)
	
	# Get existing or default
	if name not in props:
		if name in DEFAULTS:
			return DEFAULTS[name]
		else:
			return None
	return props[name]

# Props setter
def set_prop(name: str, value):
	global props
	
	# Read if not exists
	if props is None:
		try:
			with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
				props = json.load(f)
		except:
			props = dict.copy(DEFAULTS)
	
	# Validate value
	if name == 'password':
		try:
			value = value.strip()
		except:
			raise ValueError('password has invalid value')
	elif name == 'quality':
		try:
			value = int(value)
		except:
			raise ValueError('quality must be int in range [1, 100]')
		
		if value < 1 or value > 100:
			raise ValueError('quality must be int in range [1, 100]')
	elif name == 'fps':
		try:
			value = int(value)
		except:
			raise ValueError('fps must be int in range [1, inf]')
		
		if value < 1:
			raise ValueError('fps must be int in range [1, inf]')
	elif name == 'ips':
		try:
			value = int(value)
		except:
			raise ValueError('ips must be int in range [1, inf]')
		
		if value < 1:
			raise ValueError('ips must be int in range [1, inf]')
	elif name == 'server_port':
		try:
			value = int(value)
		except:
			raise ValueError('server_port must be int in range [1, 65535]')
		
		if value < 1 or value > 65535:
			raise ValueError('server_port must be int in range [1, 65535]')
	else:
		raise ValueError(f'unknown prop "{ name }"')
	
	# Set & write
	if props.get(name, None) == value:
		return
	
	props[name] = value
	with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
		json.dump(props, f)
	
		


# Event types
INPUT_EVENT_MOUSE_MOVE   = 0
INPUT_EVENT_MOUSE_DOWN   = 1
INPUT_EVENT_MOUSE_UP     = 2
INPUT_EVENT_MOUSE_SCROLL = 3


# Config
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
	if get_prop('password') != config.get('password', ''):
		raise aiohttp.web.HTTPUnauthorized()

	# Open socket
	ws = aiohttp.web.WebSocketResponse()
	await ws.prepare(request)

	# Frame buffer
	buffer = BytesIO()

	# Write stream
	async def write_stream():

		# last frame timestamp to track receive or timeout for resend
		last_frame_sent = 0

		# Write frames at desired framerate
		while not ws.closed:

			# Throttle & wait for next frame reception
			if last_frame_ack >= last_frame_sent or (time.time() - last_frame_sent) > 10.0 / config['fps']:
				# Grab frame
				image = PIL.ImageGrab.grab()

				# Update real dimensions
				global real_width, real_height
				real_width, real_height = image.size

				if image.width > config['width'] or image.height > config['height']:
					image.thumbnail((config['width'], config['height']), DOWNSAMPLE)

				# Update viewbox dimensions
				global viewbox_width, viewbox_height
				viewbox_width, viewbox_height = image.size

				image.save(fp=buffer, format='JPEG', quality=config['quality'])
				buflen = buffer.tell()
				mbytes = buffer.read(buflen)
				buffer.seek(0)

				t = time.time()
				await ws.send_bytes(mbytes)
				last_frame_sent = time.time()

				# Wait next frame
				await asyncio.sleep(1.0 / config['fps'] - (last_frame_sent - t))
			else:
				await asyncio.sleep(0.5 / config['fps'])

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

	# Read stream
	async def async_worker():

		# Write frames at desired framerate
		async for msg in ws:
			# Receive input data
			if msg.type == aiohttp.WSMsgType.BINARY:
				try:
					
					# Drop on invalid packet
					if len(msg.data) < 4:
						continue
					
					# print(msg.data)
					print()
					print('bytes')
					dump_bytes_dec(msg.data)
					
					# Parse params
					packet_type = decode_int8(msg.data[0:1])
					payload = msg.data[1:]
					
					# Frame request
					if packet_type == 0x01:
						viewport_width = decode_int16(payload[0:2])
						viewport_height = decode_int16(payload[2:4])
						
						print('packet_type = ', packet_type)
						print('viewport_width = ', viewport_width)
						print('viewport_height = ', viewport_height)
						
						print(encode_int16(121))
						
						# Grab frame
						image = PIL.ImageGrab.grab()
						
						# Resize
						if image.width > viewport_width or image.height > viewport_height:
							image.thumbnail((viewport_width, viewport_height), DOWNSAMPLE)
						
						# Write header: frame response
						buffer.seek(0)
						buffer.write(encode_int8(0x02))
						
						# Write body
						image.save(fp=buffer, format='JPEG', quality=get_prop('quality'))
						buflen = buffer.tell()
						buffer.seek(0)
						mbytes = buffer.read(buflen)
						
						await ws.send_bytes(mbytes)
					

					# # Input data
					# data = json.loads(msg.data)
					# for event in data:
					# 	if event[0] == INPUT_EVENT_MOUSE_MOVE: # mouse position
					# 		mouse_x = max(0, min(config['width'], event[1]))
					# 		mouse_y = max(0, min(config['height'], event[2]))

					# 		# Remap to real resolution
					# 		mouse_x *= real_width / viewbox_width
					# 		mouse_y *= real_height / viewbox_height

					# 		pyautogui.moveTo(mouse_x, mouse_y)
					# 	elif event[0] == INPUT_EVENT_MOUSE_DOWN: # mouse down
					# 		mouse_x = max(0, min(config['width'], event[1]))
					# 		mouse_y = max(0, min(config['height'], event[2]))
					# 		button = event[3]

					# 		# Allow only left, middle, right
					# 		if button < 0 or button > 2:
					# 			continue

					# 		# Remap to real resolution
					# 		mouse_x *= real_width / viewbox_width
					# 		mouse_y *= real_height / viewbox_height

					# 		pyautogui.mouseDown(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
					# 	elif event[0] == INPUT_EVENT_MOUSE_UP: # mouse up
					# 		mouse_x = max(0, min(config['width'], event[1]))
					# 		mouse_y = max(0, min(config['height'], event[2]))
					# 		button = event[3]

					# 		# Allow only left, middle, right
					# 		if button < 0 or button > 2:
					# 			continue

					# 		# Remap to real resolution
					# 		mouse_x *= real_width / viewbox_width
					# 		mouse_y *= real_height / viewbox_height

					# 		pyautogui.mouseUp(mouse_x, mouse_y, button=[ 'left', 'middle', 'right' ][button])
					# 	elif event[0] == INPUT_EVENT_MOUSE_SCROLL: # mouse scroll
					# 		mouse_x = max(0, min(config['width'], event[1]))
					# 		mouse_y = max(0, min(config['height'], event[2]))
					# 		dy = int(event[3])

					# 		# Remap to real resolution
					# 		mouse_x *= real_width / viewbox_width
					# 		mouse_y *= real_height / viewbox_height

					# 		pyautogui.scroll(dy, mouse_x, mouse_y)
				except:
					import traceback
					traceback.print_exc()
			elif msg.type == aiohttp.WSMsgType.ERROR:
				print(f'ws connection closed with exception { ws.exception() }')


	await async_worker()

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

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/connect_ws', get__connect_ws)
	app.router.add_get('/', get__root)

	# Grab real resolution
	real_width, real_height = PIL.ImageGrab.grab().size

	aiohttp.web.run_app(app=app, port=get_prop('port'))
