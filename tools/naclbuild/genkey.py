"""生成一对 X25519 密钥用于验证浏览器端 sealed box 实现是否与 PyNaCl 兼容。"""
import base64
import json
import os
import sys

try:
    from nacl.public import PrivateKey
except ImportError:
    sys.stderr.write("PyNaCl 未安装\n")
    raise SystemExit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
sk = PrivateKey.generate()
out = {
    "sk": base64.b64encode(bytes(sk)).decode(),
    "pk": base64.b64encode(bytes(sk.public_key)).decode(),
}
with open(os.path.join(HERE, "testkey.json"), "w") as f:
    json.dump(out, f)
print("pk:", out["pk"])
