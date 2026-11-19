"""Heuristic release scan. Not a substitute for a dedicated secret scanner."""
from pathlib import Path
import re
import sys
root = Path(__file__).resolve().parents[1]
patterns = [r"AKIA[0-9A-Z]{16}", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9]{30,}", r"[0-9]{8,12}:[A-Za-z0-9_-]{35}"]
issues=[]
for p in root.rglob('*'):
    if not p.is_file() or any(x in p.parts for x in ('.git','.venv','__pycache__','.pytest_cache')): continue
    if p.name == '.env' or p.suffix in ('.session','.db','.sqlite','.pem','.key'): issues.append(str(p.relative_to(root)))
    try: text=p.read_text()
    except UnicodeError: continue
    if any(re.search(pattern,text) for pattern in patterns): issues.append(str(p.relative_to(root)))
print('Potential secret files: ' + str(len(set(issues))))
for path in sorted(set(issues)): print(path)
sys.exit(bool(issues))
