"""Create a DKIM keypair per relay provider, each on its own sending
subdomain, and print the DNS records to publish for every provider.

When you relay through an ESP (Brevo / SMTP2GO / Mailjet / SendPulse) the
delivering IP is THEIRS, so you cannot set PTR. What you CAN control, and what
keeps you out of spam, is a clean aligned sending domain per provider.

Recommended pattern: one subdomain per provider that you send from. Each
gets its own DKIM key+selector and its own SPF (delegating only to that
provider).

Usage:
    python dkim_providers.py --domain xprfire.site \
        --subsend send \
        --providers sendpulse brevo smtp2go mailjet

Publishes (per provider p, subdomain <subsend>.<p>.<domain>):
    1) DKIM  TXT  <selector>._domainkey.<sub>
    2) SPF   TXT  <sub>          v=spf1 include:<provider> ~all
"""

import argparse
import base64
import io
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROVIDER_SPF = {
    "brevo": "spf.brevo.com",
    "smtp2go": "smtp2go.com",
    "mailjet": "spf.mailjet.com",
    "sendpulse": "spf.sendpulse.com",
}


def main():
    ap = argparse.ArgumentParser(description="Per-provider DKIM keys + DNS records")
    ap.add_argument("--domain", required=True, help="your base domain, e.g. xprfire.site")
    ap.add_argument("--providers", nargs="+", required=True,
                    help="one of: brevo smtp2go mailjet sendpulse")
    ap.add_argument("--selector", default="mail")
    ap.add_argument("--key-dir", default=".",
                    help="directory to write per-provider PEM keys (default .)")
    args = ap.parse_args()

    domain = args.domain.rstrip(".")

    print(f"One DKIM sending subdomain per provider (all under {domain})\n")

    for prov in args.providers:
        if prov not in PROVIDER_SPF:
            print(f"  !! unknown provider '{prov}' (known: {', '.join(PROVIDER_SPF)})")
            continue

        sub = f"{prov}.{domain}"
        selector = args.selector
        path = f"{args.key_dir}/dkim_{prov}_private.pem"

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

        print("=" * 72)
        print(f"  PROVIDER : {prov}")
        print(f"  subdomain: {sub}")
        print(f"  key file : {path}")
        print(f"""
1) DKIM  TXT
   Name :  {selector}._domainkey.{sub}
   Value:
   "v=DKIM1; k=rsa; p={p_value}"

2) SPF   TXT
   Name :  {sub}
   Value:
   v=spf1 include:{PROVIDER_SPF[prov]} ~all
""")

    print("=" * 72)
    print(f"""
Then send from e.g. hello@<provider>.{domain} - the provider you pick for a
given subdomain must be the one whose SPF include that subdomain lists. Keys
are per-provider; never reuse a subdomain/key across providers. ADDITIONALLY:
publish one DMARC record so receivers know how to treat failed
authentication:

   Name :  _dmarc.{domain}
   Value:
   v=DMARC1; p=none; rua=mailto:dmarc@{domain}
""")


if __name__ == "__main__":
    main()