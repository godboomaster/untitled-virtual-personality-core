"""Noise Protocol Framework (ревизия 34): Noise_<паттерн>_25519_ChaChaPoly_SHA256.

Ровно то, что нужно VPC Link, без общих обобщений: паттерны IK (обычное
подключение — телефон знает ключ ноутбука), IKpsk2 (сопряжение: плюс одноразовый
секрет из QR-кода), а XX и KK — чтобы сверяться с официальными тестовыми
векторами (scripts/fixtures/noise_vectors.json). Примитивы — из `cryptography`;
своих шифров здесь нет, только склейка по спецификации:
https://noiseprotocol.org/noise.html

Тот же протокол на стороне телефона — web/src/link/noise.ts; обе реализации
проверяются одними векторами и друг с другом (scripts/test_link.py).
"""

import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

DHLEN = 32
HASHLEN = 32
TAGLEN = 16
MAX_MESSAGE = 65535
MAX_PAYLOAD = MAX_MESSAGE - TAGLEN

# Паттерн → (предсообщения инициатора, предсообщения ответчика, сообщения)
_PATTERNS = {
    "IK": ((), ("s",), (("e", "es", "s", "ss"), ("e", "ee", "se"))),
    "XX": ((), (), (("e",), ("e", "ee", "s", "es"), ("s", "se"))),
    "KK": (("s",), ("s",), (("e", "es", "ss"), ("e", "ee", "se"))),
}


class NoiseError(Exception):
    """Рукопожатие или расшифровка не удались (чужой ключ, подмена, сбой)."""


