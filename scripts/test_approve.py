import requests, json
BASE = 'http://localhost:5000'
s = requests.Session()

# Login as supervisor1 (310)
r = s.post(f'{BASE}/api/auth/login', json={'username': 'supervisor1', 'password': 'supervisor1'})
print(f'1. Login supervisor1: {r.status_code}')

# Try to approve request ID=2 (310)
r = s.post(f'{BASE}/api/requests/2/approve', json={'comment': 'Approved'})
print(f'2. Approve ID=2: {r.status_code}')
ct = r.headers.get('content-type', '')
if 'json' in ct:
    print(f'   RESP: {r.text}')
else:
    print(f'   HTML ERROR: {r.text[:300]}')
