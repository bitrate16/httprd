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

VERSION = '3.3'

import json
import aiohttp
import aiohttp.web
import argparse
import mss
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
DOWNSAMPLE = PIL.Image.NEAREST
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
							with mss.mss() as sct:
								# Can not screenshot
								if len(sct.monitors) == 0:
									buffer.write(encode_int8(0x00))
									
									buflen = buffer.tell()
									buffer.seek(0)
									mbytes = buffer.read(buflen)

									await ws.send_bytes(mbytes)
								
								# Can screenshot
								if args.fullscreen:
									l, t = sct.monitors[0][:2]
									r, b = sct.monitors[0][2:]
									
									for m in sct.monitors:
										l = m[0] if l is None or l > m[0] else l
										t = m[1] if t is None or t > m[1] else t
										r = m[2] if r is None or r > m[2] else r
										b = m[3] if b is None or b > m[3] else b
									
									dims = [ l, t, r, b ]
								else:
									dims = sct.monitors[args.display % len(sct.monitors)]
								
								sct_img = sct.grab(dims)
								image = PIL.Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

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
INDEX_CONTENT = gzip.decompress(base64.b85decode('ABzY8>l;;M0{`tiYjfL1lHd6&CiGqi#uP<TmSl^fwj4dy+E}*Ml#=MQDhUJzA}kP~@esv0{2_Ng?|#X3PtO2@2Pr?|x~fa6YysWV)6>(_)7|p`cgOL>+pF!4xoN{Ac4Lq4y*fKPeRr_CPEc)k!ef(JV>66+JgA?a9j!gD13HT5SZta8y&18OwV7+j;~m!RbpE4X8*}$|94ly1`vC}?z>n7)bK-jQ9X1L4Aex#M?-PC|D~tlq#tJA{3m|*Gn)TGQZP&luVV!<$VurV_FJ2e5C$?*P!7bFD29X=P0Th^{DDaXP+FZf$w>8(d`NIx-(E&zrFcl0v?!@B7cB83hLJu5|KY;M?cg=P~Zc!5!G%)e|wf9LByUu*gg3fUts#@@uhy9xAxwrnB8}mt&Av1CJ*pp~%+QIB=1n*V}J~FMl+b~Fc8-((LaECqVbUNFG#-+T_dd9BT(e&I6`J|GW<2Xxr!K<((Wdo7>8wW=1O~HyrZEY09aWDZ%C3+brCkQ7+lUTs0+_znSnt*}1+_Qn;k#P!xnMBS)6Ex)q_^cQ=VWev#9?v-UQ~g_}XBpTyTN4^-f=YtW=Aejp?D`Cb%eC1P({ywbeXPgDUhFZ?9B~g#A>+|oP7RnZ@A)kPt7Jj$draV<l?n^soUc&BD0sk;7rh`N(7;3gGrA@l-x&w@JVX(YqnIqv`1ZELI@5>jf14~dfbnzVIkMm0e4MulTDs?gdAT3Z#-kiA<N1^i>Z1e(*RNk`49g1J-sZy4!Pp%4F;^velg@aWh0=lu3iTpXk>jCe#;$Fn<?2&UkXT6=p%~M^B`=gjFeVZAz*5jGd?DQKx^(or>oQ89Q-+6h{H{HyON+y(uuf=rL!D3|&f+=~Y*N}C!N0E2$r-1kgx3JXzR(XahXSn_U4dwm(1lX6j5-T^%X6)}K^+vqiPM1)$!J3RODfZdXqBUJFe_F5pa1?ZRWY(ccN#Mq{Fpz)>+em_gTU6A8O?obuV#oRa}r<7MP@Y7B`WBa$NNEW=W@e0Cmf4FiYR9Hrk8LwU^jjCw~uyUB@<9n+Y+RUo4%x)nqeFPm7s2ej@{T0HIUh2a04F7b(kT@+8+0B<1rfy2CUbtg*;9|pE$Iq!PIEV`i*`AUdOHzLw6OKpLiY`Mk;rd%57m=EoKQBvJTiFmd|JGz>GOEvfEJQ%$;z<Zr{qcCVR?^jjhd2hyB2M&*0BiNBsdm+i5m2l*NocOu?05N9*!+P=Ctob{w3a?GNJ+Jc(!`uO`&q-##)SA6pFYm9)C(!@J_S=ugdfJXr7B!Nhd^K^w|u5Cnr++Y2lcJKG)yQA}KJ@=5^vI19`uHgND;%uYg4p~cMUl$+s!gq2nTkAn|!0zodvM%7`v1J-HQ;xV{tp}4~^3__!E3^NP;5Qz@fpSIGF9P({_I-h(!<aTd{3_Jnq5R{jxGvqLchg4uR!1j??61K$@Z(C7x2Ax8kYYxqEA2?)oacKGqC0#SvXh8tBh%om<=T=$BxEXOsPu7AYh5$nTYmMK743Y9&70!%Op<-Cb-00edD7vBEyS@)m=Fz?jvdW%+qExxGxbY9y^Vkq{)M#k{J2?Y*A4Y8yx=?Ty@=;=qVLqG;p&$Yh;2OG4g_|#>AD-*;H5EuGkpVm;G7x}ODpMV*%OPwwc4IB*Q_#(Y%PiJ8m8m}3|C&gOvvgP{NHkt%5(g<t#yG;=DD5MUeVHN-7($s%qVf3l{@C?wLs0A90Yh1iLe>Hr;f*H{(78Rt!A*<W(sYVuFYR@}8mNOtu~->9mP4WRfMCEzJ{&A#DvWa#*Ul=g`5pr&$PbD=UKD+ma=puoz-ilM;>;`JD();$QwAXv24@$ne;^Hr`HBgqNN|>6RUpMj8=U+EeY`4fi(!3rcJf+Mq@gOMkhw+Mal<GU{RT1nnw+K7K-U?Vf)wh<`LT^NA89Z&iZPHG&7gtv83!STK>AA%-tq8tfYB6HQi%NJbOIWTV9l1V_qifaY)1WxWs;`bA_FW(#o{xkz(zwLnL)Ap@{Z426e+G);IOx&_u#wQz$bKhWB}XiX&6i+quEqLn}v=1@ZV5+WqD=<L0e0pF^3Ziojq{&&>matt`jV3z+YN)WXV;XBpTGAf(+z!k+%_7#im}UDwA?m8D<OBijvX9%~#XY!W*ThYEO~s_(Q_O`A|4_2(vd&3T$thZE(eh7!O{%5!fS#SS->@aN+ssikS`s?4M=kx3q<PccB5w{?rVK<meb;5EVl#NVAojA&<30AZ68JUHFHlE6jIMlZjB#a6!5vs;_8^vkEPufYRom&8@)Z*AO+I8@3snEig9+l_4iH;w}MomGdxJt?t(9YV3DrC;<Fc_loWAvggeu+<MQxh+6^O6X@<%z7_6gJ%wm)hGE(ayc7EYbJ_Q-^FW&_6;+|TpHf#MNyGs&T(-A|a<1eJo&O_vXXU-D9F(Coj{X8>6_kDie++LTHpP4xSa%#<2zfLtn5CZa4AcKk5jz|QiD$n{d?<8GFXEbQ3733~lZ8BMx->a{b9#Puefaa?o3rbax97t{`1|uAW7NPhnuE8$yb(0r<uvD~j0u|Fa+=}(ySJ~=>26ff{B(GsT0%b0meI(TkY=-=UtBaw9p52cKx!v<ay-CeARylFqqE?2{u7_0ia*xs|4Lvm>N~97sJCkMQ!j}Sc!~gIM-vmF7D6YkPi1R@5OUGLMzD?G5M2Hkp>>1~+*>#9AlOO4E`r?@>>=1o!3_jAQt%mq&r)y`!OaxhLU1buw-MY<!RH7*Pr(-mz7R6<iHo_akC;AU4nHKOhtL3_A43yO@-^h7h`SUxKt-x=|Ngf|_73w@#9k@74@#3ahye}1M8K55j|hw;u#bQxfdd3=2^=E8C2)j*BY}S)a4UgV2#h6gjDRbFe<JW+0zV;eCxO=pcoH~4U?PDx2>23si$EZOQv{|G_!j~nB=8P_Py#~)A_<%!5KG`3fkXm7BXBQ)Ul5o{;8z45ByfSiTmt`zz;6h=NhYw;d1(u)gE))0mtHzvrWfOKLSob50kl|Bq@#(uy<#j<Kn!XygXI+xh%y3^L@PZq?}H?S<xr&2IDjOaMi_)MGqiOoiOFbQ1rx3jj1kOA-*G48WOb4tl$eZW2-&)&lSG8jC?MU`%MrwRZBC=t0rk{^cQ2rBLfF3Nq0pKoUkMwBfv41L#i6J4EQ?ZIV_B+f8m<Fwxf7aXwtU5z2?-9(6`S5NdiNd@2vI_BIGiEKe-O;X#I^B>bJrL8g=~!=SXL5@FNletYl0D{H^3bUC*%k(bR;~GBV5pta6gXlKS#p%IKufH3CH6I&vPWajw9U8k#ISV@Ht1q-#EhI90_ORyr8x^)L4fg*)+A?rN+C|dY78-Qu|#Jpi2^TNrW!R&?O<dBt@6R=#m^=5~NF#bV-yh$<iZPdL&DaWa&vgNJWq=J(8tIvh+xn9?2r~?+ER`;-0XqF+LHN)~w9<*pH+ox%7sU+4RkmfCl#t-VyiOgaAH%Ih_iTJOP;q>1DlKSuZO)F<mb!n;?XNio@E8QfRFZk+dz7nv^nWWzJ-QCy5ukQ*SODq<U9u&(Qh5!SxAxOnAnj^bv0YUz<O$P}HUnF&1OsCzDQw>Pl2ML-i!8m!UQ!Y9m8Elc;AIYEz;%Gt`zuZDpu!iQ3Lk&n4=4hI%1UFC<EN3qUDnacbUjH7BR=uI44;&!-BTObM4noKX0plz9pZ+N=nGe6i5=X~^&0fD(@>yr=p;4IzysEWRkr&<Z*CQ+!0A8u8RqrCti?1J6_aiv*;)nAuQfI78ekSB4`t&Xx6ulC%L)l(ph<E_4!!7sRMAl!hQOTj{ZttES-?CL`=OF;?$TdS;G9tTjhz4@vc;_Lqt620!C*bvjb_0#akOs!XeVl8~<^f=NRDS|gxFshFBxhJQO$x`P#@(%cD;%?k9}VX95Qrn*Gx%7SA*_AY4b=VHtkv{8}{8Q4-3aXVW*q@{visTfxuB83(!dd!zEuk<AhCeTbC-gR0`ZMb%rT<AdFNSV1<&fwWd+Ye@j+<V8;6R>Q465R6=1=gRaQqle^)3-gbU!CnFh^=Q_>)_FVsB`b~*)#~_wvbDl|8&^zzvs(H(tM#V?Czi_U9HJzeN1OGc+~tWF8Ht}od>op&jn$A5zVf%UX!g+aX}(1%9vbKGI>SvRdN|;WppA^6G~{KxA~Rr4Ww+}f6sLMuEIZM6*7JQy~gB1F}cVyxll|licCnPn%dS0x#^*DPRw}R7JKQ2q06#HqBPkLEVHFlJ7w`wb(!A9QnnY8?L~oYs;&x&n<W!(Pm^eDT-K@=&8?c~8a*y*S18y@BVMQ}n39pZU2g4E{w?kOFH8QDTQY6Q<ZZcCqf@*#EJtFqVA@M;Rv33`xEMqqH(S}uP&^N~V?ut3ya<m$|1=Ch3F5rP$c}vTs#Qi_p&e1A&inte@IR-8<CbF4zNvLO9%%{t&fD+#^e^jOWxfAzjph9G6HJBpi8I!R_=Nk(zUf=s`vxW>Phn&RuB8vSQ3^h`GIhA4vTq<x#oD2S0#l~?c(@{LxRKH{B#XiVql{a|L=ZJd4R~%h&^GWdvl>_ONEhwN6-OKJ*kwg4pj%;m_VlUSOlPM?aOqxMcCMiL>SGG<u@x2YXb5V6blKEJ-dcZLUlUuLHa%{hs?}BvI7^RJvJN@=-^6?UU#tB5^ve}qu);^oOJ&qW)BcQ+LN}tRNo)H`j$PV-Zuei?0?DtMSTGKT!TuoQ!v-HQJsC^NhyEg_;uV~RxK~rXly5%5WH#p9(;1XkN6k->1GjW^Hbr0gNlcQd&LThI9%CQ_#{>e0WU?}p=r=CP%wR~g4pq4RN2{k@DS3`&{?ZUSh4PuQe)gI#-w`#Y<)c?kJ9IPa>&>N-&u3fAa%n!#XqNN&bi1kK@fD_1$?vnQXBmf;22{OkxRA&12Zu*5&tIQipT2v0Ius}Hjj0($vmmq^JFL-Y)f#vx?Z$Hew>JTFrV+gMI`Ha>*G=Vp#I>yS21RPMuJ5K-wxqXHlpk5U-Oh`!z#F@Bd9e#GL3^i?_@BeKZy*H?BW?)%Fb+aUNb#cCam*)1V;slR&|XU$Xf&G@H_NW8Zk45V(mQ4M&|=m+R=uLAcHhjpAkcUPp1i$f#@3j9T;wLLy0Dhsv6r4ZV}b=&{mM$DvSN+mW%!K0yf~)<sJJ&SqH8*+)IYlAmA|{CMXRiy8hos##)T@2kvR5a<6d6zQvEkX^QAlBk`tRGFcc#Z21C=o<?IsgeRVoljWjG%6T7U7c>)1@NYeaLu2nrlJO27!0hj8w!6R4osQJk(mP_vbA(FdaFS)x)@>;>bmA%0tQ=h%j(%IXh86SyHEL>-<PttN+)UUBt2F4tL78zAcByB!=!>f2>3>NM|#Ep`^l>l6*ipx8V`TJo7nU}X6hp0DqGWD)2@A_uGwvwbmA4Ll7mX=ZoIbeDDO<&@wRYUH6+;{oCl2jsV%&ogr4C%=fTc}fHq%5a!`OcGtpMKh<_8?Z#VHZRGA>mPc6pHy(xE5O^xV|1Gjw6<II1kS8M&-*eH0MSaFCEJNU8$t+w9aR$Aw$!zWOg1pFz>3`X|aynl~LWfmb+H8Ju@*M40>^WLtcUvcdNy2REuqGZf<Ng>0WJ)i`wCOgbnRSmH8;QTU9>|cv<ieu2@#jT`0}>YMBm}R~t+Ds_Sj!>OD84Jj=*8n-$YL;=XNE(YNXGNfV!L6;(EyZYtrbPbBXpBYe!1Dpv7wnXgPZpZ^tTm!*hU5kK((o&6?ui3;vmtI^C&CDDu3GbQ7zB<&Yx#73)<AI*qtr^C19UI)DFmidZ}4&O$Vm3duaC3%-vtBJonun(fVj18>%h*9J?W=664z&&peITm^<x!j_(?`~4V5won*?y&VN>7-<#nOa#gwLy#_^s~~;wB--^nePUy<W|&Xxg?aXq(DiYLxr^*&kB~gg;(mE%KW5WU7x=y5S6~3%6)*;)auB^3M3yz9{L`<Jdj9LG>LAn$F!(2;9V&UUfX#69_$YSC@lYAZ6VD3M=}IC0R>5ECaB7C`b@<|-fR4F1V6zjeVx!rzo^yd!1y0Jy(}FppKnxsvCz#~b*AwO(hVt{BfQp9;WpJLr2y$)`);AN=ygfrj`;j2Gh*E}wqSKxvSUZuv2uNsot7DQmC8q6Fp?_&5nhc;F$nF*b1iOcHQ7}Q@&c?0%UpK?*0BD5%5NKNA^V$z`2E-UyVvq7F<LPKY#`@s4kM&#NH%$l9C4-#WIOP^z_d$K4!MW?KDg7kL%UEdo2N!@S7M)b8`^eg&ZrG)p;ew{>yV4Akx&_?RWZ?Y!VFbw_A75R=p6gi_Zo7%Qbh@5fBB$ymk;_v=`ji|U=J$FEse@n&{I}m0iWxKYRle|BQ0p=)ilo~%}Q(PcK$TxRDG@qL6j7|+`N_?WvZI<TJxJ3%A?`*i(1OkI<$mhxCo})Pvb>;=96EA<!I6+7^GTi1-3l#%a3RAo0~7|(PyU=X@sTU+8MA0{g4wgz^r_*KW;Z0`IWj@EI+1~JW=Ihy|B|=dEWG_1HnN1@G$)qf^;t|{p1imiB2iyPbJn!DbFn|pi48c`tz~!!Z}^!H<z#OS9MMw=0aTv$@FzJb#5hPZ?m&<JP+K+Dhz31$f%UX0ch%`;#Vxda^k=WJles+y`E^N!T!z~vnXo72*VS6<<9#0l3&1Ji*?z?^(?w+R(`k#e)1PSBG~xmLi}Bs5&pObtzFLF9b*m}`1tX0Hea&6*Cu{4Td|%PxxN|B&nUNQn3x>WVxUn$9QYF+Mc}J}R?A8&Dbd9MneJ&S>4cR7VQ%WFtr+>cJ1f5JmS2N6s$z>=aLSk<(sY408a<nS&LTS=sQi+>$?&nePCspfpGeE!16miqCHKF&7J&^?P5=M'.encode())).decode('utf-8')


# handler for /
async def get__root(request: aiohttp.web.Request):

	# Log request
	now = datetime.now()
	now = now.strftime("%d.%m.%Y-%H:%M:%S")
	print(f'[{ now }] { request.remote } { request.method } { request.path_qs }')

	# Page
	return aiohttp.web.Response(body=INDEX_CONTENT, content_type='text/html', status=200, charset='utf-8')


if __name__ == '__main__':
	# Validator
	def check_positive(value):
		ivalue = int(value)
		if ivalue < 0:
			raise argparse.ArgumentTypeError(f'{ ivalue } should be positive int')
		return ivalue
	
	# Args
	parser = argparse.ArgumentParser(description='Process some integers.')
	parser.add_argument('--port', type=int, default=7417, metavar='{1..65535}', choices=range(1, 65535), help='server port')
	parser.add_argument('--password', type=str, default=None, help='password for remote control session')
	parser.add_argument('--view_password', type=str, default=None, help='password for view only session (can only be set if --password is set)')
	parser.add_argument('--fullscreen', action='store_true', default=False, help='enable multi-display screen capture')
	parser.add_argument('--display', type=check_positive, default=0, help='display id for streaming')
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
