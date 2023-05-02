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

VERSION = '3.2'

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
INDEX_CONTENT = gzip.decompress(base64.b85decode('ABzY8-ziaK0{`ti>w4QpuK)EEQ)f>_HL@(pv7K0!`=!2Yyp0oYWViLn`m-r=XfdWph8N3f>Id0<ynT`b3}(pTMY7X0e@^>-iAi8E7z_pjU@p|oB$;}<jhzWMZFnSZ;_=;Ar>7_H_IK6@YV1sTVlr!DMlnx@D`%&NtIt;e9Vc@vwoL!ljM=Bg%(aurHtTh}|1)SzxO+WG6m;MC4hWpUPgWgs>U#5SHVynB4o!;>2tSh*#(`&J1r)3ekUd|{Iy7zD^{=;CchH!c(Y5P~*Zam3+cmx58fu3@>?Upi1?D&oyflF}mvH=b)%9(Dx6NL3fl(5Kf}zKqM7-E;9C{}7!14GU2oHZ(Z8zc;HDN&mQ-9F-kj9DY%vUYw9QUEB1%G)oXqcXR?XS8CpT;>d7k8IEi6^EV%)UhMUY+1$)4I8ig4DM`C@+Y%*^_R!yH#pj$_uUM?D}0z&%KCGYneHYvw#=83R_Y(5WBx}VAR<VtZ39$$3c<=Q;<}m7jbfeXj(Rjdl;45j_Zdh7?{gF8weg4rzn_7<Sa5lQ+|NYnsF0Gx;o~`jB`KJzh!!sfsM0Op^+A-B#3Mdiby7|&tSM*n>{g2M>o-jdR*?sF7wPW_s|q_9{t7CfcgDBzeZr0EXaMA2^_RiV*#A=C2AN4cR2E*7eoXaxEp*%*JR_{li-#|C<1a6lLZ>z+HzPoyvzT0$Wj9sKR1~p`>l<Kd7Gf6TP~Pa`vGk{$l-l55BYFqoWkJxD;FBWs=~ImxG;1uHphL)Rf*nYGoEIlv><{ay$Dt0c&M3)YujkK`qUF7RvN`9Mi{u{h0++tB<3Di3Yvv4gxgt@j(%rNMhSGv@Q{w*wTCOx;xH<#5gOi5Csc^Dc!dcz8SS><zoOB}8K<L!*8s$x&<`+&0<9QbfoPM^rBbqt6&CoG=UO+z6;K2xPWwV6qXq3Rs7xcGRgNdYtWx>E|M|D77+aAWCX5C@;djZ}2NU!luq(`r=f1VuFvOENsW0XtGg{~p6?Dy$y&$-8x#62rjzu6voUmKdOF0{|s{#AlCp)mxDX6Jq3DWzkfust}D2aheP<KGbZeoZU$n1W21s=+Em?6kI9`~=42^$WFtlw%xJV_&;ICR1wG+MHLbI^p>iR&cLT}9@no`;5!$sK2MTi8~cSwe=q12%}|^BLPW6ON4R4pcdHr`)hR*Yd5!o-$*7bEDg3-?9EP__Ntnf56XnTP+M_3FCJmxH9bMihLceJY{w#3C>RUMo9#oL^M%U6Kd~opO}!3-4F4Vw0b{)cg6F55SqzkxHhnZsp<N|4wTIx2nMr`7g#2CwlfLhgt%N4l>qi}8klin;NZ8JokpTUo0(zA&1hf3N-KfK!G}13AeUpK>ad+5>$V!n1YEUH+(8rtk<mPYnT39cL<j57T4_oS#kL;Lr&tfU-I@^tPk=fE<>l&(I1J(e6&OvheI%BI9Wli_Rve!~r%>mbLv!3a4w+pNnZ80v*9<mV5`f*unERn~t1V>Qj5(wyt3jGT03rXi%5Oo2Sb44*XGW<|IV|LEbah=6T~Y5{--jskVBZB<ZO<PmRUs{I{@wLFHUb?r+Zw=*PXXS8Q5#1t6r4tUoLUo@562@Yh=BySMy^xi<}2xk=lXnA1rkbR01t@_1YniQ)Q9S72%AmZL<{;9bPM4!k9AIMs*m=+Dw5(n9hM0ajhC4wL57ksj&N5>`^aNYriep^P;QfGJifg*aXs4*)Vg=TP*$LjwSY!=<0%AmZjW$qv!aePowC_WdmXYS>Y!OJR>qFyP$)eh7_d<c2aA{r<6OqIyNqkG$G{2lgL04W%f3pv-o-`WwCyT!?iFzrcN(ZEgAfXXvkTTgkOm}t$plj*ILojqkm92QPJW6$UX^#mu)aDyel01oP?b^0+@j;SQJjc=gO~$N&Pr;a>kLdm3iZSM$i|tEG#DDi7|4uf*u?pagOETV{W*wkc=S5JXo@N+MgD3!0S(5mW-HeFToEWXqkhFQNz-ka0hXg;@mWw{vni0wpxFKXhR@p+DK1#xu(#t6;JZ4&Cvtgg0Ncwj3c}cEwban&VWT+wSCn2^o*6^X))HvU;lx5`7o0t`$L@F52o^QqFD*Lq<Z6W^8m>SE8OT>e-bP#%o0U>knUt%_Fk7froQ|h%v6`M1UMW4*dx}iQA5$L9N5Z*7n7w*ZVtdu<fGa-0c<|bd!5%rpVv$~g3olMr%yb}N|0FZNg)J1j3k^{Ar)EejN5>F@s2E~FmaSZkc%mf&8LKwy!9O%zVZQebnFy5)7o;nr28y;ktI#4!C~Xef+zM=d2~qR8VVjBB26J;z8FDfs?meI`3LYk})!STNjs3<91%Th`U9g=U_Pn)#TmRYb;#NZU1-iFcY=!%IPa&F{QIs_U@5Fw{T=p&N-qEH?O;zabW9mvIi8y42%XW8B&ZWGe^M3^Iyu6>6gEF+n(O<x<g3?dmkKs+kri70I>xQEXA&-Uyv&=J|V*1}LV@H!9_3U@44~35D#ay#3;gXMVvXE!ZmL^AUPR>p*4}Lm$b9#CF_H1+je}6h)j2c)(v;X$zH-e_OnC9$+F+tN`Of%Yh_x3e9-Ss+}9}mt|OUUQhA{yBe(rgThi;HHZ<J+VQNbTfKjt6)Q1jPG8d>Wk0f8=vi@u$YhKT{Ztm2I}tTxmB}PP{Zm;3)!-9ZgMy+6W!HK9#KsLdZn}8^I2OBXIc>gw_z+cdy-~i(oecdkFS2u#aFr1J@B;&%kF0KFh!j1UE8p6T!_4+(K|G1D_-KJOf`K_(I6Yr!MBIK4J!lIrx~G9zsKeeuzvo$=8sRBJRDw0V-B~``5oTvUixLBKAVjeN>vfLJVm5B?6`den4O>fjtB)3G5?aOW*(jE`dV?90~j#folo8LSQ0+BLrLt`~!gx68I5;8wtEdz>~l+0#gaRLBN;5TLc0LoFEWN;GYP5l)yU#A_<HTh$V1}Kq7%N1X2n7gutx?enwy>fnN}~lfXFwa|!$}0>2{gCY{1c=cO&I4&p50UV7PhnO=g+35m^y2hd_kk&Pzq_KLB{05Pb+43<|&Aj$|t60P*mybaO_mP4^d;{cLy8etI4%*fWMBqpbM6->ECFhMXceaD@MlhsLrNMdrD5oGI@P7)JBqkwc%FGmpPwHd~*1L~;-?_NONgs^?fBcU}*z7jTx0#B*gN+M6`Sr%ow#<EP=G+g`Ka;G%OZ25{a6A~PnD>l95^zJPr5Tb<Ma5y87e?ORsiEHB%=dLgG3)vb$u&g8)Ul0>R*90R@Z-6@#PRJ2n=umhdN4TIv;eH(9e-4H3afI_Z6pqIcp65__9Y?sGL*a59;d2g!zj1`aITX&uc|mP;sj)6WvT16&M~(NW^&U0fqxO3wK#wHokqAAKp+`dWNQxeb(IYu}BuI}W>5(WslBG|w^huUJ$<mj4kcuE#`Xo!AWa*PEeUe4!-x1n>#XVtJ6MP~pty!7zksnJ-a_J2xv+0}10S)dAyd&<l2?2caG7N=Co`6h*^s;`Tte=-1o359aO%cLC#bND4DYRCINZOXkOiG!wGH0^D<J3#s(3=YfsooXaQ*{1saD9Rv6P`&VeZ-r<*X9o_6tyWtjKvuE>9m`pdJ@&kQGJQ(=csjwTF+6>B<fj?+K{M?9JMJ?n>lJrqPB9>bBTJMqh3hV3yD(R0#M3XoS4^K&B+kn)x1RfS*WmSNVqiSgu)l4%oAA9=0yM$i-oRFB7W-zlz4>jp6UA}f;5(}_@Xc)E8^VG@G*gE#4}Hoc`2X|JWus67Le*<ZbP}@jBu}98IIUESJop+vIay^-ipV$&`B&_5Tn9S7J|rZrN>sTnucSPj<MgwSiM8(sW}$0)*NR&B-NAJU#7Mj{DjBV*+|_ANR8F9GOhA)O1_#1rYZSrjer`ZVrqI3{_RNV4pxv#bH_X}YtU~;nKl8N=@O}H3y%E6JEyUqi!r~ajgoB0z?R~e+xhAtD;4}I#kl$qDYRJ8W4`?ULSMpQ0?p*%UAN8DhHIC}g%0G6l$nd=44#d2{9tCty>~1<2FvEh!7Z;)VEw5o745$=ecKcJ)%i|>*m}mb4jv7NI=3#Lg+Y{bgk0kMr^|l(tyo5q77KM@cZOx@8ZAcaV>+Y3qvl_5!G|^JJg{SVE(r6pXm*YDTWpnzOA=vG&g8t7$t#ktmdhkBqZ5&aP(la2%`a?sC}sQhTc+c86#glzk?GrSH74hZ$$62<xngo&W<nx0)V5B<O%IiGY$lVA*h@DIU6xf6rNzEuxh-Ydsfw4W%k<6{vOSk<&r57Gb=64Rs+f2uOyi01zEQtuZZ|~N=yBe-K*5$8@x7XY85z0T#nw*cU(??IvgALxCDWEn(U#i{I>qb2awIWJroF&sg>h$wi$U~Zvz5J!#B-lJCghjMi|`oqPoe;nAkJHi{K&VcT4m%l+7U(Sy#Fr?|8rV6ZYh@Sn_8#ik+!h!qWzwQe_8J`>-~RgEN3T=Fcsn_&RFk~WA3MWrf+fYE0~Ntg^?M!mOkJ{Dfrmh)ZvE8zJfS4YljL7OquHA;fk=~W=7MLEJ_QEDsB-ILDVdG&xnWIOwi#Oja(C8CPg)Y2gKw0P8Vz>JH<7cZWuRP?ECLouch)N-A?4av0YZK@g3_oT3Ygi0%;PvzIL)8&XXQCKz01lZsb+p=R7YS8HOt+BTeF~xwOTj{lGA)%(ZMnpP?_Du9~*AwlAbt&l=F}>q}c8`Bf7SjtS#17VHl)k&-}vkb%fBP(7~~;Sg`nG{u#e`c-)A0VcBv=bp}>y0B?IMh@JK(b<&U+@qMJq0XW>EgoVZef|`DzGSjA3h0+5s?1<gY8|Ta+7DLGx>A|nt>S$joVwNHVEx#%SiUW4gw<11O*?d{>dVcgmd|Hf%wlOi&uA9&dA!}!^7sPNspa=s*0YGiQUj{r@Vl2M=lchTFV9||UY@*rdomJ-=grWJ<5>{d&283fwi``6Pj-_zfLj{?IuJRxS-%Ufu6W&0-bdW1$}T@-R_pq1cH2riNJaUHbvm7*2={o|b}sK^;r(UrL=yjF^!5#8k5SAGfgdG71i2*M5j#rw)M!qUB#i9Utbt~$RdXrqvhG4yRwug-b_*@$&12OI3Y534ybA)2x7WqWgIQ)`P1vXV!lcb|4s|_VdhVPF7F-N!E0M~|HOlwcbN=$qn~KTe%C`vE*`QMY=z>=9+LadI@_K6Uv6>nesw_t0$WM%0c{@w>-w@4Lu5U|DY?{JQj78*(O#hm*_jqNi+r4OJk)E2^VLePe2-roE*6-z7*E2_CfM41x;8Nc<c;vbswI0o4vE=R_BDwqJlDo?!Z<GvN*&8G``s|Gt&fYf7_*i@$;W~4Dl2+rQegm~OFct{3$f#l>Y4gF$S>?-Ku;vb8ZdCNG1mHqdTwX^kUhOK$ytwToLcO`u(7UX?rknfPT9O)llqs|eR!Sk{faSe4J^55SAcd=K-xRkwGKs7Sw{9{qq$g8sp-!2RvYgVrIZqZo{-8>2A1tH8E=K%g%H!lP67#EcqqR(Mc{xrUM=ZQ>9-QH2$d^%M&W#@47nJ{dQc2%vozGQ6hNj=9?A~=@-qp3!W?i|VqPlY_H=Jl^Woq6T^y2!4yuT{0MT?E3Hrw3TSl?*TmDmCowK??w8`?^$@=<QLu71AA&Vq+<#j<+gE@-h=i*&HK+C<7%UvIro@3|r6Swz0ks+ry~_idw&zC({kO?<i#RNHK}AA}1<k-S%o@F7!D^Rc?+p-i}#|CMMLrHEJ&zs>=j{VFziO72*z(JD+O(Tn9XCFiRo9h7Ipdb^e%%?Odzp&^2;ic7EJWv9w_z@9#ZB2-zG*L$oauK{Z{@kaypK$Mq>fmI(ciUP;nD3+g<7Y(AoLQf?ZTa@<6OlCOkd~d9@(bdYVEDdcCV+8%IH8XAbLw@F)A*;9*wa+aHr7I~>lIKuit-!N}Wntl!`KBsAsaMzMuS!If51$I(4z;v8a<Kx*N12DdeJ;-uG8IkZ>&po(stkBn3WL`U-kb;fg8&N4KUiA`Gyj1MK~6wHQke;=vYbAlxPyC%AAR806O|7Oy4lCD8XXw_UALd7gVm#nx{nQd1*^_AUPHPrrE`SWS}5Fx`Wh4<{X^YNv=+TCN!%4*4&_FyyT&%GE-QBINIO=pkFwJ$<DOFa*bBx|<v+ly@m>r<C-z*68=Eb5(T2PLYr-<uoq{#2eF*t=likbyrV)O+b@uMH{J@J=i~t+RIiJG_X&RDE9wA4Z=>pjad@nHV%9KOy5x)&?H15zYRLke7QP`E(quqv%9hoy~gIZ`;r`a0hBC8}+j%n9SG@UR<HClt(%LqEhLH(740<TO_0y$Vb=)J{*ey{WxM;5RL6&037RV(N#E3kmi<z2mHZ_1GtH1m3z=aOcrHTAlG8gr^Yl7t{iie7A93r-<b&3U8s)ePmqaQeGi%F{ZugmSnDLhfhrB0HHWuEGj5*%Ay=Ewut$ocI;Tv;4Wt@9WWLrxaO)rJu<evL^io6Enc9e6T-mx0=P3x>zhfq?aO5?P9&O(_MRn^{flQK>KDe`vHM$FD(075Iu=bDdo>3)<`MOEiIrcGqL{5uj;}%TjaMEukM$1P9Nq{T?onaVKa4ZDP@17yL3GF-PkG(X=%u)lqCUZ>Z;}kD8O=(zzRIt!NR?sc)Q8IXU$n0H(`X~3BGcBZEeAiSFrmv*~aB8zG~Hea|eF%XFev_`07G@ugnO4mxI<WXYY<M2Mv7uZaAMW`QB>_zjm!zPmEpPjOM45TQyBg4rwvatRW8kDUW0D)j+FdrL~mkVt`EdG_`cX%7L&j_0(33{K=dppJmGrxtn#dMJ_mHOb}_hz#GlJO}|8u9}m=igx+HK*jb}rrNOV8<xlyniJyV{KYRDjDh*Bm00'.encode())).decode('utf-8')


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
