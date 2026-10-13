"""Generate throwaway self-signed certificates for the TLS tests.

No OpenSSL and no third-party Python package is available here, so the keys are created in
memory by .NET's `RSA.Create` (which can export its parameters) and the certificates are
self-signed with `CertificateRequest`. The private key is turned into PKCS#8 PEM in Python
from the exported parameters, and the result is *proven* by loading it into an
`ssl.SSLContext` before this script reports success.

Why self-signed leaves instead of a CA plus leaves
--------------------------------------------------
A two-level chain built this way was accepted by Windows (`X509Chain.Build` returned true)
but rejected by the OpenSSL 3.0 that Python links against, even with identical issuer and
subject bytes, a valid Authority Key Identifier and a `basicConstraints ca=TRUE` root. The
tests do not need a hierarchy: each case is expressed by *which* certificate the client is
told to trust, which is exactly what the policy under test cares about. A self-signed
certificate loaded explicitly as a trust anchor is honoured by OpenSSL.

    python tests/security/make_test_certs.py
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

CERT_DIR = Path(__file__).resolve().parent / "certs"

PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$out = '__OUT__'

function Get-Parameters($rsa) {
  $p = $rsa.ExportParameters($true)
  return [ordered]@{
    modulus  = [System.Convert]::ToBase64String($p.Modulus)
    exponent = [System.Convert]::ToBase64String($p.Exponent)
    d        = [System.Convert]::ToBase64String($p.D)
    p        = [System.Convert]::ToBase64String($p.P)
    q        = [System.Convert]::ToBase64String($p.Q)
    dp       = [System.Convert]::ToBase64String($p.DP)
    dq       = [System.Convert]::ToBase64String($p.DQ)
    qi       = [System.Convert]::ToBase64String($p.InverseQ)
  }
}

function To-Pem([string]$label, [byte[]]$bytes) {
  $b64 = [System.Convert]::ToBase64String($bytes)
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.AppendLine('-----BEGIN ' + $label + '-----')
  for ($i = 0; $i -lt $b64.Length; $i += 64) {
    $len = [Math]::Min(64, $b64.Length - $i)
    [void]$sb.AppendLine($b64.Substring($i, $len))
  }
  [void]$sb.AppendLine('-----END ' + $label + '-----')
  return $sb.ToString()
}

function New-SelfSigned([string]$subject, [string[]]$san) {
  $rsa = [System.Security.Cryptography.RSA]::Create(2048)
  $parameters = Get-Parameters $rsa
  $req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest(
      $subject, $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
      [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
  $req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension($false, $false, 0, $true)))
  $usage = [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature -bor
           [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyEncipherment
  $req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509KeyUsageExtension($usage, $false)))
  $req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension($rsa, $false)))
  $builder = New-Object System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder
  foreach ($entry in $san) {
    if ($entry.StartsWith('IP:')) { $builder.AddIpAddress([System.Net.IPAddress]::Parse($entry.Substring(3))) }
    else { $builder.AddDnsName($entry.Substring(4)) }
  }
  $req.CertificateExtensions.Add($builder.Build($false))
  $now = [System.DateTimeOffset]::UtcNow
  $cert = $req.CreateSelfSigned($now.AddDays(-1), $now.AddDays(30))
  return @{ cert = $cert; parameters = $parameters }
}

$targets = @(
  @{ name = 'server';    subject = 'CN=localhost';        san = @('DNS:localhost', 'IP:127.0.0.1') },
  @{ name = 'wrong';     subject = 'CN=wrong-host.test';  san = @('DNS:wrong-host.test') },
  @{ name = 'untrusted'; subject = 'CN=untrusted.test';   san = @('DNS:untrusted.test') }
)
foreach ($target in $targets) {
  $result = New-SelfSigned $target.subject $target.san
  $pem = To-Pem 'CERTIFICATE' $result.cert.RawData
  # trusted.pem is what a client is told to trust: for the server case that is the leaf itself
  Set-Content -Path (Join-Path $out ($target.name + '-cert.pem')) -Value $pem -Encoding ascii
  Set-Content -Path (Join-Path $out ($target.name + '-chain.pem')) -Value $pem -Encoding ascii
  $result.parameters | ConvertTo-Json | Set-Content -Path (Join-Path $out ($target.name + '-rsa.json')) -Encoding UTF8
  Write-Output ($target.name + ' generated')
}
Write-Output 'generated'
"""


