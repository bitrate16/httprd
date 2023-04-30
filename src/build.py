# Build two files into one

import base64

def replace_template(src: str, template_name: str, new_text: str):
	"""
	Replace tag with structure:

	```
	# <tempalte:template_name>
	'something to replace'
	# </template:template_name>
	```
	"""

	tps = f'# <template:{ template_name }>'
	tpe = f'# </template:{ template_name }>'

	ind_start = src.index(tps)
	ind_end = src.index(tpe) + len(tpe)

	return f'{ src[:ind_start] }{ new_text }{ src[ind_end:] }'


with open('index.html', 'r', encoding='utf-8') as f:
	page = f.read()

with open('httprd.py', 'r', encoding='utf-8') as f:
	httprd = f.read()


import json
page = page.replace('\t', '')
page = page.replace('\n\n', '\n')
page = page.replace('\n\n', '\n')
page = page.replace('\n\n', '\n')
page = page.replace('\n\n', '\n')
page = base64.b85encode(page.encode('utf-8')).decode()

httprd = replace_template(httprd, 'INDEX_CONTENT', f'''INDEX_CONTENT = base64.b85decode('{ page }'.encode()).decode('utf-8')''')
httprd = replace_template(httprd, 'get__root', f'''return aiohttp.web.Response(body=INDEX_CONTENT, content_type='text/html', status=200, charset='utf-8')''')


with open('./../httprd.py', 'w', encoding='utf-8') as f:
	f.write(httprd)
