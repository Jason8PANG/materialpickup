"""Submit a request and verify email is sent"""
import urllib.request, json, http.cookiejar
BASE = 'http://localhost:5000'

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# Login as requester1
data = json.dumps({'username': 'requester1', 'password': 'requester1'}).encode()
req = urllib.request.Request(f'{BASE}/api/auth/login', data=data, headers={'Content-Type': 'application/json'})
opener.open(req)

# Create a request with items
payload = json.dumps({
    'remark': '测试邮件发送',
    'items': [{'job_order': 'J000001-0001', 'part_number': 'TEST001', 'quantity': 5}]
}).encode()
req = urllib.request.Request(f'{BASE}/api/requests', data=payload, headers={'Content-Type': 'application/json'})
r = opener.open(req)
resp = json.loads(r.read())
print(f'Create request: {resp}')
