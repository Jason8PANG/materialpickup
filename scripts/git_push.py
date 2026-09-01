import requests, base64, json, os

# GitHub token 从环境变量 GITHUB_TOKEN 读取（避免明文写入仓库，规避 secret scanning）
TOKEN = os.environ.get('GITHUB_TOKEN', '')
OWNER = 'Jason8PANG'
REPO = 'materialpickup'
HEADERS = {'Authorization': f'token {TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
API = f'https://api.github.com/repos/{OWNER}/{REPO}'

r = requests.get(f'{API}/git/refs/heads/main', headers=HEADERS)
commit_sha = r.json()['object']['sha']
r = requests.get(f'{API}/git/commits/{commit_sha}', headers=HEADERS)
base_tree_sha = r.json()['tree']['sha']

base_dir = 'D:/Workbuddy/多智能体/物料领取看板'
skip_dirs = {'.git', '__pycache__', 'flask_session', 'venv', '.venv', 'node_modules', '需求文档', 'deploy', 'tests', 'scripts', '.pytest_cache'}
skip_files = {'.env', '.env.bak', 'pytest_out.log'}

tree = []
for root, dirs, files in os.walk(base_dir):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for f in files:
        fp = os.path.join(root, f)
        rel = os.path.relpath(fp, base_dir).replace(os.sep, '/')
        if f in skip_files or f.endswith('.pyc') or f.endswith('.zip'):
            continue
        with open(fp, 'rb') as fh:
            content = fh.read()
        r = requests.post(f'{API}/git/blobs', json={'content': base64.b64encode(content).decode(), 'encoding': 'base64'}, headers=HEADERS)
        sha = r.json()['sha']
        tree.append({'path': rel, 'mode': '100644', 'type': 'blob', 'sha': sha})

r = requests.post(f'{API}/git/trees', json={'tree': tree, 'base_tree': base_tree_sha}, headers=HEADERS)
new_tree_sha = r.json()['sha']
r = requests.post(f'{API}/git/commits', json={
    'message': 'feat: 卷标状态收敛为在车间/在库存/盘点中/已消完（移除已消耗死状态）+ 盘点锁定接入 external API',
    'tree': new_tree_sha,
    'parents': [commit_sha]
}, headers=HEADERS)
new_commit_sha = r.json()['sha']
r = requests.patch(f'{API}/git/refs/heads/main', json={'sha': new_commit_sha, 'force': False}, headers=HEADERS)
print(f'Pushed {len(tree)} files. Status: {r.status_code}')
