"""Create a self-signed certificate for the explicitly supplied server names."""

import argparse
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def create_certificate(directory: Path, names: list[str]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "server.crt", directory / "server.key"
    if cert_path.exists() or key_path.exists():
        raise ValueError("目标目录已有证书或私钥；请选择新目录，避免覆盖正在使用的证书。")
    alt_names = []
    for name in dict.fromkeys(["localhost", "127.0.0.1", "::1", *names]):
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt_names.append(x509.DNSName(name.encode("idna").decode("ascii")))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Venus server")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    import os
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    with cert_path.open("xb") as stream:
        stream.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", action="append", default=[], help="服务器 IP 或 DNS 名，可重复指定")
    parser.add_argument("--output", type=Path, default=Path(".venus/tls"))
    args = parser.parse_args()
    try:
        cert, key = create_certificate(args.output, args.name)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Certificate: {cert}\nPrivate key: {key}\n客户端仅导入 server.crt；私钥保留在服务器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
