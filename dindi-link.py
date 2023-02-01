# dindi-link: web-based remote desktop
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

import os
import sys
import time
import json
import aiohttp
import aiohttp.web
import PIL
import PIL.Image
import PIL.ImageGrab
import pyautogui

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO


# Defaults
DEFAULT_PASSWORD = ""
DEFAULT_QUALITY = 75
DEFAULT_WIDTH = 512
DEFAULT_HEIGHT = 512
DEFAULT_FPS = 20
DEFAULT_CONFIG_FILE = 'dindi-link-config.json'
DEFAULT_SERVER_PORT = 12345

MIN_VIEWPORT_DIM = 16
MAX_VIEWPORT_DIM = 2048
DOWNSAMPLE = PIL.Image.LANCZOS


# Config
# * password: str ("" means no password, default: "")
# * quality: int[1-100] (default: 75)
# * width: int - viewport
# * height: int - viewport
# * port: int[1-65535]
config = {}

# Real resolution
real_width, real_height = 0, 0
viewbox_width, viewbox_height = 0, 0

# Webapp
app: aiohttp.web.Application

# Bytes buffer
buffer = BytesIO()


def get_default(key: str):
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
	else:
		raise ValueError('invalid key')

def set_value(key: str, value):
	"""
	Set value for the given key with respect to None (and invalid value) as default value
	"""

	if key == 'password':
		if value is None or value == '':
			config[key] = get_default(key)
		else:
			config[key] = value
	elif key == 'quality':
		try:
			config[key] = max(1, min(100, int(value)))
		except:
			config[key] = get_default(key)
	elif key == 'server_port':
		try:
			config[key] = max(1, min(65535, int(value)))
		except:
			config[key] = get_default(key)
	elif key == 'fps':
		try:
			config[key] = max(1, min(60, int(value)))
		except:
			config[key] = get_default(key)
	elif key == 'width':
		try:
			config[key] = max(MIN_VIEWPORT_DIM, min(MAX_VIEWPORT_DIM, int(value)))
		except:
			config[key] = get_default(key)
	elif key == 'height':
		try:
			config[key] = max(MIN_VIEWPORT_DIM, min(MAX_VIEWPORT_DIM, int(value)))
		except:
			config[key] = get_default(key)
	else:
		raise ValueError('invalid key')

	with open(DEFAULT_CONFIG_FILE, 'w', encoding='utf-8') as f:
		json.dump(config, f)


def capture_screen_buffer() -> BytesIO:
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
	return buffer, buflen


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
		query__key = request.query.get('key', None)
		if query__key not in config:
			return aiohttp.web.json_response({
				'status': 'error',
				'message': 'key does not exist'
			})
		return aiohttp.web.json_response({
			'status': 'result',
			'value': config.get(query__key, None)
		})
	elif query__action == 'set':
		query__key = request.query.get('key', None)
		if query__key not in config:
			return aiohttp.web.json_response({
				'status': 'error',
				'message': 'key does not exist'
			})
		query__value = request.query.get('value', None)
		set_value(query__key, query__value)
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

async def get__input(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
	"""
	Configuration endpoint, requires password for each request. If password is
	not set, all requests are accepted. If password does not match, rejects
	request.

	query:
		action:
		password: str
	"""

	# Check access
	query__password = config.get('password', DEFAULT_PASSWORD)
	if query__password != DEFAULT_PASSWORD and query__password != request.query.get('password', None):
		return aiohttp.web.json_response({
			'status': 'error',
			'message': 'invalid password'
		})

	# Route on action
	query__action = request.query.get('action', None)
	
	# Receive mouse position relative to viewport
	if query__action == 'mouse':
		mouse_x = request.query.get('x', None)
		mouse_y = request.query.get('y', None)
		
		try:
			mouse_x = float(mouse_x)
			mouse_y = float(mouse_y)
		except:
			return aiohttp.web.json_response({
				'status': 'error',
				'message': 'invalid mouse position'
			})
		
		mouse_x = max(0, min(config['width'], mouse_x))
		mouse_y = max(0, min(config['height'], mouse_y))
		
		# Remap to real resolution
		mouse_x *= real_width / viewbox_width
		mouse_y *= real_height / viewbox_height
		
		pyautogui.moveTo(mouse_x, mouse_y)
		
		return aiohttp.web.json_response({
			'status': 'result'
		})
	else:
		return aiohttp.web.json_response({
			'status': 'error',
			'message': 'invalid action'
		})

async def get__capture(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
	"""
	Capture single display image and stream it as is using current quality and
	viewport settings
	"""

	# Check access
	query__password = config.get('password', DEFAULT_PASSWORD)
	if query__password != DEFAULT_PASSWORD and query__password != request.query.get('password', None):
		raise aiohttp.web.HTTPUnauthorized()

	buffer, buflen = capture_screen_buffer()
	sr = aiohttp.web.StreamResponse(
		status=200,
		headers={
			aiohttp.hdrs.CONTENT_TYPE: 'image/jpeg',
			aiohttp.hdrs.CONTENT_LENGTH: str(buflen)
		}
	)
	writer = await sr.prepare(request)
	await writer.write(buffer.read(buflen))
	buffer.seek(0)
	return sr


if __name__ == '__main__':
	# Load initial config
	try:
		with open(DEFAULT_CONFIG_FILE, 'r', encoding='utf-8') as f:
			config = json.load(f)
	except:
		print(f'{ DEFAULT_CONFIG_FILE } missing, using default')

		# Generate default config
		set_value('password', None)
		set_value('quality', None)
		set_value('width', None)
		set_value('height', None)
		set_value('server_port', None)
		set_value('fps', None)

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/config', get__config)
	app.router.add_get('/input', get__input)
	app.router.add_get('/capture', get__capture)
	app.router.add_get('/', lambda request: aiohttp.web.FileResponse('index.html'))
	
	# Grab real resolution
	real_width, real_height = PIL.ImageGrab.grab().size

	aiohttp.web.run_app(app=app, port=config['server_port'])
