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

VERSION = '4.1'

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
import traceback

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


async def get__connect_input_ws(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """
    WebSocket endpoint for input & control data stream
    """

    # Check access
    access = (args.password == request.query.get('password', '').strip())

    # Log request
    now = datetime.now()
    now = now.strftime("%d.%m.%Y-%H:%M:%S")
    print(f'[{ now }] { request.remote } { request.method } [{ "INPUT" if access else "NO ACCESS" }] { request.path_qs }')

    # Open socket
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    # Close with error code on no access
    if not access:
        await ws.close(code=4001, message=b'Unauthorized')
        return ws

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

            # Reply to requests
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
                        traceback.print_exc()
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f'ws connection closed with exception { ws.exception() }')
        except:
            traceback.print_exc()

    await async_worker()

    # Release stuck keys
    release_keys()

    return ws


async def get__connect_view_ws(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    """
    WebSocket endpoint for frame stream
    """

    # Check access
    access = (args.password == request.query.get('password', '').strip()) or (args.view_password == request.query.get('password', '').strip())

    # Log request
    now = datetime.now()
    now = now.strftime("%d.%m.%Y-%H:%M:%S")
    print(f'[{ now }] { request.remote } { request.method } [{ "VIEW" if access else "NO ACCESS" }] { request.path_qs }')

    # Open socket
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    # Close with error code on no access
    if not access:
        await ws.close(code=4001, message=b'Unauthorized')
        return ws

    # Frame buffer
    buffer = BytesIO()

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

            # Reply to requests
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
                            global real_width, real_height
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
                            buffer.seek(0)

                            await ws.send_bytes(mbytes)

                    except:
                        traceback.print_exc()
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f'ws connection closed with exception { ws.exception() }')
        except:
            traceback.print_exc()

    await async_worker()

    return ws


