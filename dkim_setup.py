"""Create DKIM keypair + print the DNS records you must publish.

Usage:
    python dkim_setup.py --domain send.yourdomain.com --ip 1.2.3.4
    python dkim_setup.py --domain yourdomain.com          # SKIP THIS if sending from apex
    python dkim_setup.py --domain send.yourdomain.com --ip 1.2.3.4 --selector mail

Then sign with:
    python run_pipeline.py ... --dkim-key dkim_mail_private.pem --dkim-selector mail
"""

import argparse
import base64
import io
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="Create DKIM keypair + DNS records")
    ap.add_argument("--domain", required=True,
                    help="sending domain, e.g. send.yourdomain.com (recommended) or yourdomain.com")
    ap.add_argument("--ip", default=None, help="your VPS IPv4 for SPF (omit for mx-based SPF)")
    ap.add_argument("--selector", default="mail")
    ap.add_argument("--key-out", default=None, help="output PEM path (default dkim_<selector>_private.pem)")
    args = ap.parse_args()

    domain = args.domain.rstrip(".")
    selector = args.selector
    path = args.key_out or f"dkim_{selector}_private.pem"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(pem)

    spki_der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    p_value = base64.b64encode(spki_der).decode()

    print(f"Private key written to: {path}\n")
    print("=" * 72)
    print("  DNS RECORDS FOR A SUBDOMAIN SENDER  (send.yourdomain.com style)")
    print("=" * 72)
    print(f"""
1) DKIM  (signature check) - TXT record
   Name :  {selector}._domainkey.{domain}
   Value:
   "v=DKIM1; k=rsa; p={p_value}"

2) SPF   (who may send FROM this domain) - TXT record
   Name :  {domain}
   Value:
   {"v=spf1 ip4:" + args.ip + " ~all" if args.ip else "v=spf1 ip4:YOUR_VPS_IP ~all   (substitute YOUR_VPS_IP, then message me the IP and I'll re-print cleanly)"}

3) DMARC (how receivers treat failures) - TXT record
   Name :  _dmarc.{domain}
   Value:
   v=DMARC1; p=none; rua=mailto:dmarc@{domain}

4) PTR  (reverse DNS) - set this in your VPS/hosting panel, NOT your domain DNS
   IP ->  {domain}
""")

    print("=" * 72)
    print("  tip: for per-provider relay keys use dkim_providers.py (one")
    print("  subdomain + key per ESP). DO NOT reuse a DKIM selector/key on")
    print("  multiple domains.")
    print("=" * 72)


if __name__ == "__main__":
    main()