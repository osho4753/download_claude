"""Shrink a browser-exported cookies.txt to YouTube cookies and print it as base64.

Usage:  python encode_cookies.py path/to/cookies.txt > cookies.b64
Paste the output into the COOKIES_B64 env var.
"""

import base64
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "cookies.txt"
with open(src, encoding="utf-8") as f:
    lines = f.read().splitlines()

# One entry per (domain, path, name); duplicates keep the one that expires last.
cookies: dict = {}
for line in lines:
    fields = line.split("\t")
    if len(fields) != 7:
        continue
    domain = fields[0].removeprefix("#HttpOnly_").lstrip(".")
    if domain != "youtube.com" and not domain.endswith(".youtube.com"):
        continue
    key = (fields[0], fields[2], fields[5])
    expiry = int(fields[4]) if fields[4].isdigit() else 0
    if key not in cookies or expiry >= cookies[key][0]:
        cookies[key] = (expiry, line)

if not cookies:
    sys.exit("No youtube.com cookies found - export them while logged in to YouTube.")

text = "# Netscape HTTP Cookie File\n" + "\n".join(line for _, line in cookies.values()) + "\n"
print(f"{len(cookies)} youtube.com cookies kept", file=sys.stderr)
sys.stdout.buffer.write(base64.b64encode(text.encode()))
