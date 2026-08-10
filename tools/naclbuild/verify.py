"""用 PyNaCl 解开浏览器端 SA_SEAL 产出的密文，确认与 GitHub 期望的 sealed box 完全兼容。"""
import base64
import json
import os
import sys

from nacl.public import PrivateKey, SealedBox

HERE = os.path.dirname(os.path.abspath(__file__))
key = json.load(open(os.path.join(HERE, "testkey.json")))
blob = json.load(open(os.path.join(HERE, "sealed.json"), encoding="utf-8"))

sk = PrivateKey(base64.b64decode(key["sk"]))
try:
    got = SealedBox(sk).decrypt(base64.b64decode(blob["sealed"])).decode("utf-8")
except Exception as e:
    print("FAIL  PyNaCl 解密失败：%r" % (e,))
    sys.exit(1)

want = blob["plaintext"]
if got == want:
    print("PASS  PyNaCl 成功解密浏览器端密文，明文完全一致")
    print("      明文预览：%s" % got[:80])
    sys.exit(0)

print("FAIL  明文不一致")
print("  got : %s" % got[:200])
print("  want: %s" % want[:200])
sys.exit(1)
