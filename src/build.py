# Build two files into one

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
page = json.dumps(page)
httprd = httprd.replace('INDEX_CONTENT = None', f'INDEX_CONTENT = { page }')

with open('./../httprd.py', 'w', encoding='utf-8') as f:
	f.write(httprd)
