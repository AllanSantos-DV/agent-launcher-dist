#!/usr/bin/env python3
"""Proteção em repouso de segredos gravados em disco via DPAPI (`CryptProtectData`/
`CryptUnprotectData`, escopo do usuário atual) — a mesma mecânica do
`ConvertFrom-SecureString` do PowerShell, aqui em `ctypes` para manter o núcleo só com a
biblioteca padrão.

O que isto protege e o que não protege, para ninguém confiar mais do que deve:
o blob só é decifrável pela conta Windows que o gerou, na máquina em que foi gerado
(a chave-mestra DPAPI vive no perfil e é derivada da credencial de logon). Então uma
cópia *deste arquivo* — backup, sincronização de perfil, coleta de diagnóstico, pacote
de suporte, upload de EDR, pendrive — deixa de carregar credencial utilizável. Não é
barreira contra código rodando *como o próprio usuário*: esse código chama
`CryptUnprotectData` igual a este módulo. Também não protege o valor em memória.

A garantia vale por arquivo, não pelo segredo: enquanto o mesmo valor existir em outro
arquivo desprotegido, quem copia a pasta pega a cópia mais fácil.

Formato do arquivo do cofre: o blob DPAPI cru é o arquivo inteiro (entropia nula, escopo do usuário), o mesmo
dos produtos que o compartilham (native-java, .NET). `write_file` grava assim. Na leitura vale também o texto com o
PREFIX seguido do base64 do mesmo blob, forma dos cofres antigos e a que cabe dentro de um JSON como o `secrets.json`;
`migrate_file` regrava um arquivo antigo como blob cru.
"""
import base64
import binascii
import os
from pathlib import Path

from agent_launcher.domain.files import atomic_bytes, file_lock

PREFIX = "dpapi:v1:"
_PREFIX_BYTES = PREFIX.encode("ascii")
RAW_BLOB_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")  # DWORD de formato + GUID do provedor padrão

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretProtectionError(Exception):
    """DPAPI indisponível ou recusou o blob (perfil diferente, máquina diferente,
    chave-mestra recriada). Mensagem já pronta para exibir."""


def _load_api():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_p = ctypes.POINTER(DataBlob)
        crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            fn = getattr(crypt32, name)
            fn.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, blob_p]
            fn.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        return ctypes, DataBlob, crypt32, kernel32
    except (ImportError, OSError, AttributeError):
        return None


_API = _load_api()


def available():
    return _API is not None


