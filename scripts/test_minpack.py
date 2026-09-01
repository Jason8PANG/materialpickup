"""Test minpack API with real LDAP login"""
import urllib.request, json, http.cookiejar, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
BASE = 'http://localhost:5000'
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# Read username/password from stdin to avoid encoding issues
import getpass
username = input('Username: ')
password = getpass.getpass('Password: ')

# Login via LDAP
data = json.dumps({'username': username, 'password': password}).encode()
req = urllib.request.Request(f'{BASE}/api/auth/login', data=data, headers={'Content-Type': 'application/json'})
try:
    r = opener.open(req)
    resp = json.loads(r.read())
    print(f'Login: {resp.get("user",{}).get("role")} site={resp.get("siteref")}')
except Exception as e:
    print(f'Login failed: {e}')
    exit()

# Try minpack create
payload = json.dumps({
    'items': [{'part_number': 'W0303408', 'quantity': 5, 'price': 0, 'stock_qty': 100, 'stock_loc': 'WH-A01'}],
    'remark': 'test minpack'
}).encode()
req = urllib.request.Request(f'{BASE}/api/requests/minpack', data=payload, headers={'Content-Type': 'application/json'})
try:
    r = opener.open(req)
    resp = json.loads(r.read())
    print(f'Minpack create: {resp}')
except urllib.error.HTTPError as e:
    body = e.read().decode(errors='replace')
    print(f'HTTP {e.code}: {body[:300]}')
except Exception as e:
    print(f'Error: {e}')