def generate_private() -> bytes:
    return X25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def public_key(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _dh(private: bytes, public: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(private).exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as e:  # точка малого порядка — общий секрет из нулей
        raise NoiseError(f"DH: {e}") from e


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, n: int) -> list[bytes]:
    temp = _hmac(chaining_key, ikm)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = _hmac(temp, prev + bytes([i]))
        out.append(prev)
    return out


class CipherState:
    def __init__(self, key: bytes | None = None):
        self.k = key
        self.n = 0

    def _nonce(self) -> bytes:
        if self.n >= 2**64 - 1:
            raise NoiseError("исчерпан счётчик nonce")
        return b"\x00" * 4 + self.n.to_bytes(8, "little")

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self.k is None:
            return plaintext
        out = ChaCha20Poly1305(self.k).encrypt(self._nonce(), plaintext, ad)
        self.n += 1
        return out

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self.k is None:
            return ciphertext
        try:
            out = ChaCha20Poly1305(self.k).decrypt(self._nonce(), ciphertext, ad)
        except InvalidTag as e:
            raise NoiseError("неверная метка: подмена или чужой ключ") from e
        self.n += 1
        return out


class _SymmetricState:
    def __init__(self, protocol_name: bytes):
        self.h = protocol_name.ljust(HASHLEN, b"\x00") if len(protocol_name) <= HASHLEN \
            else hashlib.sha256(protocol_name).digest()
        self.ck = self.h
        self.cs = CipherState()

    def mix_key(self, ikm: bytes):
        self.ck, k = _hkdf(self.ck, ikm, 2)
        self.cs = CipherState(k)

    def mix_hash(self, data: bytes):
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key_and_hash(self, ikm: bytes):
        self.ck, h, k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(h)
        self.cs = CipherState(k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cs.encrypt(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cs.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


def _parse(pattern: str) -> tuple[str, list[int]]:
    # "IKpsk2" → ("IK", [2]); модификаторы psk — через «+»: "XXpsk0+psk3"
    base, psks = pattern, []
    if "psk" in pattern:
        base, rest = pattern.split("psk", 1)
        psks = [int(p.replace("psk", "")) for p in ("psk" + rest).split("+")]
    if base not in _PATTERNS:
        raise ValueError(f"паттерн {pattern} не поддерживается")
    return base, psks


class HandshakeState:
    """Одно рукопожатие. initiator — кто пишет первым (телефон).

    s — свой статический закрытый ключ, rs — известный заранее открытый ключ
    собеседника, psk — общий секрет (паттерны pskN). e — только для тестовых
    векторов (детерминированный эфемерный ключ)."""

    def __init__(self, pattern: str, initiator: bool, prologue: bytes = b"", *,
                 s: bytes | None = None, rs: bytes | None = None, psk: bytes | None = None,
                 e: bytes | None = None):
        base, psk_pos = _parse(pattern)
        pre_i, pre_r, messages = _PATTERNS[base]
        self.messages = [list(m) for m in messages]
        for p in psk_pos:
            if p == 0:
                self.messages[0].insert(0, "psk")
            else:
                self.messages[p - 1].append("psk")
        self.psk_mode = bool(psk_pos)
        # psk можно задать и позже (set_psk): ответчик при сопряжении узнаёт,
        # какой секрет брать, только прочитав первое сообщение
        self.psk = None
        if psk is not None:
            self.set_psk(psk)
        self.initiator = initiator
        self.s = s
        self.s_pub = public_key(s) if s else None
        self.e = e
        self.e_pub = public_key(e) if e else None
        self.rs = rs
        self.re: bytes | None = None
        self.ss = _SymmetricState(f"Noise_{pattern}_25519_ChaChaPoly_SHA256".encode())
        self.ss.mix_hash(prologue)
        # Предсообщения: ключи, известные заранее, входят в хеш рукопожатия
        for owner_is_initiator, pre in ((True, pre_i), (False, pre_r)):
            for token in pre:
                mine = owner_is_initiator == initiator
                key = self.s_pub if mine else self.rs
                if key is None:
                    raise ValueError("паттерн требует заранее известный статический ключ")
                self.ss.mix_hash(key)
        self.step = 0
        self.result: tuple[CipherState, CipherState] | None = None

    def set_psk(self, psk: bytes):
        if len(psk) != 32:
            raise ValueError("нужен psk из 32 байт")
        self.psk = bytes(psk)

    @property
    def finished(self) -> bool:
        return self.result is not None

    @property
    def handshake_hash(self) -> bytes:
        return self.ss.h

    def _my_turn(self) -> bool:
        return (self.step % 2 == 0) == self.initiator

    def _dh_token(self, token: str) -> bytes:
        if token == "ee":
            return _dh(self.e, self.re)
        if token == "ss":
            return _dh(self.s, self.rs)
        # es: e инициатора × s ответчика; se — наоборот
        init_side, resp_side = token
        if self.initiator:
            mine, theirs = (self.e, self.rs) if init_side == "e" else (self.s, self.re)
        else:
            mine, theirs = (self.s, self.re) if resp_side == "s" else (self.e, self.rs)
        return _dh(mine, theirs)

    def write_message(self, payload: bytes = b"") -> bytes:
        if self.finished or not self._my_turn():
            raise NoiseError("сейчас не очередь писать")
        out = bytearray()
        for token in self.messages[self.step]:
            if token == "e":
                if self.e is None:
                    self.e = generate_private()
                    self.e_pub = public_key(self.e)
                out += self.e_pub
                self.ss.mix_hash(self.e_pub)
                if self.psk_mode:
                    self.ss.mix_key(self.e_pub)
            elif token == "s":
                out += self.ss.encrypt_and_hash(self.s_pub)
            elif token == "psk":
                if self.psk is None:
                    raise NoiseError("psk не задан")
                self.ss.mix_key_and_hash(self.psk)
            else:
                self.ss.mix_key(self._dh_token(token))
        out += self.ss.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE:
            raise NoiseError("сообщение рукопожатия длиннее 65535 байт")
        self._advance()
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        if self.finished or self._my_turn():
            raise NoiseError("сейчас не очередь читать")
        if len(message) > MAX_MESSAGE:
            raise NoiseError("сообщение длиннее 65535 байт")
        pos = 0
        for token in self.messages[self.step]:
            if token == "e":
                if len(message) - pos < DHLEN:
                    raise NoiseError("короткое сообщение рукопожатия")
                self.re = bytes(message[pos:pos + DHLEN])
                pos += DHLEN
                self.ss.mix_hash(self.re)
                if self.psk_mode:
                    self.ss.mix_key(self.re)
            elif token == "s":
                n = DHLEN + (TAGLEN if self.ss.cs.k is not None else 0)
                if len(message) - pos < n:
                    raise NoiseError("короткое сообщение рукопожатия")
                self.rs = self.ss.decrypt_and_hash(bytes(message[pos:pos + n]))
                pos += n
            elif token == "psk":
                if self.psk is None:
                    raise NoiseError("psk не задан")
                self.ss.mix_key_and_hash(self.psk)
            else:
                self.ss.mix_key(self._dh_token(token))
        payload = self.ss.decrypt_and_hash(bytes(message[pos:]))
        self._advance()
        return payload

    def _advance(self):
        self.step += 1
        if self.step == len(self.messages):
            self.result = self.ss.split()

    def transport(self) -> "Transport":
        if self.result is None:
            raise NoiseError("рукопожатие не завершено")
        c1, c2 = self.result
        return Transport(send=c1 if self.initiator else c2, recv=c2 if self.initiator else c1,
                         handshake_hash=self.handshake_hash, remote_static=self.rs)


class Transport:
    """Канал после рукопожатия: свой шифр на каждое направление."""

    def __init__(self, send: CipherState, recv: CipherState, handshake_hash: bytes,
                 remote_static: bytes | None):
        self._send = send
        self._recv = recv
        self.handshake_hash = handshake_hash
        self.remote_static = remote_static

    def encrypt(self, plaintext: bytes) -> bytes:
        if len(plaintext) > MAX_PAYLOAD:
            raise NoiseError("кадр длиннее 65519 байт — делите на части")
        return self._send.encrypt(b"", plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) > MAX_MESSAGE:
            raise NoiseError("кадр длиннее 65535 байт")
        return self._recv.decrypt(b"", ciphertext)
