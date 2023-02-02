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

VERSION = '2.3'

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
DEFAULT_SERVER_PORT = 12345

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
INDEX_CONTENT = "<html>\n<head>\n<title>HTTPRD</title>\n<meta charset=\"UTF-8\">\n<style>\ncanvas {\nwidth: 100%;\nheight: 100%;\n}\n* {\nfont-family: monospace;\nfont-weight: bold;\n}\nhtml, body {\nwidth: 100%;\nheight: 100%;\npadding: 0;\nmargin: 0;\n}\n#dialog {\nposition: absolute;\nwidth: 100%;\nheight: 100%;\nz-index: 900;\ntop: 0;\nleft: 0;\ndisplay: flex;\nflex-direction: column;\njustify-content: center;\nalign-items: center;\n}\n#dialog > #shadow {\nposition: absolute;\nwidth: 100%;\nheight: 100%;\nz-index: 910;\ntop: 0;\nleft: 0;\nbackground-color: #0007;\n}\n#dialog > #content {\nz-index: 920;\npadding: 1rem;\nbackground-color: #fff;\n}\n#dialog > #content > #title {\nfont-size: 1.5rem;\nmargin-bottom: 0.5rem;\n}\n#dialog > #content > #form {\nwidth: 100%;\n}\ndiv.input-field {\ndisplay: flex;\nflex-direction: row;\nflex-wrap: nowrap;\njustify-content: space-between;\nwidth: calc(100% - 1rem);\nborder: thin solid #aaf;\npadding: 0.5rem;\nmargin-bottom: 0.5rem;\n}\ndiv.input-field > label {\npadding: 0.25rem;\n}\ndiv.input-field > div {\ndisplay: flex;\nflex-direction: row;\nflex-wrap: nowrap;\ngap: 0.5rem;\n}\ndiv.input-field > div > input {\nbackground-color: #eee;\nborder: thin solid #aaf;\nbox-sizing: border-box;\npadding: 0.25rem;\n}\ndiv.input-field > div > input:hover {\nbox-shadow: #77f 0px 0px 0px 0.1rem;\nopacity: 0.75;\n}\ndiv.input-field > div > input:active {\nborder: thin solid #77f;\n}\ndiv.input-field > div > input[type=\"button\"]:active {\nbackground-color: #77e;\n}\n#config {\nposition: absolute;\nwidth: 1rem;\nheight: 1rem;\nbottom: 1rem;\nright: 1rem;\noverflow: hidden;\nfont-size: 1rem;\ncursor: pointer;\nuser-select: none;\n}\n</style>\n</head>\n<body>\n<canvas id=\"display\">\n</canvas>\n<div id=\"dialog\" style=\"display:none;\">\n<div id=\"content\">\n<div id=\"title\">\nConfig\n</div>\n<div id=\"form\">\n</div>\n</div>\n<div id=\"shadow\" onclick=\"hideConfigDialog();\"></div>\n</div>\n<div id=\"config\" onclick=\"showConfigDialog();\">\u2699</div>\n<script type=\"text/javascript\" async>\nconst INPUT_EVENT_MOUSE_MOVE   = 0;\nconst INPUT_EVENT_MOUSE_DOWN   = 1;\nconst INPUT_EVENT_MOUSE_UP     = 2;\nconst INPUT_EVENT_MOUSE_SCROLL = 3;\nfunction getCookie(name) {\nconst value = `; ${document.cookie}`;\nconst parts = value.split(`; ${name}=`);\nif (parts.length === 2)\nreturn parts.pop().split(';').shift();\nreturn null;\n}\nfunction setCookie(cname, cvalue) {\nconst d = new Date();\nd.setTime(d.getTime() + (36500 * 24 * 60 * 60 * 1000));\nlet expires = \"expires=\"+ d.toUTCString();\ndocument.cookie = cname + \"=\" + cvalue + \";\" + expires + \";path=/;domain=.pegasko.art\";\n}\nfunction showToast(text, duration, appearDuration) {\nconsole.error(text)\nif (duration <= 0)\nthrow new Error('Invalid value for duration');\nif (appearDuration <= 0)\nthrow new Error('Invalid value for appearDuration');\n// Wrapper for centering\nvar element = document.createElement('div');\nelement.style.cssText = `\nposition: fixed;\ntransition: opacity ${ appearDuration }s, bottom ${ appearDuration }s ease-in-out;\npointer-events: none;\nuser-select: none;\nleft: 1rem;\nopacity: 0;\nbottom: -3rem;\n`;\nvar inner = document.createElement('div');\ninner.style.cssText = `\npointer-events: none;\nuser-select: none;\nfont-family: 'Gill Sans', 'Gill Sans MT', Calibri, 'Trebuchet MS', sans-serif;\nfont-size: 1rem;\npadding: 0.5rem;\nline-height: calc(1rem - 0.2rem);\nbox-sizing: border-box;\nbackground-color: white;\nborder: 0.1rem solid #77f;\ncolor: #77f;\nfont-weight: bold;\ntext-align: center;\nmargin-left: auto;\nmargin-right: auto;\n`;\ninner.textContent = text;\nelement.appendChild(inner);\ndocument.body.appendChild(element);\n// Wait & show\ngetComputedStyle(element).opacity;\nelement.style.bottom = '1rem';\nelement.style.opacity = '1';\n// Appear + visible delay\nsetTimeout(function() {\n// Make transparent and hide\nelement.style.bottom = '-3rem';\nelement.style.opacity = '0';\nsetTimeout(function() {\n// Dispose\ndocument.body.removeChild(element);\n}, appearDuration * 1000);\n}, appearDuration * 1000 + duration * 1000);\n}\nfunction saget(url) {\nreturn new Promise((resolve, reject) => {\nfetch(url)\n.then((response) => {\nif (response.status != 200) {\nreject({\n'status': 'error',\n'message': response.statusText\n});\nreturn;\n} else\nreturn response.json();\n})\n.then((data) => {\nif (data === undefined)\nreject({ status: 'error', message: 'empty response' });\nelse if (data['status'] != 'result')\nreject(data);\nelse\nresolve(data);\n})\n.catch((error) => {\nreject(error);\n})\n});\n};\nfunction htmlToElement(html) {\nvar template = document.createElement('template');\nhtml = html.trim();\ntemplate.innerHTML = html;\nreturn template.content.firstChild;\n};\nfunction fetchConfig() {\nreturn new Promise((resolve, reject) => {\nwindow.config = window.config ?? {};\nif (!(window.config && window.config.password)) {\nif (getCookie('password') === null) {\nsetCookie('password', '');\nwindow.config['password'] = '';\n} else {\nwindow.config['password'] = getCookie('password');\n}\n}\nsaget(`/config?password=${ encodeURIComponent(window.config['password']) }&action=get`)\n.then(async (data) => {\nresolve(window.config = data.config);\n})\n.catch((error) => {\nconsole.error(error);\nshowToast(error.message, 1, 1);\nreject(error);\n});\n});\n}\nfunction hideConfigDialog() {\ndocument.getElementById('dialog').style.display='none';\nstartFetchLoop();\n}\nasync function showConfigDialog() {\nstopFetchLoop();\nawait fetchConfig()\n.then(() => {})\n.catch(() => {});\ndocument.getElementById('dialog').style.display=null;\nvar form = document.getElementById('form');\nform.innerHTML = '';\nfor (const [key, value] of Object.entries(window.config)) {\nvar element = htmlToElement(`\n<div class=\"input-field\">\n<label>${ key }</label>\n<div>\n<input type=\"text\" value=\"${ value }\" id=\"input-${ key }\">\n<input type=\"button\" value=\"set\" id=\"submit-${ key }\">\n</div>\n</div>`);\nform.appendChild(element);\ndocument.querySelector(`#submit-${ key }`).addEventListener('click', function() {\nlet newValue = document.querySelector(`#input-${ key }`).value.trim();\nif (key === 'quality') {\nif (!(/^\\d+$/.test(newValue))) {\nshowToast('quality must be in range [1, 100]', 1, 1);\nreturn;\n}\nnewValue = parseInt(newValue);\nif (newValue < 1 || newValue > 100) {\nshowToast('quality must be in range [1, 100]', 1, 1);\nreturn;\n}\n} else if (key === 'fps') {\nif (!(/^\\d+$/.test(newValue))) {\nshowToast('fps must be in range [1, 60]', 1, 1);\nreturn;\n}\nnewValue = parseInt(newValue);\nif (newValue < 1 || newValue > 100) {\nshowToast('fps must be in range [1, 60]', 1, 1);\nreturn;\n}\n} else if (key === 'width') {\nif (!(/^\\d+$/.test(newValue))) {\nshowToast('width must be pair in range [16, inf]', 1, 1);\nreturn;\n}\nnewValue = parseInt(newValue);\nif (newValue < 16) {\nshowToast('width must be in range [16, inf]', 1, 1);\nreturn;\n}\n} else if (key === 'height') {\nif (!(/^\\d+$/.test(newValue))) {\nshowToast('height must be pair in range [16, inf]', 1, 1);\nreturn;\n}\nnewValue = parseInt(newValue);\nif (newValue < 16) {\nshowToast('height must be in range [16, inf]', 1, 1);\nreturn;\n}\n}\nvar old_password = window.config['password'];\nif (key === 'password') {\nsetCookie('password', newValue);\nwindow.config['password'] = newValue;\n}\nsaget(`/config?password=${ encodeURIComponent(old_password) }&action=set&key=${ key }&value=${ newValue }`)\n.then((data2) => {\nwindow.config[key] = data2.value;\ndocument.querySelector(`#input-${ key }`).value = data2.value;\nif (key === 'password') {\nhideConfigDialog();\n}\n})\n.catch((error) => {\nconsole.error(error);\nshowToast(error.message, 1, 1);\nif (key === 'password') {\nhideConfigDialog();\n}\n});\n});\n}\n}\nfunction stopFetchLoop() {\nwindow.fetchLoopRunning = window.fetchLoopRunning ?? false;\nif (window.fetchLoopRunning) {\nwindow.fetchLoopRunning = false;\nwindow.remoteSocketTs = 0;\n}\n}\nfunction startFetchLoop() {\n// Clear old\nwindow.fetchLoopRunning = window.fetchLoopRunning ?? false;\nstopFetchLoop();\n// Is started at least one loop\nwindow.fetchLoopRunning = true;\n// Delay steps\nvar frameDelay = 1000.0 / (window.config.fps ?? 1);\nvar inputDelay = 1000.0 / (window.config.ips ?? 1);\n// Last mouse vent time\nvar lastMouseTs = -1;\nvar connectToRemote = function() {\n// Current socket timestamp, prevent stacking\nvar remoteSocketTs = window.remoteSocketTs = Date.now();\n// Create socket for frames stream\nvar remoteSocket = null;\nvar frames_received = 0;\nvar closeConnection = function() {\nif (remoteSocket !== null) {\ntry { remoteSocket.close(); } catch {}\nremoteSocket = null;\n// Info\nshowToast('Connection closed', 1, 1);\n// Restart\nif (window.fetchLoopRunning && remoteSocketTs === window.remoteSocketTs)\nconnectToRemote();\n}\n};\n// Receive frames from server\nvar frame_worker = function(msg) {\nvar url = URL.createObjectURL(msg.data);\nvar frame = new Image();\nframe.onload = function() {\nURL.revokeObjectURL(url);\nwindow.canvasContext.drawImage(\nframe,\ncanvas.width / 2 - frame.width / 2,\ncanvas.height / 2 - frame.height / 2\n);\n// Update remote viewbox\nwindow.viewbox.width = frame.width;\nwindow.viewbox.height = frame.height;\n// Send ACK\ntry {\nremoteSocket.send('FA');\n} catch {}\n++frames_received;\n};\nframe.src = url;\n// Close connection on stop / disconnect\nif (remoteSocketTs !== window.remoteSocketTs) {\ncloseConnection();\n}\n};\n// Send input state & check open status\nvar input_worker = function(msg) {\n// Close on socket update\nif (remoteSocketTs !== window.remoteSocketTs) {\ncloseConnection();\nreturn;\n} else if (remoteSocket === null) {\nreturn;\n}\n// Update viewport\nif (canvas.width != window.innerWidth || canvas.height != window.innerHeight) {\ncanvas.width = window.innerWidth;\ncanvas.height = window.innerHeight;\n// Update props for view\nwindow.lineHeight = window.getComputedStyle(document.body).lineHeight;\nwindow.pageHeight = window.clientHeight;\nsaget(`/config?password=${ encodeURIComponent(window.config['password']) }&action=set&key=width&value=${ canvas.width }`)\n.then((data) => {})\n.catch((error) => {\nshowToast(error.message, 1, 1);\n});\nsaget(`/config?password=${ encodeURIComponent(window.config['password']) }&action=set&key=height&value=${ canvas.height }`)\n.then((data) => {})\n.catch((error) => {\nshowToast(error.message, 1, 1);\n});\n}\n// Send mouse location\nif (window.inputEvents.length !== 0 && frames_received !== 0) {\ntry {\nlet events = window.inputEvents;\nwindow.inputEvents = [];\nremoteSocket.send(JSON.stringify(events));\n} catch {}\n}\nsetTimeout(input_worker, inputDelay);\n};\n// Receive only\nconsole.info('connect to', `${ window.location.protocol === 'https:' ? 'wss' : 'ws' }://${ window.location.host }/connect_ws`)\nremoteSocket = new WebSocket(`${ window.location.protocol === 'https:' ? 'wss' : 'ws' }://${ window.location.host }/connect_ws?password=${ encodeURIComponent(window.config['password']) }`);\nremoteSocket.onmessage = frame_worker;\nremoteSocket.onopen = input_worker;\nremoteSocket.onclose = closeConnection;\n};\nconnectToRemote();\n}\nfunction startInputLoop() {\n// Input handler\nvar lastMouseMoveTs = Date.now();\nvar mouseMove = function(event) {\n// Fire only inside viewbox & with throttling to ips x 2\nif (window.viewbox.width === 0 || window.viewbox.height === 0 || Date.now() - lastMouseMoveTs <= 1000.0 / (window.config.ips ?? 1) || window.inputEvents.length > window.config.ips * 2)\nreturn;\nlet rectX = (window.canvas.clientWidth / 2) - (window.viewbox.width / 2);\nlet rectY = (window.canvas.clientHeight / 2) - (window.viewbox.height / 2);\nif (\nevent.pageX >= rectX && event.pageX <= rectX + window.viewbox.width &&\nevent.pageY >= rectY && event.pageY <= rectY + window.viewbox.height\n) {\nwindow.inputEvents.push([\nINPUT_EVENT_MOUSE_MOVE,\nevent.pageX - rectX,\nevent.pageY - rectY\n]);\nlastMouseMoveTs = Date.now();\n}\n};\n// Update props for view\nwindow.lineHeight = window.getComputedStyle(document.body).lineHeight;\nwindow.pageHeight = window.clientHeight;\n// Input handler\nvar lastMouseScrollTs = Date.now();\nvar mouseScroll = function(event) {\n// Fire only inside viewbox & with throttling to ips x 2\nif (window.viewbox.width === 0 || window.viewbox.height === 0 || Date.now() - lastMouseScrollTs <= 1000.0 / (window.config.ips ?? 1) || window.inputEvents.length > window.config.ips * 2)\nreturn;\nlet rectX = (window.canvas.clientWidth / 2) - (window.viewbox.width / 2);\nlet rectY = (window.canvas.clientHeight / 2) - (window.viewbox.height / 2);\nif (\nevent.pageX >= rectX && event.pageX <= rectX + window.viewbox.width &&\nevent.pageY >= rectY && event.pageY <= rectY + window.viewbox.height\n) {\n// Add event\nwindow.inputEvents.push([\nINPUT_EVENT_MOUSE_SCROLL,\nevent.pageX - rectX,\nevent.pageY - rectY,\n-event.deltaY\n]);\nlastMouseScrollTs = Date.now();\n}\n};\n// Input handler\nvar mouseDown = function(event) {\n// Stop event\nevent.preventDefault();\nevent.stopPropagation();\n// Check\nif (window.viewbox.width === 0 || window.viewbox.height === 0)\nreturn;\nlet rectX = (window.canvas.clientWidth / 2) - (window.viewbox.width / 2);\nlet rectY = (window.canvas.clientHeight / 2) - (window.viewbox.height / 2);\nif (\nevent.pageX >= rectX && event.pageX <= rectX + window.viewbox.width &&\nevent.pageY >= rectY && event.pageY <= rectY + window.viewbox.height\n) {\nwindow.inputEvents.push([\nINPUT_EVENT_MOUSE_DOWN,\nevent.pageX - rectX,\nevent.pageY - rectY,\nevent.button,\n]);\n}\n};\n// Input handler\nvar mouseUp = function(event) {\n// Stop event\nevent.preventDefault();\nevent.stopPropagation();\n// Check\nif (window.viewbox.width === 0 || window.viewbox.height === 0)\nreturn;\nlet rectX = (window.canvas.clientWidth / 2) - (window.viewbox.width / 2);\nlet rectY = (window.canvas.clientHeight / 2) - (window.viewbox.height / 2);\nwindow.inputEvents.push([\nINPUT_EVENT_MOUSE_UP,\nevent.pageX - rectX,\nevent.pageY - rectY,\nevent.button,\n]);\n};\n// Register listeners\nwindow.canvas.addEventListener('mousemove', mouseMove);\nwindow.canvas.addEventListener('wheel', mouseScroll);\nwindow.canvas.addEventListener('mousedown', mouseDown);\nwindow.canvas.addEventListener('mouseup', mouseUp);\nwindow.canvas.addEventListener('contextmenu', function(event) { event.preventDefault(); event.stopPropagation();  false; });\n}\n(async function() {\nwindow.canvas = document.getElementById('display');\nwindow.canvasContext = window.canvas.getContext('2d');\n// Input events buffer\nwindow.inputEvents = [];\n// Viewbox size\nwindow.viewbox = { width: 0, height: 0 };\n// Load config\nawait fetchConfig();\n// Start loops\nstartFetchLoop();\nstartInputLoop();\n}) ();\n</script>\n</body>\n</html>\n"


# handler for /
def get__root(request: aiohttp.web.Request):
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
