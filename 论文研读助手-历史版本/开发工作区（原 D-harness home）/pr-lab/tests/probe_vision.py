"""Does this OpenAI-compatible endpoint accept image input? One tiny, cheap probe.

Builds a 4x4 solid red PNG with the standard library only, sends it as an image part together
with a one-line question, and reports what came back. No paper content is involved.
"""
import base64
import json
import struct
import urllib.error
import urllib.request
import winreg
import zlib


def png_bytes(width, height, rgb):
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


image = base64.b64encode(png_bytes(4, 4, (220, 30, 30))).decode("ascii")
payload = {
    "model": "deepseek-flash",
    "max_tokens": 512,
    "temperature": 0,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "这张图是什么颜色？只回答颜色名。"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
    ]}],
}
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
    key = winreg.QueryValueEx(handle, "PAPER_READER_KEY")[0]

request = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
request.add_header("Content-Type", "application/json")
request.add_header("Authorization", "Bearer " + key)
report = {"image_bytes": len(image)}
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    report["http_status"] = response.status
    report["finish_reason"] = choice.get("finish_reason")
    report["content"] = (message.get("content") or "")[:200]
    report["usage"] = body.get("usage")
    report["accepted_images"] = bool(report["content"])
except urllib.error.HTTPError as exc:
    report["http_status"] = exc.code
    report["error_body"] = exc.read().decode("utf-8", "replace")[:400]
print(json.dumps(report, ensure_ascii=False, indent=2))
