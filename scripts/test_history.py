import urllib.request, json, http.cookiejar
BASE = 'http://localhost:5000'

def test(user):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    
    # Login
    data = json.dumps({'username': user, 'password': user}).encode()
    req = urllib.request.Request(f'{BASE}/api/auth/login', data=data, headers={'Content-Type': 'application/json'})
    opener.open(req)
    
    # History
    req = urllib.request.Request(f'{BASE}/api/requests/history?page=1&size=20')
    r = opener.open(req)
    d = json.loads(r.read())
    print(f'{user}: total={d.get("total",0)} rows={len(d.get("data",[]))}')

test('admin')
test('requester1')
test('requester2')
