#!/usr/bin/env python3
"""Live download page for the project bundle (used inside the dev sandbox).

Serves three things on port 8000:

  /                  a small Persian page with the download options
  /divar_hunter.zip  the bundle, with Content-Disposition: attachment
  /copy              the whole bundle as base64 text + a copy button,
                     for browsers that block downloads inside the preview iframe

The bundle itself lives in ``bundle/divar_hunter.zip`` and is committed to git,
so a permanent direct link always exists even when this sandbox is asleep:

  https://github.com/fa833618/divar-hunter/raw/arena/01a0d935-divar-hunter/bundle/divar_hunter.zip
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "bundle", "divar_hunter.zip")
RAW_URL = (
    "https://github.com/fa833618/divar-hunter/raw/"
    "arena/01a0d935-divar-hunter/bundle/divar_hunter.zip"
)

PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>دانلود divar_hunter.zip</title>
<style>
 body{font-family:Tahoma,Vazirmatn,system-ui,sans-serif;background:#0f1115;color:#e8e8e8;
      margin:0;padding:32px 16px;line-height:1.9}
 .card{max-width:760px;margin:0 auto;background:#171a21;border:1px solid #262b36;
       border-radius:14px;padding:26px 28px}
 h1{font-size:21px;margin:0 0 6px}
 a.btn{display:inline-block;background:#22c55e;color:#06210f;font-weight:700;padding:13px 26px;
       border-radius:10px;text-decoration:none;font-size:17px;margin:10px 6px 10px 0}
 a.btn.alt{background:#3b82f6;color:#04101f}
 a.btn:hover{filter:brightness(.92)}
 code{background:#0b0d10;padding:2px 6px;border-radius:5px;color:#9fd3ff;direction:ltr;
      display:inline-block;word-break:break-all}
 pre{background:#0b0d10;padding:12px;border-radius:10px;direction:ltr;text-align:left;overflow:auto}
 .muted{color:#9aa4b2;font-size:13px}
</style>
</head>
<body>
<div class="card">
 <h1>📦 divar_hunter.zip — __SIZE__ کیلوبایت</h1>
 <p class="muted">کل کدبیس «شکارچی معاملهٔ دیوار + بات تلگرام» — ۳۵ فایل</p>

 <a class="btn" href="/divar_hunter.zip" target="_blank" rel="noopener">⬇️ دانلود مستقیم (تب جدید)</a>
 <a class="btn alt" href="/copy" target="_blank" rel="noopener">📋 کپی به‌صورت متن (بدون دانلود)</a>

 <p>اگر هیچ‌کدام داخل این کادر کار نکرد، این لینک <b>دائمی</b> را کپی کن و در مرورگر خودت باز کن
    (حتی وقتی این سندباکس خاموش است هم کار می‌کند):<br>
    <code>__RAW__</code></p>

 <h1 style="font-size:16px">بعد از extract</h1>
<pre>cd divar-hunter
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp env.example .env
python main.py --demo</pre>
 <p class="muted">sha256: <code>__SHA__</code></p>
</div>
</body>
</html>
"""

COPY = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>کپی کل ZIP به‌صورت متن</title>
<style>
 body{font-family:Tahoma,Vazirmatn,system-ui,sans-serif;background:#0f1115;color:#e8e8e8;
      margin:0;padding:26px 16px;line-height:1.9}
 .card{max-width:880px;margin:0 auto;background:#171a21;border:1px solid #262b36;
       border-radius:14px;padding:24px 26px}
 h1{font-size:19px;margin:0 0 8px}
 button{background:#22c55e;color:#06210f;font-weight:700;border:0;border-radius:10px;
        padding:13px 28px;font-size:17px;cursor:pointer;margin:10px 0}
 textarea{width:100%;height:220px;background:#0b0d10;color:#8be9a8;border:1px solid #262b36;
          border-radius:10px;padding:10px;font-family:Consolas,monospace;font-size:11px;direction:ltr}
 pre{background:#0b0d10;padding:12px;border-radius:10px;direction:ltr;text-align:left;overflow:auto}
 .muted{color:#9aa4b2;font-size:13px}
 #status{font-weight:700;color:#fbbf24}
</style>
</head>
<body>
<div class="card">
 <h1>📋 کل فایل ZIP به‌صورت متن base64</h1>
 <p class="muted">دانلود داخل این کادر گاهی بلاک می‌شود، ولی کپیِ متن همیشه ممکن است.
    متن پایین دقیقاً همان <code>divar_hunter.zip</code> است.</p>
 <button id="copy">۱) کپی کن کل متن را</button> <span id="status"></span>
 <ol>
  <li>دکمه را بزن (نشده؟ داخل کادر کلیک کن، Ctrl+A و Ctrl+C).</li>
  <li>در یک فایل خالی پیست کن و با نام <code>zip.b64</code> ذخیره کن.</li>
  <li>کنارش این دستور را بزن:</li>
 </ol>
 <pre># Windows:
certutil -decode zip.b64 divar_hunter.zip
# macOS / Linux:
base64 -d zip.b64 &gt; divar_hunter.zip</pre>
 <p class="muted">sha256 خروجی باید برابر باشد با: <code>__SHA__</code></p>
 <textarea id="b64" spellcheck="false">__B64__</textarea>
</div>
<script>
document.getElementById('copy').onclick = async function () {
  var t = document.getElementById('b64'), s = document.getElementById('status');
  t.value = t.value.trim();
  try { await navigator.clipboard.writeText(t.value); s.textContent = '✅ کپی شد — برو مرحلهٔ ۲'; }
  catch (e) {
    t.focus(); t.select();
    var ok = false; try { ok = document.execCommand('copy'); } catch (e2) {}
    s.textContent = ok ? '✅ کپی شد — برو مرحلهٔ ۲' : '⚠️ دستی: داخل کادر Ctrl+A و Ctrl+C';
  }
};
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _zip_bytes(self) -> bytes:
        with open(ZIP, "rb") as fh:
            return fh.read()

    def _send(self, body: bytes, ctype: str, disposition: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            blob = self._zip_bytes()
            page = (
                PAGE.replace("__SIZE__", str(len(blob) // 1024))
                .replace("__SHA__", hashlib.sha256(blob).hexdigest())
                .replace("__RAW__", RAW_URL)
            )
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        elif path in ("/divar_hunter.zip", "/download", "/dl", "/zip"):
            self._send(
                self._zip_bytes(), "application/zip", 'attachment; filename="divar_hunter.zip"'
            )
        elif path == "/copy":
            blob = self._zip_bytes()
            page = (
                COPY.replace("__SHA__", hashlib.sha256(blob).hexdigest())
                .replace("__B64__", base64.b64encode(blob).decode("ascii"))
            )
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        with open(os.path.join(ROOT, "logs", "preview_server.log"), "a", encoding="utf-8") as fh:
            fh.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    print(f"serving {ZIP} on 0.0.0.0:8000")
    http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
