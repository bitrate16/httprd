# dindi-link: web-based remote desktop
# Copyright (C) 2022  bitrate16
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

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO


# Defaults
DEFAULT_PASSWORD = ""
DEFAULT_QUALITY = 75
DEFAULT_VIEWPORT = (512, 512)
DEFAULT_CONFIG_FILE = 'dindi-link-config.json'
DEFAULT_SERVER_PORT = 12345

MIN_VIEWPORT_DIM = 16
MAX_VIEWPORT_DIM = 2048
DOWNSAMPLE = PIL.Image.LANCZOS


# Config
# * password: str ("" means no password, default: "")
# * quality: int[1-100] (default: 75)
# * viewport: (width: int, height: int)
# * port: int[1-65535]
config = {}

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
	elif key == 'viewport':
		return DEFAULT_VIEWPORT
	elif key == 'server_port':
		return DEFAULT_SERVER_PORT
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
	elif key == 'viewport':
		try:
			value = value.split(',', 1)
			value = (min(max(int(value[0].strip()), MIN_VIEWPORT_DIM), MAX_VIEWPORT_DIM), min(max(int(value[1].strip()), MIN_VIEWPORT_DIM), MAX_VIEWPORT_DIM))

			config[key] = value
		except:
			import traceback
			traceback.print_exc()
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
	if image.width > config['viewport'][0] or image.height > config['viewport'][1]:
		image.thumbnail(config['viewport'], DOWNSAMPLE)
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
		return aiohttp.web.json_response({
			'status': 'error',
			'message': 'invalid password'
		})

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
		set_value('viewport', None)
		set_value('server_port', None)

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/config', get__config)
	app.router.add_get('/capture', get__capture)

	aiohttp.web.run_app(app=app, port=config['server_port'])
