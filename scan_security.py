"""从 bandit JSON 提取 HIGH/MEDIUM findings + 敏感文件扫描"""
import json, os, re

print("=" * 70)
print("1. BANDIT HIGH + MEDIUM FINDINGS")
print("=" * 70)

for label, path in [("api-gateway", r"d:\ProgramFile\语伴\bandit_gw.json"),
                    ("ai-service", r"d:\ProgramFile\语伴\bandit_ai.json")]:
    print(f"\n--- {label} ---")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for r in data.get("results", []):
        sev = r.get("issue_severity", "?")
        conf = r.get("issue_confidence", "?")
        if sev in ("HIGH", "MEDIUM"):
            print(f"  [{sev}/{conf}] {r['test_id']} {r['test_name']}")
            print(f"    File: {r['filename']}:{r['line_number']}")
            print(f"    Text: {r['issue_text']}")
            code = r.get("code", "").strip().replace("\n", " | ")
            print(f"    Code: {code[:150]}")
            print()

print("\n" + "=" * 70)
print("2. 敏感文件 & 硬编码密钥扫描")
print("=" * 70)

ROOT = r"d:\ProgramFile\语伴"
SKIP_DIRS = {"models", ".git", "__pycache__", "node_modules", ".venv", "venv"}

KEY_PATTERNS = [
    (re.compile(r'(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*["\']?([A-Za-z0-9_\-]{16,})["\']?', re.I), "硬编码 Key/Secret"),
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "DashScope API Key"),
    (re.compile(r'AKIA[A-Z0-9]{16}'), "AWS Access Key"),
    (re.compile(r'-----BEGIN (RSA|EC|OPENSSH|DSA|PRIVATE) KEY-----'), "私钥文件"),
    (re.compile(r'jwt_secret\s*[:=]\s*["\'][^"\']{8,}["\']', re.I), "JWT Secret"),
]

hits = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    for fn in filenames:
        fp = os.path.join(dirpath, fn)
        try:
            if fn.endswith((".exe", ".dll", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".wav", ".mp3", ".zip", ".tar", ".gz", ".safetensors", ".pth", ".pt", ".bin")):
                continue
            sz = os.path.getsize(fp)
            if sz > 5 * 1024 * 1024:
                continue
            with open(fp, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for i, line in enumerate(content.splitlines(), 1):
                for pat, desc in KEY_PATTERNS:
                    m = pat.search(line)
                    if m:
                        hits.append((fp, i, desc, line.strip()[:120]))
        except Exception:
            continue

if hits:
    print(f"  发现 {len(hits)} 处可能敏感信息：\n")
    for fp, line_no, desc, line in hits[:30]:
        rel = os.path.relpath(fp, ROOT)
        # 脱敏显示
        masked = line
        if "sk-" in line:
            masked = re.sub(r'sk-[A-Za-z0-9]+', 'sk-***MASKED***', line)
        elif "Y9I4" in line or "JWT_SECRET" in line:
            masked = re.sub(r'(JWT_SECRET\s*=\s*)([A-Za-z0-9]+)', r'\1***MASKED***', line)
        print(f"  ⚠️  [{desc}] {rel}:{line_no}")
        print(f"     {masked}")
else:
    print("  ✅ 未发现明显硬编码密钥")

print("\n" + "=" * 70)
print("3. .gitignore 检查（是否排除敏感文件）")
print("=" * 70)

gitignore_path = os.path.join(ROOT, ".gitignore")
if os.path.exists(gitignore_path):
    with open(gitignore_path, encoding="utf-8") as f:
        gi = f.read()
    checks = [
        (".env", ".env 文件排除"),
        ("users.json", "users.json 账号表排除"),
        ("api-gateway/users.json", "网关账号表排除"),
        ("*.pth", "PyTorch 权重排除"),
        ("*.safetensors", "safetensors 排除"),
        ("models/", "模型目录排除"),
    ]
    for pattern, label in checks:
        found = pattern in gi
        status = "✅" if found else "❌"
        print(f"  {status} {label}")
else:
    print("  ❌ .gitignore 不存在！")