def _crypt(func_name, data):
    ctypes, DataBlob, crypt32, kernel32 = _API
    # `buf_in` precisa continuar referenciado até a chamada retornar: o blob aponta pra
    # dentro dele.
    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    ok = getattr(crypt32, func_name)(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretProtectionError(
            f"{func_name} falhou (erro {ctypes.get_last_error()}): o segredo protegido "
            "só pode ser lido pela mesma conta Windows e máquina que o gravou."
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def is_protected(stored):
    return isinstance(stored, str) and stored.startswith(PREFIX)


def text_to_blob(text):
    """Blob cru de um valor em texto `PREFIX` + base64; recusa prefixo ausente, base64 inválido e blob vazio."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise SecretProtectionError(f"O texto do cofre precisa começar por {PREFIX}.")
    try:
        blob = base64.b64decode(text[len(PREFIX):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretProtectionError(f"Segredo protegido com base64 inválido ({exc}).")
    if not blob:
        raise SecretProtectionError("O segredo protegido não traz nenhum blob DPAPI.")
    return blob


def blob_to_text(blob):
    """Valor em texto `PREFIX` + base64 de um blob cru; recusa blob vazio."""
    if not blob:
        raise SecretProtectionError("O blob DPAPI está vazio.")
    return PREFIX + base64.b64encode(bytes(blob)).decode("ascii")


def protect(plaintext):
    """Forma de armazenamento do segredo. Idempotente: um valor já protegido volta como
    está, pra regravar a config não cifrar duas vezes."""
    if not plaintext:
        return ""
    if is_protected(plaintext):
        return plaintext
    if not available():
        raise SecretProtectionError("DPAPI indisponível nesta plataforma; segredos só podem ser gravados no Windows.")
    stored = blob_to_text(_crypt("CryptProtectData", plaintext.encode("utf-8")))
    # Invariante: nunca devolver uma forma de armazenamento que não volte a ser o segredo
    # original — quem chama grava isto em lugar do texto puro.
    if unprotect(stored) != plaintext:
        raise SecretProtectionError("DPAPI não devolveu o mesmo valor ao decifrar o que acabou de cifrar.")
    return stored


def unprotect(stored):
    """Aceita tanto a forma protegida quanto texto puro: um valor legado em claro é
    formato válido de entrada, e migrá-lo é responsabilidade de quem grava."""
    if not stored:
        return ""
    if isinstance(stored, (bytes, bytearray)):
        return unprotect_raw(bytes(stored))
    if not is_protected(stored):
        return stored
    if not available():
        raise SecretProtectionError("O segredo está protegido por DPAPI, indisponível nesta plataforma.")
    return _crypt("CryptUnprotectData", text_to_blob(stored)).decode("utf-8", "replace")


def is_raw_blob(data):
    """Bytes que começam pelo cabeçalho fixo de um blob DPAPI."""
    return isinstance(data, (bytes, bytearray)) and bytes(data[:len(RAW_BLOB_HEADER)]) == RAW_BLOB_HEADER


def unprotect_raw(blob):
    """Decifra o blob DPAPI cru (sem prefixo nem base64), o formato de arquivo do native-java."""
    if not is_raw_blob(blob):
        raise SecretProtectionError("O conteúdo não é um blob DPAPI (cabeçalho de versão e provedor ausente).")
    if not available():
        raise SecretProtectionError("O segredo está protegido por DPAPI, indisponível nesta plataforma.")
    return _crypt("CryptUnprotectData", bytes(blob)).decode("utf-8", "replace")


def write_file(path, plaintext):
    """Grava o segredo como arquivo do cofre: o blob DPAPI cru, por troca atômica."""
    if not plaintext:
        raise SecretProtectionError("Não há segredo para gravar no cofre.")
    if is_protected(plaintext):
        raise SecretProtectionError("O segredo já está na forma de texto protegido; grave o valor em claro.")
    if not available():
        raise SecretProtectionError("DPAPI indisponível nesta plataforma; segredos só podem ser gravados no Windows.")
    blob = _crypt("CryptProtectData", plaintext.encode("utf-8"))
    if unprotect_raw(blob) != plaintext:
        raise SecretProtectionError("DPAPI não devolveu o mesmo valor ao decifrar o que acabou de cifrar.")
    atomic_bytes(Path(path), blob)


def migrate_file(path):
    """Regrava como blob cru um arquivo do cofre em texto `PREFIX` + base64 (o mesmo blob, sem decifrar).
    Devolve se regravou: blob cru, texto em claro de antes da proteção e arquivo ausente ficam como estão."""
    path = Path(path)
    with file_lock(path):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return False
        if is_raw_blob(data):
            return False
        try:
            text = data.decode("utf-8-sig").strip()
        except UnicodeDecodeError:
            raise SecretProtectionError(f"{path}: nem texto UTF-8 nem blob DPAPI.")
        if not is_protected(text):
            return False
        blob = text_to_blob(text)
        if not is_raw_blob(blob):
            raise SecretProtectionError(f"{path}: o texto protegido não traz um blob DPAPI.")
        atomic_bytes(path, blob)
        return True


def read_file(path):
    """Segredo de um arquivo do cofre em qualquer dos dois formatos: o blob cru que os produtos gravam ou o texto
    PREFIX + base64 dos cofres antigos. Texto sem prefixo volta como está (legado em claro)."""
    data = Path(path).read_bytes()
    if is_raw_blob(data):
        return unprotect_raw(data)
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise SecretProtectionError(f"{path}: nem texto UTF-8 nem blob DPAPI.")
    return unprotect(text)
