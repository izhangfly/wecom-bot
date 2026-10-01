"""WeCom (企业微信) callback crypto — URL verification + AES message codec.

Implements the standard WeCom scheme:
  signature = sha1(sorted(token, timestamp, nonce, encrypted))
  AESKey    = base64decode(EncodingAESKey + '=')  (32 bytes)
  plaintext = random(16) + msg_len(4, network order) + msg + receive_id
  AES-256-CBC, IV = key[:16], PKCS7 padding
"""
from __future__ import annotations

import base64
import hashlib
import struct
import time
import random
import string

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


class WeComCryptoError(Exception):
    pass


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise WeComCryptoError("EncodingAESKey must decode to 32 bytes")
        self.corp_id = corp_id

    # ------------------------------------------------------------------ sig
    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        raw = "".join(sorted([self.token, timestamp, nonce, encrypted]))
        return hashlib.sha1(raw.encode()).hexdigest()

    def check_signature(self, msg_signature, timestamp, nonce, encrypted):
        if self.signature(timestamp, nonce, encrypted) != msg_signature:
            raise WeComCryptoError("signature mismatch")

    # -------------------------------------------------------------- decrypt
    def decrypt(self, encrypted_b64: str) -> str:
        data = base64.b64decode(encrypted_b64)
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        plain = cipher.decrypt(data)
        pad = plain[-1]
        if not 1 <= pad <= 32:
            raise WeComCryptoError("bad padding")
        plain = plain[:-pad]
        content = plain[16:]
        (msg_len,) = struct.unpack(">I", content[:4])
        msg = content[4:4 + msg_len].decode("utf-8")
        receive_id = content[4 + msg_len:].decode("utf-8")
        if receive_id != self.corp_id:
            raise WeComCryptoError(f"receive_id mismatch: {receive_id!r}")
        return msg

    # -------------------------------------------------------------- encrypt
    def encrypt(self, msg: str) -> str:
        msg_b = msg.encode("utf-8")
        payload = (get_random_bytes(16) + struct.pack(">I", len(msg_b)) +
                   msg_b + self.corp_id.encode())
        pad = 32 - (len(payload) % 32)
        payload += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(payload)).decode()

    # -------------------------------------------- passive (encrypted) reply
    def build_reply(self, plain_xml: str) -> str:
        enc = self.encrypt(plain_xml)
        ts = str(int(time.time()))
        nonce = "".join(random.choices(string.digits, k=10))
        sig = self.signature(ts, nonce, enc)
        return (f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt>"
                f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
                f"<TimeStamp>{ts}</TimeStamp>"
                f"<Nonce><![CDATA[{nonce}]]></Nonce></xml>")


def selftest(token="", aes_key=None, corp_id=""):
    """Round-trip: encrypt -> signature -> verify -> decrypt."""
    if aes_key is None:
        aes_key = "".join(random.choices(string.ascii_letters + string.digits, k=43))
    c = WeComCrypto(token, aes_key, corp_id)
    msg = "hello 企业微信 ✓ round trip"
    enc = c.encrypt(msg)
    ts, nonce = str(int(time.time())), "123456"
    sig = c.signature(ts, nonce, enc)
    c.check_signature(sig, ts, nonce, enc)
    out = c.decrypt(enc)
    assert out == msg, (out, msg)
    return "SELFTEST OK"


if __name__ == "__main__":
    print(selftest())
