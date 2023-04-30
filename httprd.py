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

import json
import aiohttp
import aiohttp.web
import argparse
import base64
import PIL
import PIL.Image
import PIL.ImageGrab
import PIL.ImageChops
import pyautogui

from datetime import datetime

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

# Const config
DOWNSAMPLE = PIL.Image.BILINEAR
# Minimal amount of partial frames to be sent before sending full repaint frame to avoid fallback to full repaint on long delay channels
MIN_PARTIAL_FRAMES_BEFORE_FULL_REPAINT = 60
# Minimal amount of empty frames to be sent before sending full repaint frame to avoid fallback to full repaint on long delay channels
MIN_EMPTY_FRAMES_BEFORE_FULL_REPAINT = 120

# Input event types
INPUT_EVENT_MOUSE_MOVE   = 0
INPUT_EVENT_MOUSE_DOWN   = 1
INPUT_EVENT_MOUSE_UP     = 2
INPUT_EVENT_MOUSE_SCROLL = 3
INPUT_EVENT_KEY_DOWN     = 4
INPUT_EVENT_KEY_UP       = 5

# Failsafe disable
pyautogui.FAILSAFE = False

# Args
args = {}

# Real resolution
real_width, real_height = 0, 0

# Webapp
app: aiohttp.web.Application


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

	# Check access level
	has_control_access = args.password == request.query.get('password', '')
	has_view_access = args.view_password is not None and args.view_password == request.query.get('password', '')

	if has_control_access:
		access_level = 'CONTROL'
	elif has_view_access:
		access_level = 'VIEW'
	else:
		access_level = 'NO ACCESS'

	# Log request
	now = datetime.now()
	now = now.strftime("%d.%m.%Y-%H:%M:%S")
	print(f'[{ now }] { request.remote } { request.method } [{ access_level }] { request.path_qs }')

	# Open socket
	ws = aiohttp.web.WebSocketResponse()
	await ws.prepare(request)

	# Check access
	if not (has_control_access or has_view_access):
		await ws.close(code=4001, message=b'Unauthorized')
		return ws

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

		# Last screen frame
		last_frame = None
		# Track count of partial frames send since last full repaint frame send and prevent firing full frames on low internet
		partial_frames_since_last_full_repaint_frame = 0
		# Track count of empty frames send since last full repaint frame send and prevent firing full frames on low internet
		empty_frames_since_last_full_repaint_frame = 0

		# Store remote viewport size to force-push full repaint
		viewport_width = 0
		viewport_height = 0

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
							req_viewport_width = decode_int16(payload[0:2])
							req_viewport_height = decode_int16(payload[2:4])
							quality = decode_int8(payload[4:5])

							# Grab frame
							if args.fullscreen:
								image = PIL.ImageGrab.grab(bbox=None, include_layered_windows=False, all_screens=True)
							else:
								image = PIL.ImageGrab.grab()

							# Real dimensions
							real_width, real_height = image.width, image.height

							# Resize
							if image.width > req_viewport_width or image.height > req_viewport_height:
								image.thumbnail((req_viewport_width, req_viewport_height), DOWNSAMPLE)

							# Write header: frame response
							buffer.seek(0)
							buffer.write(encode_int8(0x02))
							buffer.write(encode_int16(real_width))
							buffer.write(encode_int16(real_height))

							# Compare frames
							if last_frame is not None:
								diff_bbox = PIL.ImageChops.difference(last_frame, image).getbbox()

							# Check if this is first frame of should force repaint full surface
							if last_frame is None or \
									viewport_width != req_viewport_width or \
									viewport_height != req_viewport_height or \
									partial_frames_since_last_full_repaint_frame > MIN_PARTIAL_FRAMES_BEFORE_FULL_REPAINT or \
									empty_frames_since_last_full_repaint_frame > MIN_EMPTY_FRAMES_BEFORE_FULL_REPAINT:
								buffer.write(encode_int8(0x01))

								# Write body
								image.save(fp=buffer, format='JPEG', quality=quality)
								last_frame = image

								viewport_width = req_viewport_width
								viewport_height = req_viewport_height
								partial_frames_since_last_full_repaint_frame = 0
								empty_frames_since_last_full_repaint_frame = 0

							# Send nop
							elif diff_bbox is None :
								buffer.write(encode_int8(0x00))
								empty_frames_since_last_full_repaint_frame += 1

							# Send partial repaint region
							else:
								buffer.write(encode_int8(0x02))
								buffer.write(encode_int16(diff_bbox[0])) # crop_x
								buffer.write(encode_int16(diff_bbox[1])) # crop_y

								# Write body
								cropped = image.crop(diff_bbox)
								cropped.save(fp=buffer, format='JPEG', quality=quality)
								last_frame = image
								partial_frames_since_last_full_repaint_frame += 1

							buflen = buffer.tell()
							buffer.seek(0)
							mbytes = buffer.read(buflen)

							await ws.send_bytes(mbytes)

						# Input request
						if packet_type == 0x03:

							# Skip non-control access
							if not has_control_access:
								continue

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
INDEX_CONTENT = base64.b85decode('JZN-nY(5G+Xk}q!J_<Z^X>@F5K1ftlP*OxZFLY^iY-K(QJZ)ukVIX5@VRCb2bUh+fR7Nd0B0dT{b98xZWj+dHVQzL|b0B*PcWGpFXgVM<Ffb)M3TS0%XJ~XfATcm7B|8dz3MwFb3TAI^bS-9KZE0+IIv{OtZf|pNVPj=G3TAI^bS-yfX=iA3Iv`?iY-BqMeF|uFZEP$cVsB)5AbSdTX=HS0Iv_DHFeN(*Xk}?<XmmOtF)%PCI|^`NWMpY>XF4D-I|^-Ka%X97Iv_AR3VjMAWNBe+Z)YHT3UF_8X>@6CZaN@gVsme7b#!Gr3U_H_bZ9ytF)%PCI|^uJX=iA3Iv_DHFeN(*dM#;gWMz0dAUQBFI|_7fa5^9`I|^)NW^_6rFgprlX>)LFVR<?rW^846I|^oOWq2)QX>w&_bZKvHIv`_jY;|pJI|^!bb98BDc`ajaZggdCbUGkoWo~q3aytrPY-wk1EopRRZF4#xV`Xl1WpX<TeF`IFX<=+{XCOWxBXekBWN&vMdkS!Gb7^#GZ*DpuVPbP{Y;|;HI|_GcWOQgcATcm7B|8deWoc(<bUGk0Ffb)M3VJPRZe(S6Iv_bQFgpr#Z*V#wFgprtWoC3bATT=$Vqs%zXL4_KZe%TEZ)|UJIv^u3Ffcbe3VjMAWNBe+Z)YGrAR}XMZggdCbRc^QdM#;gWMz0dAUQHHI|^`NWMpY>XF4D;a%F8h3SwbnYiDwAb#7!WV{dG4aylR*W@ctP3VjMAWNBe+Z)YGrAR}XMZggdCbRa$;BXntWY-J#O3TAI^bS-mfdSyBwF)lT7Wo<hOZDDd}X>KiIZ*+8TZ8{(@E;VvxZ958m3L|7`VQg<_AU+@?V{dMBWo~pJJ|H7zZ*pxQdkS}HWOQgcATcm7B|8dz3S?<^E@^IXb#yIeX=Q9=AbSdAX>)LFVR<?rW^846I|^oOWq2)QX>w&_bZKvHIv{dycRLDZY-M;YcXDBHIv{RucXDBHI|^!bb98BDc`ajaZggdCbUGk&aA9L*En;PKcV%U6I|_GcWOQgcAY)-{V<<5&FeM-@ATe@fZ7DknVsCO}WpX+obZBXAAaieQX=ETHVPR%F3UFa$WNB_^Iv_AEHF9NbI|^-Ka%X97En;tUbZ>1sATTa9a%F8h3VjM>X?8AYZg6#UEoNzDY-AulAZ%e`Wo#gO3UFa$WNB_^Iv_AEGBt8#Z958m3S?<^E@^IXb#yIeX=Q9=AU+^uX?7rc3S?<>aBN|DIv{3jWq3OZW^846Eo5nOWn*+{Z*Dpua&LD#3TA9&crABwVQ@MiZf|#TVQ@PNXJK$UATTa9a%F8h3VjM>X?8AYZg6#UEoNzDY-AulAY^HFAU+^zZg6#UAbSd8VPk7&a&L8RWG!QFY;STpAR}dEWjhLDZ*pX1aylS%XlZUBb8l>EWFRA9VP-oDVsCgYb7^{MZf80mVsCO}WpXWIZ+JTjaA9O*X>MmaATTa6HF9NbI|_XYWNCITX>M?JbS-9SWo%?1J|JXib|5|=X>M?JbUJ8nc4cxPdkSK2cr9~iVPtQ2Iv^uAH)bF(aCjgvaCjgvaCjgvE-`XtZ958YaA9L<ba^@;FfKPWI|_XYWNCITX>M?JbS-9SWo%?1J|JXib|5|=X>M?JbUI;UbZK^FAbSd8Z*pX1aylS%XlZUBb8l>EWFR9qH)cBueF|i0b}ngdaCLMoW@%+?WFS5uWNCIFJ|JmsaCLNBba`-PJtAUtbaZcSB3(LRV{~bDWgvSBVqs%zXL4_KZe%TEZ)|UJIv^uAH)T5teF`IEZ*FF3XCQkDaBp*IbZKvHIv`<Ub8l>QbY(jVcWGpFXgVM<a%F8h3TS0%XJ~XfATe@fZ958LZ*+8TZ8{(^a%F8h3UX;@XmmOtF>+;XI|^@hWpZY0Z+AK%XlZ0*Wo|nPW^ZnEEpuslWjY`+a%F8h3S)J0b8m7wAaHMKZggdGI|_AkWpXWZWo%_*bUGkzZ*FBf3VjMZFLQKxY-K(QJTGWvVPrlEJYsKTc|Hm}V_|M~VRIm9WIZBeX>)LFVR<4x3Op}kVQzL|b3O_@WNCIFX=FVjWNBe+Z)YMPb98xZWj!KfX>)LFVR<@kZ*FBfB0dT{WNCIFX=FVjV{dMBWo~pLJ_<Z!X?7rKWIZBuX>@F5B0dU3Z*FF3X9_$oWNCIj3Or<Kb|7hFJtAgra&00$3Op}lX?8veJTGKvc0LL`WNCIFX=FVjb7)~?Z+9XfZ*F63X=7_WB4}x3WkYXnW@%?cX<=+{XDBH<B0fAXWNCIj3Op}lX?8veJY;EhAZcVhB4clEW@%?4Aa8DCY-wX_JtA{xZ+Am)Zf0p`L}_7cZ)YeeJ0d>fnwdN=WNCIj3OsXTa%pgMAar?fWj!KvWq5QiYGHO^b7OL8aC9OdVRLzIV?GKfVRLzIV<2XAZew(5Z*CwcDIj|aFE1cNZ*Oa9Wpf~5V`F7=a|&j4Zew(5Z*CxGWpqPtZ)<5~C~jeGWho$g3S)0>b95kfVQh6}AUz;pJ0K)`WN%}2ZDnqBE@N+RYiVVDU^@z9Z*FsRAaG%FbaNm*Aa-GFb!9GdaBOLGC}2AvBztaQZDoBuU@1EaX=WfOaA9(Eb1rOUZfA68AU!=jATlWma%FUNa&91SVRCeHE^u#fC@C&;aBOLGC?`87DK2wpX=ZdNDLV>sWps6NZXj-TY-~FUeF|oEZew(5Z*CxSWpqPtZ)<5~C}VD6ZDlMVV|HO|b!90adkSN3ZgX@XWFS2tZe@2ML}7GgC@DJ%WG-`MbW~|=Whi7WXJvF$X>DaFDIhB#C^I%SFfbr0ATl%{Dj+s6ASxg>Fd!-*F)%PNDJeS&Y-MyHWq5FDa%FQMJs=`wcyMWQWph0uD<EVpbZ=EuLsN8eX>MmIDLV>eZ)0_BWo~pXV{dP3X=NZiAYdeWAY*P}ZDk;RJtTV|V|HO|b!8xZJ0yD`Wq5FDa%FQMeLHYrbZ9*<J7jNdVQFqXE+l&(cWG{9Z+9+iZ)0I}X>V>WXm4|LZeeX@Abnsv3VjMMFCbM(AZ2W2ZDnqBa|&j4Zew(5Z*CxSXm58^Z((zED0F3bbSxlbb#h^JX>V>UAYpKDWnpqeb#h^JX>V>RAbSd9Z*FsMY-KKKZf0*NbY*ySDLV>jW*{hJb#h^JX>V>IJUt*VDGGFGa&LDaZe@2MMRIa)awsQBZgyd8X=EUFVQh6}AZBlJAY^rNVRUJ4ZYL=_3Tb8_C}D7LWnpqeb#h^JX>V>IJUt*VDGGFGa&LDaZe@2MMRIa)awsQBZgyd8X=EUFVQh6}AZBlJAYpKDWnpqeb#h^JX>V>PDLV=;FCbTPVQ_F|av)}Jav)=6ZggdGX>Ml<c42ZLWo%__Wo~pJJs@OnV|8t1ZgehVa%Ew3WkqadZDnqBC?{lTb|)!23T13%ZDnqBE^~BwY-KKEb8}Q>cyu5=AYck`Z*ysMX>V>iAZBTJWn?=FbaG*Cb7^#GZ*DpuZ*XB_X>@rYBzquXaByW|azu4<VRUJ4ZXkVgEFfZUbaZcRAS8PrVQ_F|VRA%ua$$67Z*Cxcb0B45b7d`QZY^(hbUO-gZ)t9HWpXWLc4cmKb2=byZ*FBf3Uza3axHUZY-M9~Iv{RuZe=?PY-MJ2Iv_D}Wo<hOZ*XB_X>@ryATT=$VsCVGZ*4jtEi-auZ958JI|_DTav*7LZe?;HJs@OnV|8t1ZgehVa%Ew3WkqadZDnqBC?{lTb|)!23TbX`WpXZaba`xLE@N|ZRAqQ{AUz;p3UF^}ZggdGEoF9PZgg`xAZ~ANWjhLWb7gWZb7gF0V{|$oZf|a7I|^oRZgeeXVQpz_c{(5`M`>(qAX8y(b0;hyCr4>)Y#>u%ZgU__R3|JTLt$)bVsdFLASYCEWny(>Xk~ODO;aZ<Aah}Eb1idaa%pBe3TAI^bS-mfdSyBwF>+;XI|^`NWMpY>XF4D-E;VvxZ958VX>MgLXk}?<XmmOtV_|G#C^2$nZ6GZmFfKB3Wo;=t3Sw_~EpuslX>MmaAYyNFWMy(KVsCgm3SwbnYiDwAb#7!WV{dG4aylS)XlZn1I|^cNa%5$4Iv_AEF>+;XAaieQX=ETHH#cTG3S)0<Z*n>yBR4l@I|^oRZgeeoWoc(<bUGknZ){{c3Up<7bS+_QX=iRaAY)~2bY*fo3T<I>XK8LNY-MJ2Iv`<nbZ<KfZDDd}X>KiYX=iA3Iv`<nbZ<KfU^@zFZf<3AE_7vhbVF}$bY*UIAUz;-Wq5Qu3T13%ZDnqBE@5zRWo~3cXlZO@C~0nPWpXJy3S@6%b!}yCbS`3VWO*)OaByXAWJ73aY-A{9Y-Md_ZgeR-3NJ4pS7B*%ASNJlXm584XJvFlZ*6dObY)~yba`xLC}nJAZDnqBDK2ktVPk1@c{>VaY-Md_Zgehlba`xLE@E$VbZ>1SJs>ABa%F8NI|^lNWo>0{bS`srd2D4aZ*XB_X>@rYJs>ABCp!u+FCam1aAjd~AS)ntX>(~}Y-J#1Wo%)23Ug(2RB3HxZ*_DiW_503bZKvHASfvydkQZvAWdOwWgv8NVQzD9VRB_|bRc1FWFTm1WMv9vY-Md_Zgehlba`xLE@E$VbZ>1SJs>A7Gje5ZCp!vdY-Md_Zgehlba`xLE^lyQV`+4GAUz-_Fef_-b7gc?X>Db1b#y3Zb#7yHX>V>IC@CO&3NJ4pL}_zyZ*ye|WN%}2ZDnqBE@E$Fc`kBgZEtpELuhGiWGH29Wo>0{bSXOueJmhhaByW|azu4<VRUJ4ZXhZkF)%PNDLV>%EFfWUaAjd~M0IjubZKvHASxg+FfcG6D<EWba$$67Z*CweATcm7Fey6<eF|oEZew(5Z*CxHbZu-@Z$)fnZDnqBC}?zTY$+gn3U*;~AarGIaBN|8WgtBuWN%}2ZDnqBE@N_KVRU6hY-Md_ZgeOobY*RDY+-a|Cn-A$Xmo9CAUz;xbZu-dbaH8JC@DJ%bY*RDY+-a|E@^IVWpYSVO-vv?AZT=LY&!~aWps6NZXk4JZE$R1bY(7MZ*FvDZgehYX>xOPLuhGiWIGCdI|^oXZew(5Z*CxHX=G(XZ*FF3XGCdXY;R{MDIj|aWN%}2ZDnqBE@x$QMQmklWo~prc}Zj_CuC`1Y;R{LDK2w#d2D4aWNCA7Y+-plCvI<UWhXldb97;HbXRY3Yh`jwZ*OoYDLV>%3So13Zet*3b#7yHX>V>Ib7*gOLvL<oX=g-fVQg<_C@CO&3UhRCa93|~Yh`jwZ*OoYDLV>eZ)0_BWo~pXXJvFnY-Md_ZgfI<Nn|J|WNBe+Z)YbdE^~BwY-KKFX>)LFVR=1nb!==q3U*;~AZBlJZ6G}$WN%}2ZDnqBE@x$QMQmklWo~prc}Zj_CuVPQZ6_%^3TAI|Z7ykUZe?;vR834EJs>A1I|^oRav&&UZ*FsRAX{r?c`P7yVQh6}T_A5}AWvdyWn*+MWo~qGX=QULXJvF>aB^>Ob0{e(DIj|ac42ZLWo%__Wo~pJJs@awZERF;MQmklWo~pRU<y2BX?7rEY+-YAJtAptaCLMoW@%+?WFkHaJZxcNWo$kqdmw9Nc_4i}FKl6AWo$kQJY;EhJ_<Z(Zg6#UAar?fWj!KvWq5QVAa-GFb!9ywBzqusVQh6}AblbrX=FVjX>M?JbS)%%AZulLAblb}3Os3UaCLMbba`-PJtAUtbaZcSA|Q5QY;|QlB6DSQA|PpGJtA{;Vr^-3EhKv&Yh`&LeIh;zJTGKvc0LL`FJx(UK42+33TAI|Z7yMOaAj^}LuhGiWGH29Wo>0{bSXOuWN%}2ZDnqBE^&2ba(Po_Y-M9~Z*nMLBXf0PZE18ZBzquhWqBZdU@0zPWMoBlWo~p#X>)XCZe?;PCu3}BV{0cYAZB%LV{~b6ZXhTrAbSdIWpp5JWp`F#Y;|QIJs@OnV|8t1Zgehjb!BpSQ)O&rV{~tFC}1OLZg6#UEhKv&Yh`&LePAgrc42IFWiE7bX>BMeI|_4UbYF0CZ*VAUWqB+hZe@2?VQh6}DLV>tXm58^Z((zEC}2}%bRc$NY;|QIW^ZyJBzquhWqBZdU@RapEFdu{I|_X%I|_XYeF`rxAVO(wWFT~JAZKiEVqt6wcWG{9Z+9+pXm58zZ*FF3XGCdXY;R{EJs@*vZ+Am)Zf0p`L}_7cZ)ZCScWG{9Z+9+eX=G(XZ*FF3XGCdXY;R{EJs@alWMxBdZf0p`L}_7cZ)ZCSFE1cNZ*F#Fa&#bKd30rS3TAa~V{~b6ZXjf3V{c?-UukZ1I4ERcbYUzYZ)RpGAbSdOWps6NZXje~bYWX>W@cSG3VjM@b#7yHX>V>IWMyM-WMyAzZgep=C}d%DVJskTW@afMdkS)8bairWAY@^5VOwuzW?dkBASh&EbYWX>W@aEOATeDaJUk#cDLV>%3TAa~V{~b6ZXjf3V{c?-UukZ1GBhY;VRT_EAa7=7DIj|aa%FUNa&91GVRT_zZ)Rp)AbcPwWMOn+TW@A&AS)m-T_8L>AUG)?d>|-fVRT_zZ)Rp7D<CpmAUr%EF*YeX3VjM@b#7yHX>V>IWo~0{WMyAzZge;(X)GXNa&jynZ)RpGAbSd7a&lX5W@cR=Js@cyCLl0)W@bAIeF|oEZew(5Z*CxEZewp`WnXD-bTKw4X)GXNa&jynZ)RpGAbSd7a&lX5W@cR=Js@cyCLl0)W@bAIVRCX?Z)Rp7D<CmlAUz-`X&^p6AUG)?CLl0)W@bAIeF|oEZew(5Z*CxEZewp`WnXD-bTTw3X)GXNa&jynZ)RpGAbSd7a&lX5W@cR=Js@cyCLl0)W@bAIVRCX?Z)Rp7D<CmlAUz-`X&^p6AUG)?CLl0)W@bAIVRCX?Z)Rp7D<CpmAUz-`X&^p6ATc&6ASNI%cxGlh3VjMMFCa{BV_|F{VQ_FDb97;JWeRp-av))Fa3DP(dwn|!FE1cdZ)0m^bRa7rY;SLHAaitKbY%)*aBwbiWo>VCWm9isYh`pGJs@s%Y-~FUVQ_FRcW-iQWpYe!Z*Wv|AUz;3I|^ZNa4vUma%*LBOmA;+Q)q8>Y-Cb(ZXi7%W?^h|WjhKlFCa;7aCLMbcW-iQWpZ;0W_503bZKvHAaitKa&$><aCLM{Z*OoYDIj|aFE1cTZg6#UAZ2!CZge1Yd2nTO3S)0>b95j{PEb`;Uqx0$PE=n_PgPSzUrkR|MIay`Js>bU3S)0>b95j{PEb`;Uqx0$PE=n_PgPSzUqnw=P9Pv4Js>eV3S)0>b95j{PEb`;Uqx0$PE=n_PgPSzUsX^bARr(hJs>hW3S)0>b95j{PEb`;Uqx0$PE=n_PgPSzUsFR;PfSc8Js>kX3S)0>b95j{PEb`;Uqx0$PE=n@MOj}&PghPLARr(hJs>nY3S)0>b95j{PEb`;Uqx0$PE=n@MOj}}P#_>6ARr(hJs>qZ3S)0>b8l>AE@^INZzv~obYXIINp5g;bWCq=a40DtIv{g&VRCe7Zf78AZg6#UAZ%}Ma3?7{3NJ4pO<`~#N>d<fYj0#_AarjaaCu>MbZ=*MX$oU+ZgX@XYI9U?P<cybd2=8=AbScTQFU*0Wg<EtA}1m&3L;Q!b#o#*AR;RwED9n+Z*6U1B03-<EFvrlB28&-b#o#*AR;XyED9n}WpZh6WFk5sA}%5<3L;Z%VRL9AIv^r1A}k6bL}_PfbTA@1AR;g#ED9n-X=iD4F(Nu3A~7N?3L->lXK8dYB03-<G9oMrB1CCtX>>CpIv^r5A}k6bL}_PfbTlG5AR;s(ED9n-X=iD4H6l77A~hl`3L->lXK8dcB03-<HX<wvB1CCtX>>OtIv^r9A}k6bL}_PfbT}e9AR;&-ED9n-X=iD4IU+hBA~_-~3L;ZwZE0g~Y;SHNIv^rDA}k6bMR9duY$7@!B0VB33L-*sVPk7$bWCMtbRs$+B3mLX3L-*bV{3D4VRL9AIv^rkTp}zAB0_RuV{2t}QfX&sbRs$+B3&XZ3L-*bV{37BZ**lMIv^roA}k6bOJ#XMB03-<VInLFB1>g?LLxdKB4Q#e3L;Boc|#&PAR=QTED9n^WqCv*Iv^rsA}k6bOJ#XQB03-<Wg;vJB1>g?Mj|>OB4#2i3L;Boc}F5TAR=cXED9n^WqC*<Iv^rwA}k6bOJ#XUB03-<X(B8NB1>g?N+LQSB5EQm3L;Boc}pTXAR=obED9n^WqC{@Iv^r!A}k6bOJ#XYB03-<Z6YiRB1>g?P9i!WB5ooq3L;Boc~2rbAR=!fED9n^WqD8{Iv^r&A}k6bOJ#XcB03-<aUv`VB1>g?QX)DaB61=u3L;Boc~c@fAR==jED9n^WqDL0Iv^r+A}k6bOJ#XgB03-<bs{VZB1>g?Rw6neB6cDy3L;Boc~>GjAR>1nED9n^WqDX4Iv^r=A}k6bOJ#XkB03-<c_J(dB1>g?S|U0iB6=b$3L;K*ZE#^^L1bhiIv^rpWMm>N3L-&lbWCMtbRs$+B4KQFY-MJ2A}k6bL2PtVX=iA3B03-<VQh4AX=iA3A}k6bLSbWTb8ul}Wg<EtB4S}<Yjbd6V`U;N3L-*sZ+CNLazbHaYa%)zB4ToHcXMTOVqs%zA}k6bLUM0+b7gWyVRmnIa%psBb0Rt*B4ToHcXMTOW?^=3a%psBb0RDXB0_R+cXMTOMsIR=VRB?5Iv^rqa&LEYWpZY3a(7{JWFjmIB0_R+cXMTONN;UrB03-<VsdYHb7gX9Z*65FED9n*a&LEYWpYwwW^!e7Xd*fwB4ToHcXMTOa%E<6WpijEED9n*a&LEYWpYzxVRB<=B03-<VsdYHb7gXKWnpq-Xd)~MB0_R+cXMTOQ*>`|B03-<VsdYHb7gXKbZ>AXED9n+VQ_OyZ)0mBIv^rrVQ_P7Z)0mBED9n+Z*F#Fa&#g(AR=RLZgypIbRsMYB13O(baHQOOl4+tB03-<V{~$CY-MJ2A}k6bLvL<$a&K%>X=iA3B03-<V{~$Ca%pF1bRsMYB2IN}aA9ObWn*b=VQeBgAR=UCV`*(+Y$7ZQB1C0uWprgCIv^rsWo%`1Wg;vJB2IN}aA9ObX?AI3Wg<EtB4lZHX=G(0ED9n)a&m8XL~nO)B03-<WN&wFA}k6bMQ&swIv^rtZe$`X3L-^rbY*fPIv^rtZggdGA}k6bMRQ|eaAhJoAR=XRV_|S*A}k6bMlm8fAR=ZlA}k6bMlvEgAR=ZmA}k6bMl&KhAR=ZnA}k6bMl>QiAR=ZoA}k6bMl~WjAR=ZpA}k6bMm8ckAR=ZqA}k6bMmHilAR=ZrA}k6bMmQomAR=ZsA}k6bMmZunAR=ZtA}k6bMlmoVIv^ruF)$)53L-`^F(Nu3B4#l$A}k6bMlmuXIv^ruF)|`73L-`^Ga@=5B4#l&A}k6bMlm!ZIv^ruF*G793L-`^H6l77B4#l)A}k6bMlm)bIv^ruF*YJB3L-`^HzGP9B4#l+A}k6bMlm=dIv^ruF*qVD3L-`^IU+hBB4#l;A}k6bMlvuWIv^ruGB6@63L-`_F(Nu3B4#o%A}k6bMlv!YIv^ruGBP483L-`_Ga@=5B4#o(A}k6bMlv)aIv^ruGBhGA3L-{sB03-<W^N)Z3L;2lY;YnvAR=gGY;Ynh3L;2vZDk@lAR=gQZDk@X3L;5vb7gXLB03-<X>N06a&#gr3L;BkZedMtWMv{cAR=pFZeb!U3L;Elb#7y5L2z&}B03-<Y+-e7V`yP;a4{k*3L;Elb#7y5L2z&~B03-<Y+-e7V`yP;a55q+3L;Elb#7y5O<`$lB03-<Y+-e7V`yz*X>1}a3L;HqWNBejWo%_*bRs$+B5YxGZewU|Wn^h#b7gF0V{{@c3L-&ra&LD`WoC3DIv^r!WoC3DED9n{b!~8AWKDH!bZKyGc_KO>B5ieSbZKyGc_J(dB28svX<<}yVPk7fWq5QVIv^r$Wq5RSa$#d@A}k6bPH%2QZ*F#Fa&#g(AR=yWZewq5c4cyOA}k6bPIYZ?VPr5OIv^r$b!{*rED9n{b!~8AWHBN-AR=ycZ80J&3L;K*ZE#^^G9o%4B5rkUG9oMrB2IN}aA9OKB03-<Zgp)lA}k6bPIYZ?VPrHSIv^r$b!{{vED9n{b!~8AWHll>AR=ycZ8ah+3L;K*ZE#^^HX=G8B5rkUHX<wvB2IN}aA9OOB03-<Zgp)pA}k6bPIYZ?VPrTWIv^r$b!|8zED9n{b!~8AWH}-_AR=ycZ8;(=3L;K*ZA@=tYa%)zB5rkUY;R+0A}k6bP+@0fL~nO)B03-<aA9X<WN&wFA}k6bP+@0fRd6CYAR=&KXJvJ8A}k6bP+@g*Wg<EtB5+}Kb7dkd3L;HqWNBegY+-p&VRdt5B03-<aBN|DaA9?GWg;vJB28svX<<}yVPk7ha%FaDZ*_AbIv^r&a%FaOa$#d@A}k6bP;zN*bRs$+B5-nPZge6n3L;Q)X>N2=V{&C>ZX!A$B5-nPZgg{Fa%E+1A}k6bL2`0$cT#C*XmlbvAR=;UXJ~XHED9o1V{&h7Y)o%sYa%)zB6DMMZ)|LAZ)0mBED9o1Wo%_*bRs$+B6DSIWn*+AED9o1XlZ72Ol4+tB03-<b7*O1bZlj2bRsMYB2#E-W^__%XJ~XHIv^r*XlZ72a%pF1bRsMYB2#Q-WpE-oAR=>YWo2+8ED9o1aA9L*B03-<b8ul}Wg;vJB28svX<<`zZ*U?yAR=>gZ*U?k3L;K*ZE#^^Q*~l=a$#e1B03-<b9G{La$#e1A}k6bRAFKwIv^r+VPYaI3L-&ra&LE4a3VS&B6V;gED9n)b!2I8R&Q)|ZDmAncWxp&AR=~eY;|pAWN&wFA}k6bR&Q)|ZDmAncWxp&AR=~eY;|pAWN&wFA}k6bL3LzlZ&q(?b!}x$b#!GSIv^r;Z)|mKWo>nIWg;vJB0+UzX>V3<Y;|pARd6CYAR=~eY;|pAb#Njq3L;i-Y;|pARd6CYAR=~eY;|pAb#Njq3L;NaOl4+tB03-<cWG{HWoC3DED9n`WprUoWoC3DIv^r<X>M#~W^^Jf3L;NaQfX&sbRs$+B6n$Sa%pF1bRsMYB28s<VNz*lXmlbvAR>2ZZgOd7Xmlbh3L;5vbZl8=ZX!A$B6($QA}k7hI|?r^AVPI#FGgiybairNIv`VMX=ZdxWoC3Bb#NeVZ*(AZa%pF0WpZU?AZcbGQ)p>sbW&+&XmlWHb0BbXWpi_7WGo<IZe$>KX=7y|c4cyNVG1uVAX9H_b#!TOZaN@FX>w&Ca%F5~VRL05W^ZyJVsCV4AZulLa|(80av)P^X=ZdxWoC3xa%FRKWn>^dAZB4~b7eaUc42ZLQ)p>sbW&+&Xmn6=Wpi_7WFS2tW?^h|WjhLCaBwbZZg6#UMRsLwbaNm*AX{BK3Sn??E^=jUZ**lKJs^7^cWGpFXgVM;EFfrQX=iA3Iv_A0eLD&-FCa;7aCLMbXkl(-Y-Mr^c42ZLY+-YBO>cE`Wle8(WmIz@Js?D3bY(7XZ+9puI|^ZNa4v0cb#rA+Z+2xxc4cmKNMUYdY-MsFJs@UvZew(5Z*CwcWp-t5bSWTv3NJ4pMrm?oAa8DLc_3+Sb7^E{Aa-eGcVcgNASNJpX>@2HbZByKbaZTKZf78LZy;%Kb0ByiG74#CASeo9aBwbmX=QhCZ*p`lcWGpFXdpd3Js>b3e0&OFaBwbmX=QhCZ*p`lXk}?<XmlVwJv|^WAbflZL}7GgE^cpkC@CN<AZ%fCbWLw{b7f6$c4bs^AUr)FF)%PNE-)Z3ASh>LbYF0CZ*V9lX>fBVDIh;TATcQ*e0&OFaBwbZZg6#UMRsLwbaO6jWo~D5XdpfyXJvF>aB^>OC?{!fb0;YvDj+fnDIjuXbairWI|^)NbRcqNV{}*`Js>DyaBwbTVQzL|b1q|SX=QG7S7~H)Xdo{jGASS}AShvQa4vRfWp{9Ia&#_tX=HS0ATJ;?DLV>mWpp5NWn*+%AUz-`VQ_FRV_|M~VRJ5HY-wd~bVy}sXJ~XFFCa20AT1y$VQ_FRc4=jIaBp&SE@)+GXJ~XFFCa20I|^xLASeoDc4cmKE^uLIWmq6SJs@&rV{}*`CMF<dc4cmKE^uLIWmq6QJs@&rV{}*`D<ENTa4vRfWp{9Ia&#_tX=HS0ASNaXWp-t5bS`jVXJuI+K0P3EWn*+%ASNatWp-t5bS`jVXJuI+JUt+CWn*+%AS)nYaBwbmX=QhCZ*p`lXk}?<XmkoGAbSdIWpp5NWnpYsAUz;WVRUFNa&L8RWGE<Qc4cmKE^uLIWmq6BAaZ46bXX}MDj;ESa4vFXZEtjCE_Z2UbZ8(iAYpKDE_P{UcW`fVbS`&kWOQgLI|^)NbRcqNVQg6-Js?eCbZ9PeZ*^{DC@5uiWo~pXaA9X<Ss*PSa%E$5St%eYAYpKDE^=jUZ**lYXk}?<XmlViAYpKDE_P{UcW`fVbS`LRX=iA3DLV>baBwbZZg6#UMRsLwbaO6nb#rJaTM9`|P*qf4MOH;lR9{U`RZ~S@O;1)uEDCaEVQg3|3UXy(Y*`9jDLV>mVRLj%Z*_BJO>cH(RC6FbAVgtwWiD=ScPJ@43VjNFI|?r^AW3d;b#x$TVQyq>WpWC3VR9gBVRLj%Z*_BJQ)6;(Y;06>AUz;NVRU6KZf|!eDLV>baBwbdZ*_BJQ)6;(Y-~k#Wo~pxVQyq>WpW@rAZB%LV{~b6ZXhUSc4cmKDIj|aFE1cQX>w&CZ*FXPAZc!MX=G&}c4=jIVsCgLCLnidbZ8)SXmW3KbZlvEXCQQMAZc)OAb21$3Tb8_C<<Y4a4vRfWp{9Ia&#_tX=HS0AU!=jATS_&d<tQ3a4vRfWp{9Ia&#_eWoc(<bRa!FJs>b3e0&N-VRU6KZf|!eDIhH%Y+-YBO>cE`Wm98vZ)|K-b09oDATcm7FfK44FCZvqWprO~a&K@bCuwkVCn+F5KOiwFAbflZVQ_FRX>M?JbVYV$Zgg`lY-Mg|bZ8(xAZKNCUvP47a408faC0XqASxg-3Mn9RWps6NZaWHWWpp5NWn*+$AUz-`VQ_FRV_|M~VRJ5HY-wd~bXRF)bZ8(iATlW+Eg&diaBwbmX=QhCZ*p`lcWGpFXdo{jGATO>Y-MyHa%E$5Ss*<iC}D7LE@NSCc42caV{B<<ZgfaxX=iA3ATJ;?DIhH%C}D7LE_P{UcW`fVbS`LRX=iA3ATJ;?DLV>jW*{gEWp-t5bS`jVXJuF*K0P3EWn*+$ASNatWp-t5bS`jVXJuF*JUt+CWn*+$AS)nYaBwbmX=QhCZ*p`lcWGpFXdosg3T1X>ZgehiVP|DoAU-`Ha%E$5Ss*4RAZ2!CZgehiVP|DoAUr)Fa%E$5Ss*JQVQ_FRc4=jIaBp&SE@)+GXJ~W^DIj|aY-MyHa%Ev`SRg$hO<{CsE^=>mZe%DZWp-t5bS`jVXJuF*Eg*7bV{}+4ASxhXaBwbiWo>VCWiEGVWOQgCFCbxXa4vRfWp{9Ia&#_tX=HS0DLV>mWpp5NWnpYtAUz;WVRUFNa&L8RWGE<Qc4cmKE^uLIWmzCCAaZ46bXh4NDj;ESa4vFXZEtjCE@)+GXJ~XFFCbxXa4vRfWp{9Ia&#_eWoc(<bSXOuVQ_FRX>M?JbVYV$Zgg`laCLKNC|e3iPEb`;Uqx0$PE=n_PgPSzUsFR;PfScK3UXy(Y*;J`a%Ev`Su6@IWp-t5bS`9NY;<8+3SB8X3T$C>bWLw{b7fOwa&K&GRC6FbAVgtwWiD=ScPJ@43VjNFI|?r^AW3d;b#x$TVQyq>WpWB(aBwbdZ*_BJL~nO)MRsLwbVy-tWNc+}AUz;vb#7yHX>V>IC}nnKZgeRidkQZvAX9X2a3E!NWo~o|Wp-t5bS`jmWp-t5bVOxlVRdYDC@DJ%Wp-t5bS`srZ*Wj@Z*XB}VRUJ4ZYU``3NJ4pLuh4VYYJ&*AShvQa4vFXZEtjCE_Z2UbZ8(wJv|^WAbflvVQ_FRa%F9AbY(7RWoc(<bRa!FJs>bC3UXz1b#iVy3T$O`AaZ46bXXugAShvQa4utEZgydFE@NzIWo~p=X=HS0ATJ;?DIhH%C}D7LE_P{UcW`fVbS`&kWOQgCFCa20I|^)NbRcqNV{};{Js>DyaBwbTVQzL|b1q|SX=QG7NM&hfXmlViATlW+Eg&diaBwbmX=QhCZ*p`lXk}?<XmlViATlXC3Tb8_C<<kEWo~pXaA9X<SRg(<AaZ46bXXuJCLm>YWo~pXaA9X<SRgz-AaZ46bXXuOAYpKDE_P{UcW`fVbS`&kWOQgCCMF7Hc4cmKE^uLIWmzCTJs@&rV{};{CMF<dc4cmKE^uLIWmzCRJs@&rV{};{D<ENTa4vRfWp{9Ia&#_eWoc(<bP6dTdkSo2bRcqNVQg3+Js?eCbZ9PeZ*^{DC@5uiWo~pXaA9X<SRgGRa%E$5SScVXAYpKDE^=jUZ**lYcWGpFXdo{jVQ_FRc4=jIaBp&SE_Z2UbZ99%3T$O`AaZ44Y*`>ZAWdO(XfASZb#7!RC}nnKZgehiVP|DoAT1zrWn*+%DIh8!VQ_FRa%F9AbY(7RWoc(<bRaJvVQ_FRc4=jIaBp&SE@)+GXJ~XOI|^ZNa4u<XaCLM=c4cmKb1raob7&}A3Q0~-Ra9R^Rz*%!UrkR{Q$=4yPghPX3UXy(Y*;J`a%Ev`Su6@=c4cmKE@E|bbZ>4f3SB8X3VjNFI|?r^AW3d;b#x$TVQyq>WpWB(aBwbdZ*_BJRd7XiWo~pxVQyq>WpW@rAZB%LV{~b6ZXhUSc4cmKDIj|aFE1cdbZ>AVWp-t5bP8p5Wo~pXaB^jKWo~ptWoBV@Y;-6oI|^lXWo~pXb98TTP;zf@VP|1<X>V>QDLV=;FCar`Wn*g!X=WfOVQ_FRa%F9AbY(7gX=HS0AU!=jATS_&d>~<Pa4vFXZEtjCE@)+GXJ~XFJv}`jFewUhWps6NZaWHWWpp5NWn*+$AUz-`VQ_FRV_|M~VRJ5HY-wd~bXRF)bZ8(iATlW+Eg&diaBwbmX=QhCZ*p`lcWGpFXdo{jGATO>Y-MyHa%E$5Ss*<iC}D7LE@NSCc42caV{B<<ZgfaxX=iA3ATJ;?DIhH%C}D7LE_P{UcW`fVbS`LRX=iA3ATJ;?DLV>mWpp5NWnpYsAUz;WVRUFNa&L8RWGE<Qc4cmKE^uLIWmq6BAaZ46bXX}MDj;ESa4vFXZEtjCE_Z2UbZ8(iAYpKDE_P{UcW`fVbS`&kWOQgLI|^)NbRcqNVQg6-Js?eCbZ9PeZ*^{DC@5uiWo~pXaA9X<Ss*PSa%E$5St%eYAYpKDE^=jUZ**lYXk}?<XmlViAYpKDE_P{UcW`fVbS`LRX=iA3DLV>baBwbZZg6#UMRsLwbaO6nb#rJaTM9`|P*qf4MOH;lR9{U`RZ~S@RZuJna%Ev`SS$*1WnpYtEDB|IWo~pXVs&(MZ*D9KT`4;XeLD&-FCas2ZggdMbRcbIZgn7Yb#QQUWpi^1VQ_FRV{dMBWq5Q=Wo~stVQyn(Y(;iuZgfatZe(m_av(h*W_503bZKvHASh*aWo~pSAbSdBc4cmKE^u;Xc4cmKL}g}Sb!>DfDLV>fc4cmKE^~Bma8Pn@aA9X*bZKvHC@DJ%a%FUNa&91IVQh0{I|_X}3NJ4pOJ#X;AW3d;b#x$TVQyq>WpWC3VR9gBVRLj#WqCwzcWzX3AUz;NVRU6KZf|!eDLV>baBwbbWqCwzcWy;?Wo~pxVQyq>WpW@rAZB%LV{~b6ZXhUSc4cmKDIj|aWp-t5bS`jmWp-t5bVOxlVRdYDC@DJ%Wp-t5bS`srZ*Wj@Z*XB}VRUJ4ZYU``3R7rlW^_ztW^_<;Wpi_7WFS2tQ)p>sbWCMtbWn0-b8}^6AbflvWp-t5bS`6WWMv>dJv|^NQ)p>sbWCMtbSFCsQ)p>sbW&+&Xmn6=Wpi_7WFS2tQ)p>sbW&+&Xmn6=Wpi_7WFUNeAZ2!CZgehVZ)9a4Jv}`jCsSx?W^__%XJ~XMI|?r^AW(8;c4cmKAaZ4JWprtDX?A5GWp-t5baNncaA9q2X>Ml<X=WfO3T1X>ZgehkWpHI-bRZ@sASen%VRU6KZf|!eDIhH%Y+-YBOJ#XPZ+C7~b09oDATcm7FfK44FCZvqWprO~a&K@bCuwkVCn+F5KOiwFAbflZVQ_FRX>M?JbVYV$Zgg`lY-Mg|bZ8(xAZKNCUvP47a408faC0XqASxg-3MmRHAaZ4Nb#iVy3Tb8_C}nnKZgehVZ)9a4X>K5Db5w6oc}rz^b15Ku3Sn??E@^IXb#z5`Wo~qHE^u{oXee6>Nls8zR9{6_MNU*-OGQ~<L{C>vEDCCKRBupuOJ#X;TV-}-ZgehVZ)9a%3SB8X3T$C>bWLw{b7fOwa&K&GRC6FbAVgtwWiD=ScPJ@43VjN4Wps6NZXjl1Y;$Eh3Vk~YFE1cVWqETTNp5g;bRcM9Ze(m_atdK^a4u_Qc~x*lc4cmKNMUYdY-MsFJs@UvZew(5Z*CwcWp-t5bSWTv3T1X>Zgehia%Fa9ZgfOtW?^+~bSNo13T1X>ZgehlbZ>A_a&K^9XJK?{Z*C|lI|^xLASh*aWo~pXV{c?-AU!=jASY93X=ZdxWoC3IAbflvWp-t5bS`6WWMv>dJv|^NQ)p>sbW&+&XmlqjAbSdFW*{h2XlZ72Ol4+tP;zB+b7f>HAbSd7aBwbZZg6#UMRsLwbaO6nb#rJaTM9`|P*qf4MOH;lR9{O)SzlF9EDCCKRBupuOJ#X;TPIU!X=ZdxWoC3IT?$<(I|_XYX=WfOQ)p>sbW&+&Xmn6=Wpi_7WGNtf3Sn??E@^IXb#z5`Wo~qHE^u{oXee6>Nls8zR9{6_MNU*-OGQ~<RZuJnYI9U?P<cybd2?GQQ)p>sbW&+&XmlrC3SB8X3VjM}VRLj%Z*_BJQ)6;(Y;06>AUz;NVRU6KZf|!eDLV>%AZ2WGWguy0ASh*aWo~pXV{c?-AZczOYI9U?P<cybd2=ZsdkSH2a4u<XaCLM=c4cmKb1raob7&}A3Q0~-Ra9R^Rz*%!UrR+<UsX^n3TksyZ%}zlWqEU3Wp-t5bS`6WWMy3nT`4;XY+-YBO>cE`Wm98vZ)|K-b09q+L}7GgE^cpkC@DJ%eF}1AbairWAZB4~b7eaUeLD&-FCbE7XK8bEWpW^FX>)XCZe?<F3Sn??E@NSCc42caVPs@Qc4cmKOlfm;Wo~71C?{=ib#rBHZ+2xTEFfWUa4v0cb#rA+Z+2xxc4cmKNMUYdY-MsOI|^ZNa4utEZgydFE@5P3MRsLwbWCY;bY*U3awsQvXk}$=CoCXgaBwbdZ*_BJQ)6;(Y-~k#Wo~pxVQyq>WpXJy3Sn??E@NSCc42caVPs@Qc4cmKOlfm;Wo~71C?{=ib#rB8Z+C7dEFfWUa4v0cb#rAzZ+C7*c4cmKNMUYdY-MsOI|^ZNa4utEZgydFE@5P3MRsLwbWCY;bY*U3awsQlZ*_BJb#NyvAYpKDE^Tjhb7fU<MRsLwbVy-tWNc+}DLV>baBwbTVQzL|b1q?IWJPvmZgfm(b97~HWpXGdV{dMBWq5RLWo~sREFfWUa4utSZggdMbWLS$bwgopV`Xebc4cmKNMUYdY-MsOI|^iPV|8t1ZgehTWMoBlWo~p#X>)XCZe?;PCu?PSWN&wFCoCXgaBwbbWqCwzcWy;?Wo~pxVQyq>WpXJy3S@6%b!}yCbS`0JWJPvmZgfm(b97~HWpXGdYh`(La3?GvVQ_FRYh`&=a7A`yZgfatZe(m_aw$6seLD(fb#7yHX>V>Ib98TTNp5g;bWCq=a40DtdkSN3ZgX#JWiDxMW^X7bb98TTNp5g;bWCq=a40DtIv{g&Z*XvFZf78AZg6#UAZ%}Ma3?7{3Sn??E@NSCc42caa%F9Ac4b9&Wo~p#X>)XCZe?;PCv9(ab7gIBc4a3lAYpKDE^Tjhb7f6$c4b9&Wo~pxVQyq>WpXJy3Sn??E@NSCc42caa%F9Ac4b9&Wo~p#X>)XCZe?;PCwFLNWo#!bAYpKDE^Tjhb7fOwa&K&GMRsLwbVy-tWNc+}DLV>baBwbTVQzL|b1rgaZEtpEMRsLwbWCY;bY*U3awsQlZ*_BJWN&wFCoCXgaBwbdZ*_BJL~nO)MRsLwbVy-tWNc+}DLV>baBwbTVQzL|b1rgaZEtpEMRsLwbWCY;bY*U3awsQlZ*_BJb#NyvAYpKDE^Tjhb7fU<MRsLwbVy-tWNc+}DLV>baBwbTVQzL|b1rgaZEtpEMRsLwbWCY;bY*U3awsQbZ*FvDcyw)LZgnRtAYpKDE@N+QbY*ySO=WI%Lt$=XWo$)uWo~pxVQyq>WpXJy3S@6%b!}yCbS`pbZEtpEMRsLwbWCY;bY*U3awsQjWqD+8cWx&vAYpKDE^B3ZL~nO)MRsLwbVy-tWNc+}DLV>eZ)0_BWo~pXa%F9Ac4b9&Wo~p#X>)XCZe?;PCu?PSb#NyvAYpKDE^B3ZRd7XiWo~pxVQyq>WpXJy3Sn??E@^IXb#z5`Wo~qHAUz;kT{{YW3NJ4pL}g}Sb!>DXVQ_FRaB^>Oa|&Z`ZgX@XL`6nHRZLW0P*P7&Qy@JcdkQCTVRLhLZ*pWOIv^(}ED9%abzy92ba^K_AU8EE3MXc8b0<0=GB7L(CuwkVCpsWC3Vk~YVQ_FRaB^>Ob09q+Zgp&II|?r^AaHVTaC0DGV`F7=a|&j4Zew(5Z*CxIZfSI1aB^>Ob0{ewdkSH2a4v9iZ*X%UJs^7^E-o%aMMgnYOjKV`QcqA*AbmRubaHthdkS`8av*GPV_|Gia&K^RAUz;TQ%_DVaA9(DWhiWKV_|GlbZ>HDXJsyDWpqh&Wo;-YXmoUNa%3%GaBwbga&K^RCn+gA3TAI|ASh#RZgX@XTWe)`EFfQ9Aa7<MPhx6iV{|TMZgg^KWpgNDaBwbga&K^RDJdX(3U*;~Aa)=<AZ%}AVQf%xZ*X&4Yh`&|I|^xLASiYz3Sn??E^u;haC2L0WqDm7Js@^F3VjNFAY);4V`w0IeF}X$3TAa~V{~b6ZXjo6bYF0CZ*VAXVQpn8AbSdOWps6NZXjWBa4v9iZ*X&4ZeeX@T{{YWI|^oXZew(5Z*CxGWprO~a&K^RC@CO&3UXz1b#iVXVQ_FRaB^>Ob2|!s3TAa~V{~b6ZXk1IbYF0CZ*VAXVQpnBAa-GFb!90adkSf0ASiBOZDk-mJv|^NaA9+EcW-iJCn+F%3U*;^b!8wuAa-GFb!9Gea%pWSDLV>%AZ2WGWguy0ASiBOZDk-mJv|^NadlyAX>@rfDIj|abaHthdkS`8Y;|QIJs@yla&u)#ZgePiVQh6}DLV>%AY);4V`v~KWho$g3Up|4Z+9SWWp^M&a&m8SC?|1sVQgu1c_3|db95kLWguy8AaY@DXJsH;F)Sc4Ffd&wDLV>%3Tb8_D0X3Nb!8wtATc0(d?0pVY;|QIJ|HnLFewUjXmW3NAZ}%MAVqR=Z*nLnadlyAX>@rYZFO^WAYx@8X>K5LVQyz-AX_mkATcm7T_-6!3Vk4DY;$EGX=WfOZeeX@AU!=jASY&Ub0;YvdkS=Nc_4cVc42IFWgtBuaA9(DWl3&yD0X3Nb!90#3Vk4BVRU0?ASh)iAbSdQXmW3NAZ}%MAVqR=Z*nLnW^i*LZFO^WAYx@8X>K5LVQyz-AX_mkAZc!9T_-6!3VjM`W*{hbVQh6}AUq&3DGGFGa&LDaZe@2MMRIa)awsQeaC0DSb#rteVr3v{ZXj}DZf9j6TQOZHDLV>%AZ2WGWguy0ASiBOZDk-mJv|^NX>fBVDIj|abaHthdkS`8Y;|QIJs@yla&u)#ZgePiVQh6}DLV>%AY);4V`v~KWho$g3Up|4Z+9SWWp^M&a&m8SC?{!fb0BSXb95kLWguy8AaY@DXJsH;F)ScyZf0F4DLV>%3Tb8_D0X3Nb!8wtATcQlbZByKcOY(McOXS_a&K}dCuwkVAZ>MXbRc47AZczOa$#;~WguHIT_-6!3VjM;aBwbga&K^RTW(=(WnCaWAa-GFb!9sWY;R*>Y*Tb^a$#p>E^}pcNpxjxC?{xibZ~NHEn#qQE^u;haC0XtAWBnDPA+qFa%pa7X=ZsSVQ_FRaB^>Ob15l13Vk~YFE1chZ*ps8atda3Zew(5Z*CxSbZ>B1Z*ps8a!hY;a40DtdkSN3ZgX#JWiDxMW^X7bb98TTS8sA_WpYe!Z*V9nAUYs(bZ>BQX>MmAY;SLHCn-A$baHthdkQZvAWdO%YancIZ*U-Cb0BVSbRcqNb97;HbZKs93Sn??E_ZKoYh`jwZ*Oo@Xm53FWKwl*AUz;vVQh0{I|^ZNa4vFXZEtjCQ*UEyWpplMY;SXAC@DJ%eIR3DbYo~BC}k-idkSN3ZgX#JWiDlMa&K}dWhpxfeF}XFW_503bZKvHAaitKa&%X3a%*LBOmA;+C@CO&3NJ4pO<{6tAZ%}Ma3EoGAaZ4MbYXIIX>Ml<VQ_FRcW-iQWpYe!Z*Ws+Z*^>BQgv=1Js@;)b!9sWX>D+9UvqR}a&%X3a%*LBOmA;+C@DJ%eF|oEZew(5Z*CxIZE$Q~b97;HbXRY3Yh`jwZ*OoYDIj|aV{dMAZ){~QX>Mk3C?|7tVRCd=Z*ps8a!hY;a40DtIv{g&VRCe7Zf78DZ*OoXDLV=;FCbHNZ*U-GcxiKVX>MmAY;SLH3U*;~AZ%}Ma8z?3Js?D3bY(7XZ+9puI|^xLAShvQa4vUma%*LBOmA;+RC6F9Jv|^WDGG9BbairWI|?r^AVX+nV{0I3W*}^DZ*U-Kb0BkcX>4pDa&>NQX>Ml<W_503bZKvHAY*7{V{1%rZ*V9nAbSdOWps6NZXj%LZ*Wv|AU!=jAYpKDE_ZKoYh`jwZ*Oo^b2|!s3NJ4pP<3K#X>({GZe@2MY;SLHAarSMWpi|4ZEy-<aBwbnZ*ps8a!hY;a8z?3Js@mvZ*Wv|I|?r^AVX|rVR9g3X>Db0b7^mGb0BYKAY^58YjkgL3Sn??E^=jUZ**lKJs^7^cWGpFXgVM;EFfrQX=iA3Iv_A0eLD(aaBwbmX=QhCZ*p`XJs^7^cWGpFXgVM;EFfrQX=iA3Iv_A0eLD(vVR9gHWpQ<7b96>>VQpm~Js@UvZew(5Z*CwcDIj|ac42ZLa%FLKWpi{!a$#*{UteN%W@cq_AUz;%Wp^M|X>N2lL2`0oc_=X;D<Co;D<Co;D<Cl`I|?r^AX95;a3E=BAZ%}Ma3E=OAY*KAb7f=-X=WfOA!BG|V{1%rZ*V9nDGG9BbairWI|_7ic_4cVFE1cdWo~33aA9L>Wpp5Pd2nS4Wo~0{WMyAzZge;(FnBOAEFf}aadl;LbVhPvZDn6yVs&O_WpXSaFey6<FE1cdWo~33cWGpFXdosaXk}?<Xmko}Wpp5RX=QhCZ*p{BcWGpFXdpcxO<{CsE^T3WC{1B>XfADOZYXzYZe(wFE@^IVWpY<(WOQgOAT~8MGc_qJATcRB3T$O`Aa-eGcW`fVbYEy?X=iA3AUz;WVRUFNZDDvQO<{CsE^TRUD0gXYWN&vaX>M+1a!6%qXJ~XRAT~8MGc_qJATcRB3T$O`AaQkJY-x0PAUz;WVRUFNZDDvQO<{CsE^TRUC}(AKUvP47a408nbzy92ba^K!EFdv3Fexk`F)2F=Wo~0{WMyAzZgep=D0XROcW`fVbYFLAWOQgOAaZ4Kb!BsOMsi_oWnW)nb!KK|ax5S*DLV>fZewp`WnXD-bTKw4c4=jIaBp&SUub1%XJ~XRAaZ4Kb!BsOMsi_oWnW)nb!KK|ax5S-DLV>fZewp`WnXD-bT}w+bzy92ba^Zwa%FLKWpi{!a$#*{UteN%W@cq_EFd*0I|?r^AVFhvbzy95c`P7vWo~2&VQ_FRa%F9AbY)X-V{2t}E^}pWWGHfFadl;LbVhPvZDn6yVs&O_WpXZJb!KK|aw$6seIR3DbYo~BC}k-idkSN3ZgX#JWiDlMa&K}dWhpxfeF|h{Y-Mz1AaZ4Kb!BsOMsi_oWnW)nb!KK|aytrrI|_DTav*SZb7)C!aCLMbJs@UvZew(5Z*CwcDIj|aFE1cdYiV#GX=Wg7Z*OoQX>%ZBY;SXAWD03!ASfYYXk}w-OmA;+C@Cola%FUNa&9{cbaHthdkSf0AShvQa4u<XaCLM=c4cmKb1rOUZfA68DIj|ac42ZLX>M?JbVYV$Zgg`XJs@Fla4u<XaCLM=c4cmKb2|!QaBwbZZg6#UMRsLwbaNm*AX{BK3U*;~AaiAMX<=+>dSzrFJs?U`Pfjj#baH8KXK7}6C~0nRb#z5`Wo~qHDLV>wVR9gHWpQ<7b96~=aCLNFUt)D;W@U09Js@sncOX@1Zge<7a&lpLC@~-_AaiAMX<=+>dSzrTY-Mg|bZ99%3NJ4pQ)O;sAaG%0Yh`pGba`-P3T19%Z)9a(X>N2lC@^?1Gb|u-WpQ<7b96~=aCLNFUt)D;W@U0LATTLA3NJ4pQ)O;sAY@^5VG3q%av&&dWpp5EZe$=mATT>1X>Md7JRoyra%o{~X?kU3E^K9PXLM*gAS)|rZe%G6a%FLKWpi{%Zg6#UUteN%W@cq_TWM}&AS)m-T_8Omb7gXAVQgu7Wn?a6Xkl_gZ)9abbSP<VWGOoeFE1cLV{~<4Y;1WfAaiAIWC~$$a4vFXZEtjCQ*UEyWpplcWo~3Ba%FLKWpi{%Zg6#UUteN%W@cq_E@E|NW@U0II|^iFY-Mz1AaZ4Kb!BsONp5g;bYEX$b!KK|aytrr3Vk4BVRU0?ASh)iAbSd9Z*FsMY-KKGa&m8SC}k--3VjN5Wpq?&ZDntDbSQ9jb7)C!aCLMnATcm7FfK44FCb@SbYF0CZ*V9lX>fBVDJeS&eLD(vVR9gKaAaY0Wm9Q-WgtBuW_503bZKvHASfvydkQZvAW~&>X?kTKV_|M~VRH&;W*{hGaBwbTVQzL|b1rvjWOQgCAw3{>X>Me1cP?peZe?;;X=HS0AbflvVQ_FRV_|M~VRJ5MWoc(<bRZ!;Aa`kQWN&vaX>M+1a!6%qXJ~XOAbSd7aBwbTVQzL|b1rvjWOQgCJs@{!Ze(wFE@^IVWpY<(WOQgd3Sn??E@NSCc42caXk}?<XmlVwAa`kQWN&vaX>M+1a!6%qXJ~Xg3NJ4pRd8fsbY&o6aBwbga&K^RAZBlJAa-eGcM4%}a4u|VZe>ViX=iA3AUz;=X>Me1cP?jTbVF}#aCLNLWK(o`Y-K29Z)0_BWo~pXVsB)5DK2bjZe>ViX=iA3I|^ZNa4v9RXJtrbX=iA3AUz;=X>Me1cP?XWX=QG7NM&hfXmmRYeF}X$3TAa~V{~b6ZXj)QXJ2SxZe(m_awu(cXDJ|i3Tb8_C~b3RE@WYJVIXO4b97;DV`Xn<AVG3+VR=GzW@cq_DIj|ac42ZLWMOn+AUz;%Wp^M|X>N2lL2`0oc_?jjXD(!6bYUqw3U*;~AaG%0Yh`p_ba`-PAUz;tWn*t-WnXD-bT}wvVRT_EATTLA3NJ4pMsi_oWeRC#ASiHQV{2t}UvznJWgtC0ATW3^GAST?3UqRLAbScgFCbNLWMOn=AaZ4GZ**lKWMy+}bZ>AVaB^>OWpZ?BWpfH)aBwbiWo>VCWiEGVWOQgCJs@OdV{c?-UukZ1F*Yb<VRT_EATcRB3Sn??E^=jUZ**lYXk}?<XmlVwAY^4@Z)9a(X>N2eHYj9abYUzYGbuX?c42ZLW^!R|WnXl8aAhDpAY^4@Z)9a(X>N2lC}d%DVJsjuDLV=;FCa;0Zf|mBAZ2ZEba^0Va$#*{a|&r@ASh&EbYU)RWo~D5Xdpd3Js>t9e0(5ga$#*{UvznJWgtC0Js>c6Ffb_~dkQZvAW~&5a%FLKWpi{OZe@2MW^!R|Wguy8AToF$X>N37a&}>C3Ug(2RB3HxZ*_Dia%FLKWpi{!a$#*{EFdy4FfcAKATJ<iWprO~a&K@bCuVSSCn+gA3UXz1b#iVy3VjMMFCa#BY-}KMWn^+;cM54{ASh;XVQpn!ba`-PAU!=jATW3^F)1K>3NJ4pM`d&%W^!R|Wguc~Z(<5|VR9g1Y;R&9Js@sncOXJ+Z(=B0AY@^5VJ>rQX=7z5HYp%oEFgOzba`-PIv^)$ZDD6+FKTdQXD1+iDLV>wVR9gKa%><yAXQRKE@N_KVRU6rVrpe$bX8JJC}M1HVktWcFE1cOWnpY=Z)0I}WeQ|vY-Mz1AYyE9Vmk_UVR9g5a$#*{AUz;%Wp^M+ZDD6+C@DJ%W^!R|WiD@SY;R#?AUz;vb#7yHX>V>IC@CO&3RO}}E^=jdZ);^wVrpe$bX8JJD0OmdDLV=;FCbNLWMOn=AY)-}c42cMb7^{IAZcbGa%FLKX>w&`3UzQ~VRU6vX?kTSDLV>baBwbTVQzL|b3<=#bY*ySE@X0HcS&twXJse~W^!R|Wh@F|aBwbTVQzL|b1rvjWOQgCFCa1?Eg)ucVQpnDcWGpFXdo{jGAs&VaBwbTVQzL|b1rCQX=iA3ATJ;?AT1zfa$#*{E@)+GXJ~XFFCa1sDLV=;FCbE7V`XV}Wn>^`a$#*{AaiMYWeQ<%a4vRfWp{9Ia&#_tX=HS0AUz;va$#*{E_Z2UbZ9#YVQ_FRc4=jIaBp&SE@)+GXJ~XFJs@UsVQpnDXk}?<XmmRYFE1ccWo%_(b7cx-Wo%`1Wgup9VQpnQ3NJ4pQe`c2WpQ<7b95kXWp^NEa$#*{AZczOX>N37a&}>C3Ug(2RB3HxZ*_Dia%FLKWpi{!a$#*{EFdv3FfcAKATJ<iWprO~a&K@bCuVSSCn+gA3Vk~YW^!R|WiE4aV<0^sb#iPw3NJ4pP+@X(X<=+2a%E(4VRs6BAZ2WGWguy0ASh;XVQpn!ba`-PAU!=jATW3^GAST?3U*;~AY*cGa9?;JJs@OdV{c?-UukZ1F*Yb<VRT_EAT}vG3U*;~AY*cGa9?>KJs@OdV{c?-UukZ1F*Yb<VRT_EAUG*I3NJ4pM`d&%W^!R|Wguc~Z(<5|VR9g1Y;R&9Js@sncOXJ+Z(=B0AY@^5VJ>rQX=7z5F)%40T`VAbAar?fWjY`yX>DO=WiM)QWoIWKeJMK%c42ZLb#iPVJs?$5OfF+`Wnpw>Phx6iV{}zgOekV(Z(=Ds3NJ4pL}g)YY;R*>bY%);Wo%`1Wguc~Z(=(Nc42ZLW^!R|WgtBuZe@2MNo`?gWhf~-3TAR)ZDlTRZftL1WFS2tW_503bZKvHASfvydkR%jOfGU|c5iECPhx6iV{}zgOel47Y$-bmFE1cfaAaY0Wguf=ZgydFAaiMYWguy0AaZ4Kb!l>CWD0d~WMOn=Q)zl-C@DJ%VQ_FRV_|M~VRJ)oZggdMbS`9aVRuPwVP|D13TAR)ZDlM9VQ_FRV_|M~VRJ5bX=HS0ATJ;?AT1zaaBwbmX=QhCZ*p`lcWGpFXdo{jG9W7;V{&hBUwAAEVQ_FRV_|M~VRJ5MWoc(<bRaJvG9WD=VQ_FRc4=jIaBp&SE@)+GXJ~XFFCa1?D<ETXZ*X6E3Mo4ZFE1ccWo%_(b7cx-Wo%`1Wgup9VQpnQ3NJ4pQe`c2WpQ<7b95kXWp^NEa$#*{AZczOX>N37a&}>C3Ug(2RB3HxZ*_Dia%FLKWpi{!a$#*{EFdv3FfcAKATJ<iWprO~a&K@bCuVSSCn+gA3Vk~YW^!R|WiE4aV<0^sb#iPw3VjNFAY);4V`v~KWho$g3S)0>b8l>AE@g6ZZ*nMQDLV=;FCbE7EplaXb!BsOAZ}%MAZBu5ZDk;7ZXjuHbY*gOVQe5_b0B76X>4U=3Ug(2RB3HxZ*_Dia%FLKWpi{!a$#*{EFdv3FfcAKATJ<iWprO~a&K@bCuVSSCn+gA3VjNF3NJ4pL}g)YY;R*>bY%);Wo%`1Wgui>bYVLReF}X$3TAa~V{~b6ZXj=PWo}<+VQyq>WpXHGc4cmKDIj|aV{dMAZ){~QX>Mk3C?{`lWo}<+VQyq>WpXGfAUYs(bYXIIWn>_1Z*OoXDLV>jW*{gbV`yb#YfNu%a40D$3UXz1b#iVy3NJ4pLvL+xb#!GQb7^{IAY^H6a|(5EWMOn=Q)zl-C@DJ%FE1cTZg6#UAa`$aYh`kC3UhQ}a&$><aCLM{Z*OoYDLV=;FCb85a%pd5X=5N}a$#*{AaHMNYzlK_bW~|=Wp8zKC~{?Sb!BsOMsi_oWh@{uFfcGKFd#1=XJvF>aB^>OC?{rcb0;Y&I|?r^AX9W<a&#bRZg6#UAaHeaXk~H=b7gc?X>Db1b#y3jb#rJ*Zg6#UEFdv3FfcAKATJ<iWprO~a&K@bCuwkVCn+gA3S@6%b!}yCbS`vhbZliHJs>AYR8&w>L?9?bZ*Fd7V{~O?DJMG$eLD(fb#7yHX>V>IV{C78WnXAvZe(m_awuhXWo~pSAbSd9Z*FsMY-KKKZf0*NCu3}Hb7fy>VQyq>WpXGfAUYs(bZ>BQX>MmAY;SLHCn-A$b98TTNp5g;bWCq=a40D|3Tb8_C}nnKZgehVZ)9a4Jv}`jG%zqRDGGCFZ+BF0VRLjSCrNI0VQgt+AaG%Gb9ZlYWG5^jF)Sc4DLV>fY;$D_b7*gORBvH(bSNi7Z*Fd7V{~b6ZXjc9Z*yg2CoCW_EFdu{I|_DTav*YLb97;HbXRY3Yh`jwZ*OoQJs@UvZew(5Z*CwcDIj|aVQ_FRcW-iQWpYe!Z*Wv|AUz;3I|^xLAShvQa4vUma%*LBOmA;+Q)q8>Y-Cb(ZYc_BZE$Q~b97;HbXRY3Yh`jwZ*OoYDLV>%I|^xLASh*aWo~pXV{c?-AU!=jAT%&AF)0djWpq?&ZDntDbSQFVb97;HbXRY3Yh`jwZ*OocAT=;BFey6<Wo&b03Ug(2RB3HxZ*_Dia%FRLVRCd=Z*ps8a!hY;a4aA)H83eV3S@6%b!}yCbS`vhbZliHJs>AYR8&w>L?9?cX>((5Zf<2`bY)~ICp!v#I|?r^AVY6%Ze?S1AaieHYh`o_V{dMAZ){~QX>Mk3C?{iYZf<2`bRcwZCoCXfBzqutX>Me1cP?yiV_|e@Z*DGda&L5RV{dFAJv}`jCunqZaC15*AU_}{cXM+mAUYr?cXKBoeL62MBzqutX>Me1cP?yiV_|e@Z*DGVZ*z1YeJ^8gZf<2`bYFLKU?~b=aBwbiWo>VCWm9isYh`pGJs@sncOX|~VpDHpYh`pOU?h7WcWG{9Z+9+iZ)0I}X>V>WaB^>SZ)0z4AU!=jASY;abZ~PzCm=r{CwFsmCm=c?CwFrvAbmP7FC=>)cWG{9Z+9+iZ)0I}X>V>WXm4|LAbl@mZ*Fd7V{~74b3brlb8~lZa%4Rudmv?QV{c?-RZ>YqZ*6dIZe?zCC}(AKUvP47a408mVRLhLZ*pWODJdX*U@1EaVQ_FRa%F9AbY)X-V{2t}E@EkJVRCs?d2nSQJs>Axa&lpLVs&O_WpXDw3Sn??E^=jUZ**l-Z)0m^bS`ghZDn(FVP|C^Js@p!XJ2SxZe(m_aytrPaBwbiWo>VCWm9isYh`pUZ*FgJWo{rnAa8JGZeM6&Ze(m_aytrPaBwbiWo>VCWm9isYh`pUZ*F63Z*yfJJs@LjZ*yf|Xkl(-Y-Msg3VjM;aBwbTVQzL|b09q+WN%}2ZDnqBE@x$QMQmklWo~prc}Zj_CuC`JaBN|DCn-A$VQ_FRV_|M~VRJ)oZggdMbRaz-VQ_FRV_|M~VRJ5LWpqPtZggdMbSNh>WG5**3NJ4pNp5L$AY*TCW@%>%X>MtBUvP47aC0ar3NJ4pQ*>c+bRcYRZ*X%8b97;HbXRY3Yh`jwZ*OoYDLV>%DIh2*I|@86b7OL8aCANjJTGEzWO+UcJTGW;ZEQXY'.encode()).decode('utf-8')


