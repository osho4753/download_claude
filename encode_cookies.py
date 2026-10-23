"""Shrink a browser-exported cookies.txt to YouTube cookies and print it as base64.

Usage:  python encode_cookies.py path/to/cookies.txt > cookies.b64
Paste the output into the COOKIES_B64 env var.
"""

import base64
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "cookies.txt"
with open(src, encoding="utf-8") as f:
    lines = f.read().splitlines()

kept = ["# Netscape HTTP Cookie File"] + [
    line for line in lines if line and not line.startswith("# ") and "youtube.com" in line.split("\t")[0]
]
if len(kept) == 1:
    sys.exit("No youtube.com cookies found - export them while logged in to YouTube.")

print(f"{len(kept) - 1} youtube.com cookies kept", file=sys.stderr)
print(base64.b64encode(("\n".join(kept) + "\n").encode()).decode())
