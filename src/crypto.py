"""微信公众号 消息加解密：WXBizMsgCrypt（AES-CBC）"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
from typing import Optional

from Crypto.Cipher import AES


class CryptError(Exception):
    pass


def msg_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


class WXMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, app_id: str):
        if len(encoding_aes_key) != 43:
            raise CryptError("EncodingAESKey 必须 43 位")
        self.token = token
        self.app_id = app_id
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise CryptError("AES key 长度异常")
        self.iv = self.key[:16]

    def decrypt(self, encrypt_b64: str, msg_sig: str, timestamp: str, nonce: str) -> str:
        if msg_signature(self.token, timestamp, nonce, encrypt_b64) != msg_sig:
            raise CryptError("msg_signature 校验失败")
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        # 去 PKCS#7 padding
        pad = plain[-1]
        if pad < 1 or pad > 32:
            raise CryptError("padding 异常")
        plain = plain[:-pad]
        # 结构: random(16) | msg_len(4 BE) | msg | receive_id
        if len(plain) < 20:
            raise CryptError("明文过短")
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len].decode("utf-8")
        receive_id = plain[20 + msg_len:].decode("utf-8")
        if receive_id != self.app_id:
            raise CryptError(f"receive_id 不匹配: {receive_id}")
        return msg

    def encrypt(self, reply_xml: str, timestamp: Optional[str] = None, nonce: Optional[str] = None) -> tuple[str, str, str, str]:
        msg = reply_xml.encode("utf-8")
        plain = os.urandom(16) + struct.pack(">I", len(msg)) + msg + self.app_id.encode("utf-8")
        pad_len = 32 - (len(plain) % 32) or 32
        plain += bytes([pad_len]) * pad_len
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        encrypt_b64 = base64.b64encode(cipher.encrypt(plain)).decode("ascii")
        ts = timestamp or str(int(time.time()))
        nc = nonce or os.urandom(8).hex()
        sig = msg_signature(self.token, ts, nc, encrypt_b64)
        envelope = (
            f"<xml>"
            f"<Encrypt><![CDATA[{encrypt_b64}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{sig}]]></MsgSignature>"
            f"<TimeStamp>{ts}</TimeStamp>"
            f"<Nonce><![CDATA[{nc}]]></Nonce>"
            f"</xml>"
        )
        return envelope, encrypt_b64, ts, nc