# handler for /
async def get__root(request: aiohttp.web.Request):

	# Log request
	now = datetime.now()
	now = now.strftime("%d.%m.%Y-%H:%M:%S")
	print(f'[{ now }] { request.remote } { request.method } { request.path_qs }')

	# Page
	return aiohttp.web.Response(body=INDEX_CONTENT, content_type='text/html', status=200, charset='utf-8')


if __name__ == '__main__':
	# Args
	parser = argparse.ArgumentParser(description='Process some integers.')
	parser.add_argument('--port', type=int, default=7417, metavar='{1..65535}', choices=range(1, 65535), help='server port')
	parser.add_argument('--password', type=str, default=None, help='password for remote control session')
	parser.add_argument('--view_password', type=str, default=None, help='password for view only session (can only be set if --password is set)')
	parser.add_argument('--fullscreen', action='store_true', default=False, help='enable multi-display screen capture')
	args = parser.parse_args()

	# Post-process args
	if args.password is None:
		args.password = ''
	else:
		args.password = args.password.strip()

	# If password not set, but password for view is set, ignore view mode
	if args.password == '':
		args.view_password = ''

	# Set up server
	app = aiohttp.web.Application()

	# Routes
	app.router.add_get('/connect_ws', get__connect_ws)
	app.router.add_get('/', get__root)

	# Listen
	aiohttp.web.run_app(app=app, port=args.port)
