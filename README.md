# httprd
Single-script remote desktop with web access, password & settings

![httprd.py](demo.png)

# Features
* Single-script
* Configurable framerate, Input per second, resolution, JPEG quality
* Mouse input
* Websocket connection for less overhead

# Download
[httprd.py](httprd.py)

Connect to `127.0.0.1:7417`

# Usage
`python httprd.py`

# Requirements
* `aiohttp`
* `Pillow`
* `pyautogui`

# Build
`cd src && python build.py`

# License
```
httprd: web-based remote desktop
Copyright (C) 2022-2023  bitrate16

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```