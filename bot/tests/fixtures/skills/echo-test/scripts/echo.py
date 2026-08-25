import json
import os
import sys

args = json.loads(sys.stdin.read())
result = {
    "echoed": args.get("message", ""),
    "workspace_exists": os.path.isdir(os.environ.get("WORKSPACE_DIR", "")),
    "has_path": "PATH" in os.environ,
}
print(json.dumps(result))