def run_powershell(script):
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if completed.returncode != 0:
        raise SystemExit("powershell failed:\n" + (completed.stdout or "")[-600:]
                         + (completed.stderr or "")[-1200:])
    return completed.stdout.strip()


# --- PKCS#8 assembly ------------------------------------------------------------------
def _der_length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    encoded = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _der_tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(payload)) + payload


def _der_int(raw: bytes) -> bytes:
    value = raw.lstrip(b"\x00") or b"\x00"
    if value[0] & 0x80:
        value = b"\x00" + value
    return _der_tlv(0x02, value)


def pkcs8_from_rsa_parameters(parameters: dict) -> str:
    """Build an unencrypted PKCS#8 key from exported RSA parameters.

    RSAPrivateKey  ::= SEQUENCE { version 0, n, e, d, p, q, dp, dq, qi }
    PrivateKeyInfo ::= SEQUENCE { version 0, AlgorithmIdentifier(rsaEncryption),
                                  privateKey OCTET STRING }
    """
    def value(name):
        return base64.b64decode(parameters[name])

    rsa = _der_tlv(0x30, b"".join([
        _der_tlv(0x02, b"\x00"),
        _der_int(value("modulus")), _der_int(value("exponent")), _der_int(value("d")),
        _der_int(value("p")), _der_int(value("q")), _der_int(value("dp")),
        _der_int(value("dq")), _der_int(value("qi")),
    ]))
    oid = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01])
    algorithm = _der_tlv(0x30, _der_tlv(0x06, oid) + _der_tlv(0x05, b""))
    info = _der_tlv(0x02, b"\x00") + algorithm + _der_tlv(0x04, rsa)
    encoded = base64.encodebytes(_der_tlv(0x30, info)).decode("ascii").strip()
    return f"-----BEGIN PRIVATE KEY-----\n{encoded}\n-----END PRIVATE KEY-----\n"


def write_keys():
    for name in ("server", "wrong", "untrusted"):
        source = CERT_DIR / f"{name}-rsa.json"
        parameters = json.loads(source.read_text(encoding="utf-8-sig"))
        (CERT_DIR / f"{name}-key.pem").write_text(pkcs8_from_rsa_parameters(parameters),
                                                  encoding="ascii")
        source.unlink()                     # the exported parameters are not kept around


def verify():
    """Prove the material loads and that a TLS handshake with the test trust works."""
    import socket
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(CERT_DIR / "server-cert.pem"),
                                   str(CERT_DIR / "server-key.pem"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.socket = server_context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client_context = ssl.create_default_context(cafile=str(CERT_DIR / "server-cert.pem"))
    raw = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    wrapped = client_context.wrap_socket(raw, server_hostname="localhost")
    version, cipher = wrapped.version(), wrapped.cipher()[0]
    wrapped.close()
    httpd.shutdown()
    httpd.server_close()
    return {"handshake": "ok", "tls": version, "cipher": cipher,
            "client_verify_mode": str(client_context.verify_mode),
            "client_check_hostname": bool(client_context.check_hostname)}


def main():
    if CERT_DIR.exists():
        shutil.rmtree(CERT_DIR)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    print(run_powershell(PS_TEMPLATE.replace("__OUT__", str(CERT_DIR))).splitlines()[-1])
    write_keys()
    report = verify()
    (CERT_DIR / "README.txt").write_text(
        "Test-only self-signed certificates generated by make_test_certs.py.\n"
        "Not installed into the operating system trust store: the suite passes the\n"
        "certificate it wants trusted to the SSL context it builds.\n"
        "A client that trusts a different file fails verification, which is the point.\n"
        "The RSA parameter JSON files are deleted once the PEM keys are written.\n",
        encoding="utf-8")
    files = sorted(item.name for item in CERT_DIR.iterdir())
    report["files"] = files
    print(json.dumps(report, ensure_ascii=False, indent=2))
    required = {"server-cert.pem", "server-chain.pem", "server-key.pem",
                "wrong-cert.pem", "wrong-chain.pem", "wrong-key.pem",
                "untrusted-cert.pem", "untrusted-chain.pem", "untrusted-key.pem"}
    missing = sorted(required - set(files))
    if missing:
        print("missing:", missing)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