# Encoded page hoes here
INDEX_CONTENT = gzip.decompress(base64.b85decode('ABzY8%)fkO0{`ti?RMKXvj6oISa-Lg5=oZi*hwtQds2UFytQMmC8zbx`q-2RT8t@@B`M2lst39E@$Qq{nE^n86e+t&yY1cWIf=y?0E59`FdqQOopC&IcPl$%Zd&jYJF&}m-wp<6=leVB0;=pxcx<xK*bE~c_tq{3hmBWjfR5rh7LQEt-i+9%%FMCi@iuF>T7T(Q#@x9b#|m0ho<RlM_u__aP8@f>%_hF*M}axwU4fs;21DPqumLhw2gsVQCLNfT<#@N-tktbd%<$In=y6f`%5qHCzlGL;A33q(LxDMre0Lf{pDQT-*l;|HKWwwtEg%&80TFb$9n*v5M1gC<2yB->0Q2y-VL2fmi7rOa!NluUK2D?9vFD8u432xybOe8S*sYkZbL%ynm`|b<nX<dfzKX`C<<Gu^@pcj8LvwU@8~Rhv0;XI)+-6_3TCJD4&LzLldrGd;(&XF@`J|ATZQCWZ;E{Am=|JTC$bnFElSqkKYYhE3_9wupM3+&r{cw_3i3QBcebe!RDJYo3T?;BaGEbpDlgL?Uf}}hje+%Y~l(aGA@r-jXk^jhaM+SDz8YGb#h{O*q4uXisj>llS9E*KrnzpW@kBvA#ie2WKL++v~q%=C^wE^?_Bfmvp6)#AAmk|znDbN7U`3f-%{Rf<R8il9?5_stTjG@WSx5xfH50M3=NQ(tJfBDj8t>7X3-xQka!~8k%9Lc}je4MrkQo83ve0dzu$D<TJ#Pfjn)`nA<TyO14Be*QEmoGUfy2s-vHsg{dzy6FVZO0L@XY1+07mHCUl7Pg5mxBjE*)Gaw>{u4+uf71O$~1zBh`0;tfx6&9hnjf+JHj90M9PgeNS6MzXu;Rn5`utyjTENw!s4=>$4oPEw`!oFUi5^Kj;3LR;|hF7Sl!=X=sWAuZSJf~pNlRIexw_9tlpYjzvyGv1sa}E!$cw4#5G1_lDgZ({hC%!+AkeNHW(0lBz+(b1zIs0R_arrbEQHIYs~jXt~0vpt$~=iptMgc88zs?q_NcRv|Qs72xwHJu|Hd)`oI74->P9W3Y{QkVvXVatba5?K!jamW;FLkyA^|enLYK$<T0a$o<~8qJl^yDJBJ&dIpJ6YTtqRuH{B^`J$BP&fBt0oqv-_X*BnvR#Z6b%3d}H$pcd6`g7BT#pcY7M(Ypah;MmNdYE74WxAB<udOg;uRYD$5Lr+j>27X}FWczBj3XfyQj$yc}nm2V_)R=_tDB*jAebw2Bct{6e0b3rQv3)b<NXTkJlYukghSj{4Pc`<O85>)htrmO6IxpbQR!jW>ch;)a(165@KLp@0aG-1QvA6b|S<TqL80_`q5DYeTkrfk4@6VqY@y8ZDd=ye$bm3XiTyz679{1L}mOn8auh)dK8N?d?tm*nA69?NI`%x^Y++>9S`Z4g$C^m5N>&%*l)S%AHAmC=WFJUDW-{oMcZJ(;kxlwJ{PLH)}m3Ryukpy=ThJI*Nk6_86xfiU%vPgQVN($M&o=zv*4yoOnAp--1G6dnJ;tV-V;(;hIs-XK=SypV)+Ha1cXaIvknQL~`b{;qc&~a#b3MD-t=xB}swumsqMw?n#$haAChyok_GzLE{%w~h%0}qihm<2X8<A4(Hn_BP21{K|i(L0_8&iv7_Q?<g8Kas0U_EG(t<GQR5GOE@!fSn8g-h){ih7J@ALOz_1#;_hv`cM!-6`&eAc7b<Uit=2?;|=A%M7#)ih!-KwQ4wQtM6?_!%*IZv#a1G=$wVb-NMRRdROHJj_eir28HbAbGN-YhprmURxEm#Xq_HO>@g74c)k#zy&)OS1u4Pbd-8!I;%hZsqpblB%3B*m@>f_`lMNO$Xd9|1N+GACeK{a2jlpV{VP+CAxU?b}e%7~J3uA<snMKwEOpafw_e#DEstx~49ya<f8wOl#1ikK`9e6?f{LS}GvL7EBiLCjYyFjWOt88(Hg_|pU<KS3L>%9}K;ZwDvGvPR;nk{UATXxdH~#WZeU6H?x+J6?_)fLU{9Ix&tOBn}P|$~}>=WZVpBi{$&8`H_Xj0STb@q-m1TP_K$hzKSBsb-f&3<hu5QFY8%&ZInFashZUD1h5=Mj=vevR;z?$hVbei?)bbee8?5^ZFV~R2<$b1PU!H+0J3MHA4Ep2rsh2LCt1C?5pmbZH6!rmS^|SnA0?W*;02&Nw%A!0FtvccBI!$`t~J3>Zw(qq55GnMouI17tmT@@q)0V}Wh7cf)8WL)Hq!&i8zrV<6OxGdhba%|eX{!Ce{a6Zk-e!k!A2jTqd0aVP*e_1n&M8d?b&sVArN@-_cA0b?IAl{=s<*pYUxB`atxY6MGx^w6m!$(u@)dClpuG2f4ItNr7tQnK*}pHFqcPlRo(nLLxsqpx~UDZ_{g{THF(okhGoWP9hA*RS;$3>xDSB3%2=MXReNi7GxifR6ae<DeZ_Wm*sEFzwa$yrqLxE<2;JVw_QJh%q~PVvFig6EXF<Nl9QHM9J&3HWpeYRZDQzWEu-IdU!*+L(&y}=c@V^A@w7ip+gD|wt(Okfyg3?cDr@;i?JABsnM|T|Uhp=Xdp84S&508C67+_M+%47Ruf9hK2Qx6Jl(~Y>wm-Y`1-&`CIuFuX-&-&O;r5b?!ftFSk;;?GHQvG2HaY;M}aI*!V9YpZh!AFN4H!G6201aKjRw6jZbCeB4QE3%Fv1YTG<zRua@myL;OgG#!S^4k%(|64PcexG1_hE^fw;T=k5qLPGI*#KYv>HhV)mp8<QePKY>7-3!r0=1}w0mrNC31`VS~>(mW3ov$0D{PPG=`Z%dFbgZ*%+0~%9WDRz=Er8Q5zw!e1m+_nNXKr2-_f|l%8Q~n@*@0oUoZ$OSk~96mgbqrzReosYwxyX&R2eXWdJSsRnOQ_e-gkq{Jpun1><s<1t8pZaMpaX0+A1s*(vH`BE2P71&QdNz1#7RcfD0wP<9h!(Tospi<m7WVA&isy&%RIp@wV!nyP1oI9&HujJHQ=^JES`qGU`mu_7w_lR~f9DA;>P+BXRQ4z*RZqTiW=wu97%`~8ViE`Bx*QBI}@J6*`seJ&sOLnll9gqz>s%pPj^^}X4v9C<nYv{Wn81+_{Vr;g}C@Iu-IGRqB)&R@yfux>9XbQ#<GT3Uem!&4`j`QG+i@V{FJ%%`2%Hx2fCr-pOE{9^Pq5@2-D5LqL_QhLurV{NI(;Qjghw)?e3Pzc*mz|MZ{)=&X0so-E5;BaCgQV`ka9-C~Z&zCWu^EB4oOx%`YLdREZWF6a%sc)b$Y$m=Gn?eSAb>m!LH*A;C`c3!`iYpZI22f_p*0r)F3$0vvU39Fa55LJ|D4+(Wz(AXW6B-DFnq^{qO7@heA++QPi=(WXyp14H#GYI&MMV`JXhD(!>Mi4?hlIX0wc0FVQ9{cHU=&7KSsf#Cp~a$J+#1_6j7_QmfXZtDsWBH551h2@whoL9}Mx}c!ms>DgXKoYt`A-=H|v`O+?=rDnfN0CG(c5Ez?onq^P}e;;Bm{2P0c&qR#A~%0y7!Y%JL;Znu$X_sWp`lvUrvu@$Ky?m;&_e}=lCD@X+Sn^8ooDSnn*i?TMHyaKtNiUpPs)Ut>S*>2RcX#jZu?>j#h>%L};g>w9<$Ch*fmxNYP&VR0^m~It$W@r|W)Lla}QK)5lq8@G`zRguy+Az!PIn}f_GH3=zn5O|rFEmiK7D{HclWL<l_c=H+sE=Ds%@$ivqD&FlXrW}9FP!iw!oEz;av`&xM7O`0p}|g}|0bq_Fe<R&-iSj~PR8XMp=i>GQk5n9qZ&)k9GKYeHEv>{TVBePTgj=9Ru8hIhgL`RLt;H23^HqjH42RQBW?!|SQ283Npz#EW9k|c^()BE+cn*ZrYcxabwiGxdMh9NraD#uNI&t~!im<~3W>CoR^+wq8S8+m?evn-;fYzg!VNkAiMYP`s+ZSUF~LSkFB9@EbJkXKHFW)<)QNB4(fB}G(u`bZ#0|)wuj&v|U`t3~oeAjt`p1CZR@p)dY#QPw>c#o7+-eocAixGv#vLnRz!1_zEW12Hia1(AHhs_aO>4=BA@z{o`*#|3=ogx$md_}PM4MJ<&O{%ghx)SNS_j7~<(6XV1*S`{n4&7RYzUU<qed}TBhf>hRw5`ub_=!U7u1)mrYQMG;Rx6Q(LSO*$x0uAoxFZ1UQ%09L#WQYxXvqCXQdXlTmLa?SfEMk6c`FYm#bxwf@v-1mD=x4!5$5M{-qqIp%!YM7AORI>LMv;au2#{(ocF_-m^=?hSPw1DrTTV+}|u}C>d43VIQjgBY7NK)YU=VvM|a>CZU|TE(tlNxQojSnF(^MR@F)8XM0g7;yoO8p^OP=q6s1?zGw+2Na8OXUrjr^sJ`==IT}IO$6{Yxeyb+u79*FNN{B|0Wbw4IM1msu7S>kp?}(0)Mrl$^^J$fIniIQzP_!i8eJ2`e5>8TtMEy`IC9_tbthfv*^G{abaz<~6^WNeq_$%AQW8dxMtXIfFqq^v3@3&MX|1cFGkDG!kH9_dd{>XR5J|FIgMcY;AsXB|IDs%<E;5)Y0*GsPBu*JIU;~G`#dKTT(#BN2p!lds=R-^H+VdSq$!SKqFNMA3`k1$R0Jv{kLvX^wHx`xMf+1zyKcxE^sh&Z=uVj>`t%4)Wr?@dIigS(C;=}*3e4^}QyT{C<6x}?>a%K8E>IHu8>lVX%8TdjCs?U&lHqSd*2$BJ&wY3a{jP`@)LIY8p)Ws|9)&x)nc*8et{;b-{JB8mJPJIYOA<!CdR)Ci9h^ZrZ|MX^J}+Z&nI|Arp!OC1xP_oYsRCC(|gdjCIrCz_|<g?mz*{C57S)S{L+sA79l=AlZKq~>ReeUvyaKYDj|F}OZ>fADT_eR6uyKY+jQ55(<)vKsrR-@U_q$?_T(XHecLFYoW2pC02qtBs;M-yU45K3<fSQxBV}^kX#e&*tCqIbArYto>u^$9!#@tyS0Rm9;Z>8X@o;0hsWKiBKJ(6UP%}4XVgP$F~q{BG`A$XpGQ0Li^6G6Soj-C14xDb^>+~>?Gg@f*T3=0>KvvxQXCq0&XF=m4Gi1e3^i+5PX$@uMvDrJo1Ty*G@gebP;oa#{~%W5c(!0=XlKR7;zs62dGGm?VtbDs-0tgj@T<z?*}Ex8^nNw-ymR0;2Q*n64*mvB!PVdED0PSz$I{qfGvT)A#f{!w+M_SaD;#(fxjd0Q3Bs0a3_Id1Y8N6ATW`@I|MuloFd>$;0%F40{=kZ2ML@b5K5qrKqP?y0<i=x5SU8fJp%U<_zr=Y1inY$K?0Wu%q8$&2>gh^yXhn_tv42FooSK8-nhwpnQn|XgC#bZ9zfIW;AA%O{WzM71fWUvXAuvv0+k66t7xT%=Dj}+otQ^j9UG7Wr&aXBnHgGoEs06%y!9tst1w0|Ed{d!?rE%52qh-1(+7V((yK%Qq1Av0LoY`V*R>f$$G#Zp2%cSE3=`bxJr7B0Bl#$>ap=2B%%eDTm7HZ!B5N#5ge|6PpO2i0SY(!b#Fa@Qra;nDTHm>MEKwq!a5^Chne9VZLTLQOwd;|5xn~I|=AnS`0WlE_jR<k^1h_*oLN-~UL$W|NnV>^5KQ`H)L$W<K8J|NkJT_ULL$W$HnVmy2IX2mxL$WtE8Jt5hHuh`LS4(u(5|Hd#^xYPnw?*%5(S2L=-xdtC1qW@xLR;|A7EH7S7j3~tTkz2qjI;$OZNW-g@X`^ybObLQ!AnQVK?*|f(h<CL1TP)IOGofR^0!I)Z@EjFHO60rr8X-yKJp@|Nsf5J#cX=!i7zJi4xSNrY(fAyeiH=5l1q?@kY3iwly%aw6Vq|ivI#=ysJP{r;`WJFh)D5<MWRwlrIk7h4Lq5;u@ks+GLY(7ksYA%e}^}U(PF}H97-GU&iAzS<3Z4oE+Ijf=D?dyS}Ce6QSB7fk*H3J+K{M?6!k))UZki^iP}t2TN1UEqFzeW%M|rWqF$w_*An$wqLj4&lrk1)5SytriD$NIT_XM>P}pf8aMOqj6h0_np1I~cEdn50OtL-;`Mu+dz$1X?MBZm1q^$yr4+_&Cg`9f{J`$js@x)RkRtoC-uB*lu5u}Eg>QJgUeSGarDGt2@r?iJkk`AaS?Zt(RhS2nY80Cf%7bMhHT5M&i#dP$iLmamtte&BCU=GRGn!{v-Li9rHZ>E;xzsDE+lbO0FNX^x%GOh8+RM=`!VKNo=TB{&B6~&_KGW=;@$qrHxCAkwGn+538ej-i4CbGoZg#|}m>|TnwpVOSnor`43K$jxOPP1D<>9iFVQ&vEC@G)NgaMew&GEoLPh;o-lROt!mEI;w@`BIIEDj}*e1<4##B|8Lk^fm?8@2#MRS##m>C7$!KpMEMnxKxD&<Qq!^*eSSJK^HG8HKw*%@X8Rrv;RF_-PVq{#NCbW)HxX5Aux)M$Z9E(%R(Y=lQXepD(QKd5{cLfKqK@$+wDo-zW$o&_#K6RUUY2t_17AaOGV@|OXN}!xy%z0ELGI|86h{_@6~N8dRxvgbY2>Ql^T1-maayu7qFLz%XBYG$zDpbmpQVDxC%I?w-*zwZwAw7Y<#E`XTS9d4NLr7R<7jzs;ssy>zKB$m{b>*xOlmUvY)?TN@)IaOD5@{Q817{mdzi_rffqeyKw3i@j`GD(xKSQts_l|i&RgpC0Zxw${$}Pa-1Rk?sMCOD3?zD?!_CVet^1a-kNG>Rqf(tuWQNmP#_;Fs#X5b@&8mE$1TachKruXE0uN9@KkRvf<Lr&mDc`SD#^v!6I6t@t?>fZ3HPRZc>3x73aTRCDU)%(7MbAoEZEeg2;xrQe+5<wvXvzisBi9|tD4lQN)ox0W*JvTgeq0LdMs5Yh4jEO<N-HhG=ED?8aY9hMn&n;%N>L|o_1<M2ZgKEOC5^TD`~S2DZQ)2z3Su2^Nr?dNiAU7e>GE8KBJFJl{z}jtH~23h09Qrlh*H*w9*N2U4OoWy5L)pe=GyguCachQskH^Qi+v}EGtC%;%V+KK;esLdil0!g0F9hPY$?%&%TRS@cKV_OZ-YlwayAR5*{Nk8*}ce#>;8dllsRkB1Ix^1)oG<8mQ*k)$~|n=?W&`3V^^$$Dm)ITP6hSSPr7VkU!d#*k3XQ>V^%dp12nAyLg99)EC!q)Ab=<dr%p(R&uRQ$px74<g3cGy)pdYkm))<lH19>wW?FAWnTLuk*$8gE+wq^@8(~z+o%`P6Ze|v)prbe9M7UtdwSSerrTxDpF>e{IXOe?12&SMFwk1!_h$Eil{c}0P0QaR%MeT7A}ilf%)ULAY5tTO*bDu-O`@FjQYTDi?K7HamN0XzDgR-!YQB7%S_CT%q4+Y<m)XKX8b8Y%mUHoRi&#$MX-2WI{b$<6LRwEYjb*J%t>dz}SR~g!i>`Ckx=(ls{K28u$`|U*a{YryPkOOD`voQaz4H7k;L_`-pI_46q}I{;h1FH>Y)g8NzO?-)!+9AjeUEBUWB9YPE<QYo-zk!x@m!}5YX2Xg?vn^tTL1t'.encode())).decode('utf-8')


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

    # Password post-process
    if args.password is None:

        # If no passwords set, enable no-password input+view mode
        if args.view_password is None:
            args.password = ''

        # If only view password set, enable password-protected view mode
        else:
            args.view_password = args.view_password.strip()

    else:

        # Enable password-protected input+view mode
        args.password = args.password.strip()

        # If view password is set, enable password-protected view mode
        if args.view_password is not None:
            args.view_password = args.view_password.strip()

    # Check for match and fallback to input + view mode
    if args.password == args.view_password:
        args.view_password = None

    # Set up server
    app = aiohttp.web.Application()

    # Routes
    app.router.add_get('/connect_input_ws', get__connect_input_ws)
    app.router.add_get('/connect_view_ws', get__connect_view_ws)
    app.router.add_get('/', get__root)

    # Listen
    aiohttp.web.run_app(app=app, port=args.port)
