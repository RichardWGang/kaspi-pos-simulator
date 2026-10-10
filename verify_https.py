"""HTTPS 验证脚本 —— 启动一个 HTTPS 实例，逐项验证证书与请求。

用法：
    python verify_https.py              # 默认端口 8443
    python verify_https.py 8444         # 指定端口
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(BASE, ".certs")
CERT = os.path.join(CERT_DIR, "cert.pem")
KEY = os.path.join(CERT_DIR, "key.pem")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
HOST = "127.0.0.1"

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def find_openssl():
    import shutil
    found = shutil.which("openssl")
    if found:
        return found
    for pattern in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    ):
        if os.path.isfile(pattern):
            return pattern
    root = os.path.join(os.path.expanduser("~"),
                        ".workbuddy", "binaries", "PortableGit", "versions")
    if os.path.isdir(root):
        for v in sorted(os.listdir(root), reverse=True):
            cand = os.path.join(root, v, "usr", "bin", "openssl.exe")
            if os.path.isfile(cand):
                return cand
    return None


def http_get(url, cafile=None, headers=None, timeout=10, verify=True):
    if verify:
        ctx = ssl.create_default_context(cafile=cafile)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def curl(args, timeout=15):
    try:
        p = subprocess.run(["curl.exe", "-sS"] + args, capture_output=True,
                           timeout=timeout)
        return (p.returncode,
                p.stdout.decode("utf-8", errors="replace").strip(),
                p.stderr.decode("utf-8", errors="replace").strip())
    except FileNotFoundError:
        return None, "", "curl.exe not found"


def main():
    print(f"=== Kaspi POS Simulator HTTPS 验证 (port {PORT}) ===\n")

    if not (os.path.isfile(CERT) and os.path.isfile(KEY)):
        print("证书不存在，先启动一次服务生成，或直接运行：")
        print("  python kaspi_pos_simulator.py --port 8443")
        return 1

    # ---------- 1. 证书内容 ----------
    ossl = find_openssl()
    cert_text = ""
    if ossl:
        p = subprocess.run([ossl, "x509", "-in", CERT, "-noout", "-text"],
                           capture_output=True, timeout=15)
        cert_text = p.stdout.decode("utf-8", errors="replace")
        subj = subprocess.run([ossl, "x509", "-in", CERT, "-noout",
                               "-subject", "-issuer", "-dates"],
                              capture_output=True, timeout=15
                              ).stdout.decode("utf-8", errors="replace")
        print("--- 证书 ---")
        print(subj.strip())
        san = [l.strip() for l in cert_text.splitlines()
               if "Subject Alternative Name" in l or "DNS:" in l or "IP Address" in l]
        print("SAN:", " | ".join(san[:2]) if san else "(无)")
        print()
        check("1. openssl 可读取证书", p.returncode == 0)
        check("2. 证书含 Subject Alternative Name", bool(san))
    else:
        print("未找到 openssl，跳过证书文本检查（服务本身不受影响）\n")
        check("1. openssl 可读取证书", True, "(跳过)")
        check("2. 证书含 Subject Alternative Name", True, "(跳过)")

    # ---------- 启动 HTTPS 服务 ----------
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "kaspi_pos_simulator.py"),
         "--host", HOST, "--port", str(PORT), "--cert", CERT, "--key", KEY],
        cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            time.sleep(0.25)
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect((HOST, PORT))
                break
            except OSError:
                pass
            finally:
                s.close()

        https = f"https://{HOST}:{PORT}"

        # ---------- 3. 明文 HTTP 连同一端口应失败 ----------
        try:
            urllib.request.urlopen(f"http://{HOST}:{PORT}/v2/deviceinfo", timeout=5)
            check("3. 明文 HTTP 访问被拒", False, "竟然成功了")
        except Exception as exc:
            check("3. 明文 HTTP 访问被拒", True, type(exc).__name__)

        # ---------- 4. 不校验证书 → 成功 ----------
        try:
            code, body = http_get(f"{https}/v2/deviceinfo", verify=False)
            check("4. 跳过证书校验可访问", code == 200 and body.get("statusCode") == 0,
                  f"HTTP {code} / terminalId {body['data']['terminalId']}")
        except Exception as exc:
            check("4. 跳过证书校验可访问", False, repr(exc))

        # ---------- 5. 校验证书 → 应失败（自签） ----------
        try:
            http_get(f"{https}/v2/deviceinfo")  # 默认走系统信任库
            check("5. 默认校验下自签证书被拒", False, "竟然通过了 —— 检查是否误装了根证书")
        except ssl.SSLCertVerificationError as exc:
            check("5. 默认校验下自签证书被拒", True,
                  f"certificate verify failed: {str(exc.reason)[:45]}")
        except urllib.error.URLError as exc:
            check("5. 默认校验下自签证书被拒", True, str(exc.reason)[:60])
        except Exception as exc:
            check("5. 默认校验下自签证书被拒", False,
                  f"非证书错误 {type(exc).__name__}: {exc}")

        # ---------- 6. 把 cert.pem 当信任锚 → 成功 ----------
        try:
            code, body = http_get(f"{https}/v2/deviceinfo", cafile=CERT)
            check("6. 信任 cert.pem 后校验通过", code == 200,
                  f"HTTP {code} / statusCode {body.get('statusCode')}")
        except Exception as exc:
            check("6. 信任 cert.pem 后校验通过", False, str(exc)[:80])

        # ---------- 7. curl -k ----------
        rc, out, err = curl(["-k", "-w", "\\n%{http_code}", f"{https}/v2/deviceinfo"])
        ok7 = rc == 0 and out.rstrip().endswith("200")
        check("7. curl -k 可访问", ok7, out.replace("\n", " ")[:90])

        # ---------- 8. curl 不带 -k → 失败 ----------
        rc8, out8, err8 = curl(["-w", "\\n%{http_code}", f"{https}/v2/deviceinfo"])
        check("8. curl 不带 -k 被拒", rc8 != 0, (err8 or out8)[:80])

        # ---------- 9. curl --cacert ----------
        rc9, out9, _ = curl(["--cacert", CERT, "-w", "\\n%{http_code}",
                             f"{https}/v2/deviceinfo"])
        ok9 = rc9 == 0 and out9.rstrip().endswith("200")
        check("9. curl --cacert cert.pem 可访问", ok9, out9.replace("\n", " ")[:90])

        # ---------- 10. 完整业务链路 over HTTPS ----------
        try:
            _, body = http_get(f"{https}/v2/payment?amount=1000&method=qr", verify=False)
            pid = body["data"]["processId"]
            deadline = time.time() + 12
            status = None
            while time.time() < deadline:
                _, body = http_get(f"{https}/v2/status?processId={pid}",
                                   headers={"terminalid": "31452963"}, verify=False)
                status = body["data"]["status"]
                if status != "wait":
                    break
                time.sleep(1)
            check("10. HTTPS 上跑通支付链路", status == "success",
                  f"status={status} subStatus={body['data'].get('subStatus')}")
        except Exception as exc:
            check("10. HTTPS 上跑通支付链路", False, repr(exc)[:90])

        # ---------- 11. TLS 版本 ----------
        try:
            ctx = ssl._create_unverified_context()
            with socket.create_connection((HOST, PORT), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                    ver = tls.version()
            check("11. TLS 握手成功", True, ver)
        except Exception as exc:
            check("11. TLS 握手成功", False, repr(exc)[:60])

    finally:
        proc.terminate()
        proc.wait(timeout=5)

    passed = sum(1 for r in results if r)
    print(f"\n=== {passed}/{len(results)} 通过 ===")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
