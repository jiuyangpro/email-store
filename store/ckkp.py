import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def build_sign_content(data):
    items = []
    for key in sorted(data):
        value = data[key]
        if key in {"sign", "sign_type"}:
            continue
        if value is None or value == "":
            continue
        items.append(f"{key}={value}")
    return "&".join(items)


def sign_payload(data, private_key_path=None, sign_type="RSA", md5_key=""):
    sign_type = (sign_type or "RSA").upper()
    if sign_type == "MD5":
        sign_content = build_sign_content(data)
        return hashlib.md5(f"{sign_content}{md5_key}".encode("utf-8")).hexdigest()

    sign_content = build_sign_content(data).encode("utf-8")
    private_key = serialization.load_pem_private_key(
        Path(private_key_path).read_bytes(),
        password=None,
    )
    signature = private_key.sign(
        sign_content,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def verify_payload(data, public_key_path=None, sign_type="RSA", md5_key=""):
    signature = data.get("sign", "")
    if not signature:
        return False

    sign_type = (sign_type or "RSA").upper()
    if sign_type == "MD5":
        sign_content = build_sign_content(data)
        expected = hashlib.md5(f"{sign_content}{md5_key}".encode("utf-8")).hexdigest()
        return signature.lower() == expected.lower()

    sign_content = build_sign_content(data).encode("utf-8")
    public_key = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
    try:
        public_key.verify(
            base64.b64decode(signature),
            sign_content,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False
