"""Control experiment: how much reasoning does this model spend on trivial prompts?

Three calls, all tiny: text-only arithmetic, text-only colour question, and the same colour
question with a 4x4 red PNG attached. The point is to see whether long reasoning is a property
of the model or of image input.
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
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
    key = winreg.QueryValueEx(handle, "PAPER_READER_KEY")[0]


def ask(label, content, max_tokens):
    payload = {"model": "deepseek-flash", "max_tokens": max_tokens, "temperature": 0,
               "messages": [{"role": "user", "content": content}]}
    request = urllib.request.Request("https://api.deepseek.com/chat/completions",
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        print(json.dumps({"probe": label, "max_tokens": max_tokens,
                          "finish_reason": choice.get("finish_reason"),
                          "content": (message.get("content") or "")[:120],
                          "completion_tokens": usage.get("completion_tokens"),
                          "reasoning_tokens": details.get("reasoning_tokens"),
                          "prompt_tokens": usage.get("prompt_tokens")}, ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        print(json.dumps({"probe": label, "http_status": exc.code,
                          "error": exc.read().decode("utf-8", "replace")[:300]},
                         ensure_ascii=False))


ask("text-only arithmetic", "1+1 等于几？只回答数字。", 512)
ask("text-only colour", "红玫瑰通常是什么颜色？只回答颜色名。", 512)
ask("image colour", [{"type": "text", "text": "这张图是什么颜色？只回答颜色名。"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}],
    4096)
