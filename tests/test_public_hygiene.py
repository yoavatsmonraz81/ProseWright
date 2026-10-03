"""This repository is public: it must carry no private story material.

The deny list is kept privately; only salted hashes of its words and word
pairs live here, so the check reveals nothing about what it checks for. Every
text file is split into lowercase words; each word and each adjacent pair
(across line breaks too) is hashed and looked up. A hit names the file and
line, never the word.
"""

from __future__ import annotations

import bisect
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SALT = "story-editor-public-hygiene-v1"
DENY = {
    "045d93023af97781", "05a3380b06cca1a5", "060a25d180b23ab3", "09552314f0fec905",
    "0ef280ea8f7d1507", "101c78200c36920c", "103279c37e753902", "112756c266acb518",
    "158c9d1dc4d5da8a", "15ffc3100b5435f5", "1aff8ef13a6c0603", "1c45e5904cef8608",
    "1dc1024b29df5cad", "2309b92881e2fcdf", "2429bbe48fc888d0", "244faf72e7f3d215",
    "2482c302a5458253", "295bf96660cdae1a", "2a147cc9bdecab07", "3101d48a1fe97dd7",
    "328a92f213fdb501", "3326b07f8e189244", "332fc515fdbfa27e", "33d6d2fd20fe414c",
    "35f37a44d91f3e1d", "3869740aae006215", "3b4d44f1514cb8da", "3c2e228d4091f7da",
    "3cd752a7a3be38e6", "422dceba9571e8cf", "42b3592a49763e75", "452cc5ceabc1a69a",
    "47957fdbc6522379", "4906f6d2f56b6c26", "4afe6fe38be24bdd", "52d81ee080f54135",
    "542fb48ce79f4ff6", "558548a86b0324a7", "5650ce95fbe40bca", "57cb4382bdaf2b83",
    "58ee5829a165b547", "5b4661f8cf5be7b1", "5de4eeaab531c05d", "5e1361f49c21c883",
    "5fbb70fc7a59976c", "6011ee2112165ed5", "645c5565a83b372c", "691e1a6cf9d86370",
    "6ae0c811122906b5", "6b6bfd8a802f1b86", "6e226f0e6c5a68d1", "7072fd586454a82f",
    "714dc6ce27cedaed", "7189a9ffe88aa6c5", "7289ebea75e82147", "73a3daa53728e763",
    "73ae824c8d7b90ab", "7766ede7a5d474c9", "79c4d0b190627ce0", "7b9fdc119f60a570",
    "7c2ee12d6e890c4f", "7f4aa8b66fcffd97", "806112321e6f90f8", "83bcf8e4f6973437",
    "86633f7c60e94c9b", "8a2f7a95aed96c4d", "8a6e7ef64759e21f", "9124adcae3ec9e74",
    "933ec6b8156a2f66", "93e1a4ca26e8ab08", "93fdc0185fafa31f", "9699b00c3572910d",
    "96e0c563fb11eb0f", "97c6b26e64616eeb", "97d9e708cdf7257f", "98af8329a2e04e44",
    "9aa7f9f975b87c5a", "9dc5774f42473591", "9ef95dfba2078b5e", "a3145d8536db8cd3",
    "a6b8007e0e8c5f8b", "a74aafc533418142", "a7d44cb228311272", "a99d5a109bd626b5",
    "ab051db72c0e9057", "abf09fb1c2470d1c", "aed79cf7d64876a0", "b10cb4dea5a1fc18",
    "b168b524646c0a19", "b2b9e317d63b1155", "b3c03386a1635caa", "b65abf23e00b11f9",
    "b707265263c1fc60", "b9efbe69b006d61a", "baa9189c763b59e5", "c09a57e7d559e49c",
    "c537d84ce24cc074", "c7f1181f4d142ceb", "ccdd08adda14efd2", "cf5dc61ffe7d4003",
    "d1a4885a0b25a2ea", "d33904503ae20892", "d3490a45272e95a4", "d37016004f57deb2",
    "d40c966a354cd937", "d41e282f3addeac8", "d7664844a47bca24", "d9739d721c956a09",
    "df086c974f1db43f", "e03128f941adb062", "e370d2c7127a23e0", "e665efbc4dd9ec95",
    "e7df88d49f7a3a11", "e98bd8d56ff33c2d", "eaafd4941abed90b", "ed35398f17cbdc05",
    "ed925479a9503c4c", "ede3ddf4ed0e8df2", "ef3cc41f513f6987", "f3e4680be885988d",
    "fa14f508b57dbc03",
}
BINARY = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".ttf", ".otf", ".woff", ".woff2",
          ".sqlite3", ".pdf", ".zip", ".gz", ".pyc"}
_WORD = re.compile(r"[^\W_]+")


def _h(token: str) -> str:
    return hashlib.sha256((SALT + token).encode()).hexdigest()[:16]


def _files():
    """Exactly what a commit would carry: tracked files plus untracked ones
    that .gitignore doesn't exclude. Outside a git checkout, every file."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode()
        paths = [ROOT / rel for rel in out.split("\0") if rel]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in ROOT.rglob("*") if ".git" not in p.relative_to(ROOT).parts]
    for path in sorted(paths):
        if path.is_file() and path.suffix.lower() not in BINARY:
            yield path


def scan() -> list[str]:
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        # An escape in source ("\\nName") would glue its letter onto the next word;
        # two spaces keep every offset, so line numbers stay right.
        low = re.sub(r"\\[ntr]", "  ", text).lower()
        breaks = [i for i, ch in enumerate(low) if ch == "\n"]
        words = [(m.group(), bisect.bisect_left(breaks, m.start()) + 1) for m in _WORD.finditer(low)]
        bad_lines = {n for w, n in words if _h(w) in DENY}
        bad_lines |= {n for (a, n), (b, _) in zip(words, words[1:]) if _h(f"{a} {b}") in DENY}
        hits += [f"{path.relative_to(ROOT)}:{n}" for n in sorted(bad_lines)]
    return hits


def test_no_private_story_material():
    hits = scan()
    assert not hits, f"{len(hits)} lines carry private story material:\n" + "\n".join(hits[:60])


if __name__ == "__main__":
    import sys

    found = scan()
    for hit in found:
        print(hit)
    sys.exit(1 if found else 0)
