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
import gzip
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
INDEX_CONTENT = gzip.decompress(base64.b85decode('ABzY8UT;oh0{`ti?N-~!lK=e_J<Q#)JTb<00!aWfCkbJ~Oh~u^Cd1}(K$cpzf-HITf$?N`kln}IC)xVwmRhn5B$+vTc6ZK6u&TPcy1Kfmy81)f9;cIFr?EYjwhKS0p9XU0)$#G+yWQ<Ih8o)wncBh`+i@b(!OF?;-s+PTKqu)Oiyb?>vJ>&CG4tJYyeWF!?!Rp{#?l{+Qw`lTz6Sy?3e#23p7_ChQ%s^TN~X3Ww-`TD6^2pZVg(ee4Ujv(o%PgqT|XRcitbipV#gytq{o}ax2|so(Fkf!qr^}B2ny_B5(QZbZQjE1_f<c1<@Kg`+66{wG$n?C^iq0o{bU;0&;u`!*C0InU3L9fI^2W<4NStV#>Xs4eQ&<%K<8u#RUP;%<E@4r_@i*uPvs;jkcGHA;@f0wyV2}x1n<=eKD3?7Q5<EV3ql1^yeYozcDs)&jVpPf^@3f$Yv{Qb%SkOW&-0e>f=99?Z3Bt_y97p^bz;S%wmOW`G@5{<3cZYz7sZpRN!-AwTy^|#nt_4&GH`+5j&X{knL^HD8#EP0_**k>WTdM@na-pPbNxGZ;8@tWSS5|LKqXP^N>D^P_Co=~<-6ir+xAQoy{pI7UhIg#9?Aesq2SS9P7RnZ@A(LU+hjrRJA!b~N{t0bDQ{83FuKN(r(Q4-XyAJ5GrFc4-yBC*GDZ=QBRLB+{`j#cy3_08e}^qKg7NdyIkJDeem8F$v~(qj`C>nyjXODfNas^ISQ%z8xZ%o~!EjMwj~`1ibTBs0zbjNxZ*q@kFH{!9P;3^Vio5_dGxl8<E!T8CB5|@fK{2M0&rv8#U`!GjfTf^Wge2VdnhNyWYigFjpbS4M@cZsyMOhrq3TuppC)5cSq8G0SVw2Nu68{y0PI;V(qNo9g7o;Cx4h2Rrh5~An(UnrRj1>`uPT)J2gB4JOWKO#zlGTFtmsI8vF)AnHXtq%Kzy9+-bj8Gp{b?$A@Kbr6u6?vY4}@J2b}|p0orXof!plPPMPapI%-7b$izvGEWg=|HkxA0X%SSThA}h2f5{W^sBo$Y7kV!ER7hB>VpWMjFCZM>EL!>tsTZ(FG$7upoMBM?c`>91WklD@P0%Df$35&=&fec6KSPTXO(Qh?knPzdw96HlzYPD4T=2jCP$G(?>0yJ5e1p%5$E_ag4?O<DN;gAeP2V4*<lrynwrxF>t9jJ2bPo(8`M(U|09ti8<#(K9az8C#R@Moi||3KvJwpuv3r9xg$A-G{jSJdNR<$-WJX>@Y@;wX(FwonsgHKF$Y@kx+;;%0!4tks(>c-A~`wx)JE9;|J-(Zu${K?llaFfB&2P7paZcD6H)l9ahzpb}s>Pxb{k5jJLwB^uCiWGAVG*3lMj7E}GUu%}aL$GZyFN{RvrLB@-STn&}3Ben;k+iIj^h~=cmmvJ1$R&yU*4+9nV1l%KUrKvcS+qyrWay{e*{d^xIIwk_nWI)%AD?8TR2PGG3k0lKDOD?dQ;5Eoa5q2mzbetqPhTh`X7@^SfuO;M!X>5lYrGh%xaz!$6lVH|}VX?MQNIQ{`*sMlb3R4e9zEydJW2IwWO&H`Psyd+*0eJNx6@dX@@BJ_Yt=!pnBCGBBJ*6ro)Xg9LAP`5Oqh{Lx#KAGZFJL%_u@42uu^eX382sko2nrG)0j{y{)l3iziA&&za#c@3oB{zJQXrVRbrMuR$uCZRv$3BV=>kvJrHQtfCB5230y@SjrPD<stx_u<FFQ-49HnMQ#$9ObBaas<xf}?D3Y$dZ3EdZCKX5IgHlqkmf+Y%7OL>Ovr{X*E`342?31kM+J;JfhDLcxNs$QVXcOaUml4iA7+dh^zskLWh<q$5f{KOwZp6yDQVH+x>f~8vvGtPm1DFt~H#CYty9lDq)$fdH7iQgtn7Yc7{au+84D3KKv0#is;sw%vxMnxU-mlr|z-9_Re{^El2IMP0d5Xyz_3QIA_NK$!=$7&LETdWGC_|t*eWrCB7F7J??ygEL3ttj$2GN({!PRH}(B&B|Xm|KRN3t0-L^ja=3CMDGK`M!&x1Zi+irr}V@)u4$10>>kT8T@?|U&{D(gwr^xq>_XzW;W1Z0?Ws8z0VbalLFK)t=J6RRvBP9Di(iB3T!qBDJ)JJK3vLqo9Co65qaY6@FN7n4)BQ~d9r}*`81BEiPdW9p)Ka2^6+19rso890@;F*%VRo8YpxxbJ)k{tv%SVJ)quae{weaj6_#kQ0u@vSu|g>%bJc8CDpl3`KvxFG;aW*HocQHxW`=s9^;92vavlGc$#{N5vjt4(7vEOcUbH$8+h5`Y^V(0q9wp5Bl<C5pQ1)C*u^=`0MWvieTPSxI8sM~3dq|>2$D%>h46z{3i!P32YNQ-FtG4LDKQvu3UrMo_LlP#^NQ{DLXa^0IlT}R_<gKE%G;P(-&_*h#EzZqDSW2a0I8UXK&biWwTzL*N^%KjrQ@afo>7l-qza#Dgpw3D`sHoN3xV@VALs%Ms(AGN>+uPzvYYDghqc7rCLH7yW+bFjp!=k5<LD+GeHv`Yiejt39?z-2!1yoZNx_h6x3dtf4gyoB!9hCD{-q86!gLhHhFUmm~M&n=(uOaOZa0Y_)EQE84G6)UCryH`DqsX}gJANPn2%L%t2xy{9r79h$H4_%h@-Tahi<oW|do+%+z<rm6Q0Un~BB_~uiX}{Ct}-y-QyIr?o_d*>@<|FmiCTv3zd1ZPK7aYk%Qwg82X9Y~Uc%pBUJAitETh?d`~D5l^p?|{911}+{pB=AFW$X<jdA{A9nH@#PjySk=g~46)e_RIZ<SY0%>`|2vVtHzRr^>tNNP26U{A$QN8<7l%nC!xbdI^=DDzdrACu$gaQ?HL<0yS<to$>B@m$#yE6tU5W92Z&5(FL~0Qug;MyQR@fgf_&DiK1m9k~d05Ilk@XpGPrLc9LRPrC?qbFhbCF9-Vw_H*zdf)8`>5rU6$a2>(*9Na)~BL^QN_&5ijAowH)pCb5_WRw#hlkpHSTZno2TV@9c4G?-B+i2CVA?Zfk2f_g=(S7^ZzYMZ>n200xOw;|QHF<#;(C{+^Yy~_=V5oo>2sjGZMZi_SO9Z3>_7LzC@FM~v1-wFFtblz4d<Fc3z()oAjKHM=ULz1F-~fS%0^T4HD&Q>wkpd18m@43(2>hmicL>A^I6@#%z%c@;0!|Rf6z~fIR|<HKz)S(ZB5<vMQv~J;_;&<;N8n91fu-Ctm#hxveZ)Nr^6|2R6xT}%n-345Y0Z?6CT@h&SmXc=YBYoOBN9*<gGgeO?%7vS7Q=ckF=#wMGR`23;+Y-0CY8bzG_RtGGzi8B7Nzf`7fZG}MGz}YL30GF2gf8y7-3LA`fHXWh<<HP6PN&~rw%*^5qDGN2!vLTj7{Sx&}w$lIM8}lMY*oAELS!U*RFK@2|Jmq9?>%);bLEL`Kh4yuONM)5`Mzrj6we0Xhx3f;xGDcNcx5Bks(+&GmH<2NuX=QNb(cl_9zH>6ovLE0(lgI_9*;#6o2+8_IMP0_9*ap6nXY2>Ub1(_9*0d6m#|{-gp#n_9)nRPr0owH`ZlHHO+1JxbYsh-s9$b+<uP*=&=Mn7NN&7^jL@<OVML7dMrne1?jOQJr<?Mvh-P&KFiW)S^7#3N)aqepJnN@EPa-x&$5vIJ<|Rw8IWa-@fTrb&DxCj!$et<&rj&gc4!|&Jh+$ejJVe}O!;G2`IATiK{i5WS-(`)FUk&VKPbv32;oE}VJ*igy-|or-gnAPN}IGcXSTqDEJ*!nFsA^ipEcWK4E}F$y@U}He$!aRh&NGa_zx^x4Hsg@GzMWd=@zJ-LiGw%U!nR1>Y+kCEKrXW>QRAOSE%&@wV_ZO1?sUvJuXmB6zWNVda6)Q6-q}7K<Qv{Xpf}!$tgT*zeN1WRAaL#<FZ6D3LmsG4`G2_6ai2!CS4!K^2(1m@tDGMuJ6Mba%RTjgT@>=v6NwsPZ-oAo=2)YN&$To1iF6-LAr~D4HbrSgog&&aOea<TMw1w4Ny_hN+2caB%uey=xHdQg4k?T#MYsjhvO(4V!xTOeumOxdq{JwJ<NN^s%N!7%UnPD1yAzxk-8#CkJW8uM&*NyV>J^@GLF{<0XNFU-1IX1+as+zSifq`9mv$KLBBo9wF%f<mq=Y(upg$uDUbb}#++9&&$H3mp0r^lDK|wvd?K5QFoJaiR3FJ$y2AHFF@(ZT1ejZrNViR!idViP1^9Df^Xe$bcng2ekEMvgJRrExdVd(DV_fkkw5H12J7i|X;wLYq_7;k9@kF^j?QUSM{^4xPSb<^kF4UQ7w=MK;c30dO>#8wG9>|#5f+RLQ07sGs(Uqhn8lGBp!f1y38pwE}OR33MTo)zEH&STnr50n<@hA-Dumnk9q6Q7g*)Y0>b>_?m72s)1ng+N=fmMh<O~ke6)8P#@hSbw2PCL{GI<V`CKmI6}aj%NPLSnJH(4p-CZI^bsVol_`XjT(iZnne^KZssS^EdRW4-9uiwZ``#ZA<VD1fS61)q`JgNsT=Ce7NHTKCpRDeOMEHl*_8=9u^f$PHUOG;?CA`85d=I0M?+P=wOKbRqPCuY~Ot+O#HUSKM*xCefOQg<Ww^`Ei*aQOirszSfqyDTZ^R~pmGlEbljm663a4WS!GdBuZ0ok+F2AYSC<`}E@gYF*q&C{=IW}ExV7M0ooSYgtq+a*ZG?7%y2d}JjWZPNR#&=d-06Zm=`43bF8^QVhIyy{cErCO@lSR{-eW7fV!Odd%N<xkrgp_Mmv~scZ{~i7nb5tw4%0Lq96fX~{q9Q7h9v_w#dyr)4#Al15gqASyhrz9ES*bzk)b>U+R19PNJaWm%io^x|I-t2FS+Um+&Uk{waE|eu=A7Y-&XzwR{nq1s7?;=VN&`S5l@T7M24A2vMH^Rs3C_-4xr$L9Xj$J>C0yul{ybpOC*+vPBQ9qE!<m&h+dm#U2@r9AY{#wZvh3C&-L-}k4(Oq(=-)}%DQk7w~Pr9@s*lG78{IbDpQ|s@bYymybnH^;5|1ZbR(6WVzjjubG$;Ce?@+0WOyi$(JwzVj;!cZ^lq!xhi>ggQT0Q?^X#6v!adDRnbUoO>Sr5MMHv-Kxx)moMPyJlzt7MjMg~KNM%!m<qRAUDZS!keVEJ_ucaD^hFhb%_GMtJ)e~#I_lOw#K0&z**Q&B;a=b^<*EHq8ym9i$?6RY2wQK4)pli66xz+|wvuDpu{Rl_YolT9^n-it{#HCdFM<t_$lZox@MF}ZcNF>eqpG6PpNI#e?c+*v*E%7WLo%9r5Kq06@zst5b!@=dBST|Cz}w8J+IzusJG`Fys;ESKi<jAl8X``b+|kFPMDT7I8pJ<B-UYC!cnzjSLy%@|ab?g*+o9lJ00o}IitK0kc-_V9?Vbu_1TlFXvmZElKYv)yRoO$R@n1Ne9yKo6$gP0{bdqfd|PXt+7w{ALd{%82Sa9e(KN_Y_rZ)ipnfPN!2A=?3q;&(&>4eB~fGRK!0Wy?p~YZk$Mq@UZ5HA>+l{mHVljSj};oPGfg9Z=l&~)m*1Mue(Z_*U2wZUO|gR^H}wa=h&-O(FH=|3n#RW5{{iZWAW*x<k@)=qu*#zLBC*v1!r5el~`rf8r4@%3jXT0sGiR0Ee)FM^FikR@zuuig+*g-Eb3`vz-oG2xH65zewbQU>RzMnzeUY2ykem^iAe@SF{DX1w!@JWAMmncw|myilLT&JTl6q%VPFSIT3^bwu4f*llwaE`;8Nc<gzdT>weHPgx#a$zBDw$dlKZzw-l!P3wl~O&Oz%yWdT*OOKA{(yd~a?#sZN&o#&&rcHE(g(&Z8v)V<Od4DC?Cj$i<g@tI<yA2oO?Mb(`+)D~r`DkR2z85@{_AjFwN5PNll>TfW~|=~7j+dx@3CsC$1h4bE#X$rtgumZV0LRSM&-p>fNQw}!4^Qqu)0w5#i%W=5=cfJ!$+zbS9?=<OnO3Q~X%OVkwK{)w|NL>=TkagZ(-Uqw;fFmSClrRL7VYzVpcm@J)INc{<FcrvZKz8a{iC|7`QrMN&HCeVI@6OAjAOVGeQ&tuKqncL`4gR%TA1F`mE3JjG?>{WvE^I_(BwAjRMo#5r>XK`%LtsdV0RsVZx$S(b<sa~>NmqzCsL#M@<<Q{V*Ll+_uUHQd(w+-_yb@#dp;iXQhw&<#svA){jRXwInFRhbvwJFHEUlaS<;s-yp)Gc><oq{&|+G1mU{o#6xuhN&e=ncd>*zk7WA|E~J)YUKcEb^(7CL&e6bV0n_t7WQLUTv)8tFQO4RPTwU<XJ|(&Li;5Nr5f^vINFrF^VD9s^ie%pL@kOzRFiC2t{_ZUR-nY?#W>B{oyaPP#s?ucbl}&l+)=7?Xrv;E8;7Kpx@tV2d*-488uoZ-=+TD?wSQ(E$P;M9PMGdwvDo*;hPYgAfr{|3nq9Co*uRr8{QHR9zYT5bdm7~tfXEvFlypQR9=8w&r%Dkezt{`+9)inY(FI;GZSBKfW~Wkxt+C(y~@hQV=FhxYzjJ)N6@!At9&Q%Wm3V5%-q=2nv)~fHv_Q{sPyTEB2>YtLdmg6FQ7_1Ygm?6yLq%*<VOQ?LY*p>7j!uZgA6gjS72XNI4r#3S$f5>Wz<nCMacB3Jj@dnbqtj&bdrqD)t;#Z&w4onj~%pGyugs+f*j^o@|5Rn7|Mr<WWkwqW#xhFJLl*-_>$Mc%T3BpC^o6D1{f8<Pp-TDA_*_FsOESz?bDK2LQW);#^TXd-P=*UmYF&hV^>4^P<6$l__?$vDzNX5!YGzPPNpz(Q57~(Br-pSuz}`ivgFQoa~oe(Zbu3xLQMYq*MeT+h8*AYE;GcpMSs!3Jmp|IP*VoBmti_5-PoRyjEmy*lsG>^;9?nuK{O<F;DZ|e=kREKAcN>6f$vCbqb1JTkXIqEO;}1zz%ti9PUWa6Zj?H+7~gh1dG}hq6U$o#09(kJmANbJfWC(ybu7X%V>pxm`^X<NTS9iCFo<k-A#`9{vAl{djkZ9Fmg$}9<0BMcKqaVGA2ie`B9c|w<5I_O9n>L9!7{WL<Hs80eyc2Yfoa!75R<S#HCk%l+L8VhOn=;e6two<klD~y{q3Pr6N))qo7Bpt@~zv<@orn}Ew{xRJzh!dfHW`^d_;ze8#^~lV{N`@pM4#S9N>I@UGKmf%HoJ-UQhFcwfnht-s}Et=t~T}{_G!;7apqRhQH*vU)P-Hj_^Nit9NHW_mBoR7ChSzK=ofFm)?i2+7B9Ob-sr=GR!CZ{0OwX-%z5-Hya?{=)H#J$v@pjpmc@J3RdVz#hw;kFs;5x$q|5FW%M1Tt!Wf|*`U$32=G^DH<*IT`16y}JnQ0*%?(78zho#ZaEcK8OS-LQd1ILtN_VNcOjNr^RymKVJ$`=Fg=yM&-97)xPQDqPzvzyU)TC7M=Mo#FynL$cJ}pF=`uB_%_m}b=vexpgvD-Rl4t}LBWM2GD4({Bol>PPYt>d}tCr)KZ`HBsqL_!Ov>L9AZGy>gS)O^MXY$=VLDBw+H+<r|qo8kx2oFz#U#vFcOuGm~#Tk;7gakHk{IG-gKt=f0KA+WrcLx!!tT!<gEVd2-UctL#fZXaDg3h^Ttg$EVKAuarLL(OjO&=2i+e#}XA)5awPFEE=m#8C*@bOLh^&>Cx(T1t##KxT%sS~{|OAS^kf-d9(X<SpMAQC}r#*3Fe_XG6_#w3s8j)$F@X9Sr?c@HgQa<^f(2x-b$XwclH53H;e!<8Q&?hdk5=^w#*}tN#rXk0x$}RR91'.encode())).decode('utf-8')


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
