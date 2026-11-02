# -*- coding: utf-8 -*-
"""edopro_replay2json — replays de Yu-Gi-Oh! EDOPro (.yrp / .yrpX) → JSON.

Módulo ÚNICO y autocontenido: **solo stdlib** (struct, lzma, sqlite3, urllib,
json, dataclasses). No depende de pywindbot ni de nada externo.

ES UN PORT de la implementación PROBADA de PyWindBot (mismo autor, Jose):
``pywindbot/replay.py`` (headers yrp/yrpX, LZMA con padding, packets 163/231,
nombres UTF-16LE, decks, respuestas), ``pywindbot/parser.py`` (decoder de
mensajes del core v11) y ``pywindbot/net/tracker.py`` (espejo de campo),
reducidos aquí a un solo archivo de distribución pública.

ESPECIFICACIÓN (fuente C++ oficial, leída — no copiada):

* edo9300/EDOPro ``gframe/replay.h`` + ``replay.cpp`` (master, 2026-10):
    - ReplayHeader (32 bytes): id u32 ('yrp1'=0x31707279 o 'yrpX'=0x58707279),
      version u32, flag u32, timestamp u32, datasize u32, hash u32, props[8].
    - ExtendedReplayHeader (72 bytes): base(32) + header_version u64 +
      seed[4] u64. Presente si flag & REPLAY_EXTENDED_HEADER (0x200).
    - Flags: 0x1 COMPRESSED (LZMA1 raw), 0x2 TAG, 0x8 SINGLE_MODE, 0x10 LUA64,
      0x20 NEWREPLAY, 0x40 HAND_TEST, 0x100 64BIT_DUELFLAG, 0x200 EXTENDED_HEADER.
    - Payload tras el header: nombres UTF-16LE de 40 bytes (20 u16 terminados
      en NUL) con conteos u32 si NEWREPLAY; params lp/hand/draw (YRP1) y
      duel_flags u32/u64; decks [u32 n][n×u32] main+extra por jugador y reglas
      custom; respuestas [u8 len][bytes] hasta len==0. En YRPX el resto es un
      stream de packets [u8 mensaje][u32 len][payload] donde 163 = MSG_AI_NAME
      (nombre rival UTF-8), 40 = MSG_NEW_TURN y 231 = OLD_REPLAY_MODE (un
      .yrp clásico serializado embebido); el resto son mensajes del core v11.
* Compresión: LZMA1 RAW con props de 5 bytes del header (byte 0 = lc/lp/pb,
  bytes 1-4 = dict size LE). Confirmado por DyXel/edopro-replay-parser
  ``src/decompress.cpp`` ("fake header" .lzma con las mismas props).
  Los replays grabados en vivo (_LastReplay.*) van SIN comprimir.
  Particularidad de EDOPro: la salida LZMA puede exceder ``datasize`` en unos
  bytes de padding del compresor; el core hace ``resize(datasize)`` de fábrica,
  así que aquí se recorta a datasize y se rechaza si sale MENOS de datasize.

Uso rápido (API):

    from edopro_replay2json import leer_replay, eventos, timeline, analizar, a_json
    rep = leer_replay("duelo.yrpX")          # o .yrp, o el par combinado
    ev = eventos(rep)                        # mensajes del core -> dicts
    tl = timeline(ev, rep.params, rep.decks, rep.jugadores)
    datos = a_json(rep, incluir_analisis=True)   # listo para json.dump

CLI (entry point instalado por pip):

    edopro-replay2json duelo.yrpX -o salida.json --analisis
    edopro-replay2json duelo.yrpX --cdb cards.cdb       # nombres offline
    edopro-replay2json duelo.yrpX --online              # nombres via API pública

Los nombres de cartas son SIEMPRE opcionales: por defecto solo se emiten
códigos (offline, sin DB ni red).
"""
from __future__ import annotations

import json
import lzma
import sqlite3
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"
__all__ = [
    "Replay", "ReplayError", "leer_replay", "eventos", "timeline", "analizar",
    "a_json", "nombres_desde_cdb", "nombres_online", "NEGADORES_CONOCIDOS",
    "main",
]

# --------------------------------------------------------------------------- #
#  Constantes del formato replay (spec EDOPro)                                 #
# --------------------------------------------------------------------------- #

REPLAY_YRP1 = 0x31707279            # 'yrp1' en little-endian
REPLAY_YRPX = 0x58707279            # 'yrpX' en little-endian
REPLAY_COMPRESSED = 0x1             # cuerpo LZMA1 raw (props en header)
REPLAY_TAG = 0x2                    # tag duel: 2 jugadores por lado
REPLAY_DECODED = 0x4
REPLAY_SINGLE_MODE = 0x8            # puzzle / single mode
REPLAY_LUA64 = 0x10                 # scripts Lua de 64 bits
REPLAY_NEWREPLAY = 0x20             # conteos u32 de nombres + reglas custom
REPLAY_HAND_TEST = 0x40             # hand test (sin decks reales)
REPLAY_DIRECT_SEED = 0x80
REPLAY_64BIT_DUELFLAG = 0x100       # duel_flags de 64 bits
REPLAY_EXTENDED_HEADER = 0x200      # ExtendedReplayHeader de 72 bytes

MSG_AI_NAME = 163                   # packet del stream yrpX: nombre del rival (ocgapi_constants.h)
OLD_REPLAY_MODE = 231               # packet del stream yrpX: .yrp clásico embebido

FLAG_NAMES = [
    ("COMPRESSED", REPLAY_COMPRESSED), ("TAG", REPLAY_TAG),
    ("DECODED", REPLAY_DECODED), ("SINGLE_MODE", REPLAY_SINGLE_MODE),
    ("LUA64", REPLAY_LUA64), ("NEWREPLAY", REPLAY_NEWREPLAY),
    ("HAND_TEST", REPLAY_HAND_TEST), ("DIRECT_SEED", REPLAY_DIRECT_SEED),
    ("64BIT_DUELFLAG", REPLAY_64BIT_DUELFLAG),
    ("EXTENDED_HEADER", REPLAY_EXTENDED_HEADER),
]

# ---- ubicaciones / posiciones / fases (core v11) --------------------------- #
LOCATION_DECK = 0x01
LOCATION_HAND = 0x02
LOCATION_MZONE = 0x04
LOCATION_SZONE = 0x08
LOCATION_GRAVE = 0x10
LOCATION_REMOVED = 0x20
LOCATION_EXTRA = 0x40
LOCATION_OVERLAY = 0x80

POS_FACEUP_ATTACK = 0x1
POS_FACEDOWN_ATTACK = 0x2
POS_FACEUP_DEFENSE = 0x4
POS_FACEDOWN_DEFENSE = 0x8

PHASE_NAMES = {
    0x01: "Draw", 0x02: "Standby", 0x04: "Main 1", 0x08: "Inicio de Batalla",
    0x10: "Battle Step", 0x20: "Damage Step", 0x40: "Cálculo de daño",
    0x80: "Batalla", 0x100: "Main 2", 0x200: "End",
}

WIN_REASONS = {
    0: "rendición", 1: "LP llegaron a 0", 2: "deck out",
    3: "experiencia", 4: "return", 5: "victoria por juego",
    6: "empate directo", 7: "empate por deck out", 8: "empate por efecto",
}

MSG_NAMES = {
    1: "RETRY", 2: "HINT", 3: "WAITING", 4: "START", 5: "WIN",
    10: "SELECT_BATTLECMD", 11: "SELECT_IDLECMD", 12: "SELECT_EFFECTYN",
    13: "SELECT_YESNO", 14: "SELECT_OPTION", 15: "SELECT_CARD",
    16: "SELECT_CHAIN", 18: "SELECT_PLACE", 19: "SELECT_POSITION",
    20: "SELECT_TRIBUTE", 21: "SORT_CHAIN", 22: "SELECT_COUNTER",
    23: "SELECT_SUM", 24: "SELECT_DISFIELD", 25: "SORT_CARD",
    26: "SELECT_UNSELECT_CARD", 30: "CONFIRM_DECKTOP", 31: "CONFIRM_CARDS",
    32: "SHUFFLE_DECK", 33: "SHUFFLE_HAND", 38: "DECK_TOP", 39: "SHUFFLE_EXTRA",
    40: "NEW_TURN", 41: "NEW_PHASE", 50: "MOVE", 53: "POS_CHANGE", 54: "SET",
    60: "SUMMONING", 61: "SUMMONED", 62: "SPSUMMONING", 63: "SPSUMMONED",
    64: "FLIPSUMMONING", 65: "FLIPSUMMONED", 70: "CHAINING", 71: "CHAINED",
    72: "CHAIN_SOLVING", 73: "CHAIN_SOLVED", 74: "CHAIN_END",
    75: "CHAIN_NEGATED", 76: "CHAIN_DISABLED", 83: "BECOME_TARGET",
    90: "DRAW", 91: "DAMAGE", 92: "RECOVER", 94: "LPUPDATE",
    100: "PAY_LPCOST", 101: "ADD_COUNTER", 102: "REMOVE_COUNTER",
    110: "ATTACK", 111: "BATTLE", 112: "ATTACK_DISABLED",
    113: "DAMAGE_STEP_START", 114: "DAMAGE_STEP_END", 120: "MISSED_EFFECT",
    130: "TOSS_COIN", 131: "TOSS_DICE", 132: "ROCK_PAPER_SCISSORS",
    133: "HAND_RES", 140: "ANNOUNCE_RACE", 141: "ANNOUNCE_ATTRIB",
    142: "ANNOUNCE_CARD", 143: "ANNOUNCE_NUMBER",
}


class ReplayError(Exception):
    """Error de formato/corrección de un replay (o del stream de mensajes)."""


def flags_legibles(flag):
    """Lista de nombres de flags activos a partir del entero del header."""
    return [n for n, b in FLAG_NAMES if flag & b]


# --------------------------------------------------------------------------- #
#  Lectura del archivo: header, LZMA, nombres, params, decks, packets          #
#  (port de pywindbot/replay.py)                                               #
# --------------------------------------------------------------------------- #

class _Lector:
    """Cursor little-endian sobre un buffer (equivalente a Read<T> de replay.cpp)."""

    __slots__ = ("buf", "off")

    def __init__(self, buf):
        self.buf = buf
        self.off = 0

    def take(self, n):
        if self.off + n > len(self.buf):
            raise ReplayError(
                f"buffer truncado: quiero {n} bytes en offset {self.off}, "
                f"hay {len(self.buf) - self.off}")
        b = bytes(self.buf[self.off:self.off + n])
        self.off += n
        return b

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def nombre_utf16(self):
        """ReadName(): 40 bytes = 20×u16 UTF-16LE terminados en NUL."""
        raw = self.take(40)
        return raw.decode("utf-16-le", "replace").split("\x00")[0]


def _parsear_header(buf):
    """ExtendedReplayHeader::ParseReplayHeader: base de 32 bytes + extensión.

    Lanza ReplayError si el archivo es menor que el header o el magic no es
    'yrp1'/'yrpX' (las pruebas de magic viven aquí).
    """
    if len(buf) < 32:
        raise ReplayError(
            f"archivo de {len(buf)} bytes: menor que un header de replay (32)")
    lec = _Lector(buf)
    h = {
        "id": lec.u32(),
        "version": lec.u32(),
        "flag": lec.u32(),
        "timestamp": lec.u32(),
        "datasize": lec.u32(),
        "hash": lec.u32(),
        "props": lec.take(8),
        "header_version": None,
        "seeds": None,
        "len_header": 32,
    }
    if h["id"] not in (REPLAY_YRP1, REPLAY_YRPX):
        raise ReplayError(
            f"magic desconocido: {h['id']:#010x} (no es yrp1/yrpX)")
    if h["flag"] & REPLAY_EXTENDED_HEADER:
        if len(buf) < 72:
            raise ReplayError("header extendido truncado (quiero 72 bytes)")
        h["header_version"] = lec.u64()
        h["seeds"] = list(struct.unpack("<4Q", lec.take(32)))
        h["len_header"] = 72
    return h


def _descomprimir_lzma(props5, data, datasize):
    """LzmaUncompress del core: LZMA1 con las props de 5 bytes del header.

    Como ``decompress.cpp`` de DyXel, se antepone un "fake header" .lzma
    (props 5 bytes + tamaño u64 desconocido 0xFF×8), con lo que el decoder
    acepta streams SIN end-marker (LzmaCompress del SDK, replays guardados)
    y CON end-marker (variantes de terceros).

    Padding de EDOPro (verificado contra replays reales): la salida puede ser
    MAYOR que datasize (el header declara p. ej. 555631 y el stream decodifica
    555634; el framing de packets termina exacto en datasize y el resto es
    relleno). El core resuelve de fábrica con ``replay_data.resize(datasize)``,
    así que aquí: si len(out) >= datasize se recorta a datasize; si
    len(out) < datasize el stream está truncado/corrupto y se RECHAZA.
    """
    fake = bytes(props5) + b"\xff" * 8
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    try:
        out = dec.decompress(fake + bytes(data))
    except lzma.LZMAError:
        # último recurso: RAW con filtros explícitos (props[0]=(pb*5+lp)*9+lc)
        p = props5[0]
        lc, lp, pb = p % 9, (p // 9) % 5, p // 45
        dict_size = struct.unpack_from("<I", props5, 1)[0]
        filtros = [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
                    "dict_size": dict_size}]
        dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filtros)
        out = dec.decompress(bytes(data))
    if datasize and len(out) < datasize:
        raise ReplayError(
            f"LZMA: el header declara {datasize} bytes pero el stream solo "
            f"decodificó {len(out)} (replay truncado o corrupto)")
    if datasize and len(out) > datasize:
        out = out[:datasize]    # padding del compresor: el core no la consume
    return bytearray(out)


def _cuerpo(buf, h):
    """Bytes tras el header, descomprimidos si flag & REPLAY_COMPRESSED."""
    resto = buf[h["len_header"]:]
    if h["flag"] & REPLAY_COMPRESSED:
        return _descomprimir_lzma(h["props"][:5], resto, h["datasize"])
    return bytearray(resto)


def _leer_nombres(lec, flag):
    """ParseNames(): jugadores home y oponente (tag = 2 por lado)."""
    nombres = []
    if flag & REPLAY_SINGLE_MODE:
        nombres.append(lec.nombre_utf16())
        nombres.append(lec.nombre_utf16())
        return {"home": nombres[:1], "rival": nombres[1:], "todos": nombres}, lec.off
    conteos = []
    for _ in range(2):
        if flag & REPLAY_NEWREPLAY:
            n = lec.u32()
        elif flag & REPLAY_TAG:
            n = 2
        else:
            n = 1
        conteos.append(n)
        for _ in range(n):
            nombres.append(lec.nombre_utf16())
    home = nombres[:conteos[0]]
    return {"home": home, "rival": nombres[conteos[0]:], "todos": nombres}, lec.off


def _leer_cabecera_comun(lec, h):
    """ParseNames() + ParseParams(): comunes a YRP1 y YRPX (replay.cpp llama
    a ambos en OpenReplayFromBuffer para los dos formatos)."""
    nombres, _ = _leer_nombres(lec, h["flag"])
    params = {}
    if h["id"] == REPLAY_YRP1:
        params["start_lp"] = lec.u32()
        params["start_hand"] = lec.u32()
        params["draw_count"] = lec.u32()
    else:
        # el header yrpX NO trae lp/hand/draw: llegan del MSG_START o del
        # clásico embebido (packet 231) / hermano .yrp en disco.
        params["start_lp"] = params["start_hand"] = params["draw_count"] = None
    if h["flag"] & REPLAY_64BIT_DUELFLAG:
        params["duel_flags"] = lec.u64()
    else:
        params["duel_flags"] = lec.u32()
    scriptname = None
    if h["flag"] & REPLAY_SINGLE_MODE and h["id"] == REPLAY_YRP1:
        slen = lec.u16()
        scriptname = lec.take(slen).decode("utf-8", "replace")
    return nombres, params, scriptname


def _parsear_clasico(data, h):
    """ParseParams + ParseDecks (+ ParseResponses) del formato YRP1 clásico."""
    lec = _Lector(data)
    nombres, params, scriptname = _leer_cabecera_comun(lec, h)
    decks = []
    reglas = []
    if not (h["flag"] & REPLAY_SINGLE_MODE and not h["flag"] & REPLAY_HAND_TEST):
        for _ in range(len(nombres["todos"])):
            nm = lec.u32()
            main = list(struct.unpack(f"<{nm}I", lec.take(4 * nm)))
            ne = lec.u32()
            extra = list(struct.unpack(f"<{ne}I", lec.take(4 * ne)))
            decks.append({"main": main, "extra": extra})
        if h["flag"] & REPLAY_NEWREPLAY and not h["flag"] & REPLAY_HAND_TEST:
            nr = lec.u32()
            reglas = list(struct.unpack(f"<{nr}I", lec.take(4 * nr)))
    respuestas = []
    try:
        while lec.off < len(data):
            ln = lec.u8()
            if ln == 0:
                break
            respuestas.append(lec.take(ln))
    except ReplayError:
        pass  # cola de respuestas truncada: se conserva lo leído
    return {
        "nombres": nombres,
        "params": params,
        "scriptname": scriptname,
        "decks": decks,
        "reglas_custom": reglas,
        "respuestas": respuestas,
        "bytes_leidos": lec.off,
        "bytes_totales": len(data),
    }


def _packets_yrpx(data, h):
    """ParseStream(): tras nombres+params, packets [u8 mensaje][u32 len][payload].

    ``data`` es el CUERPO sin header (lo produce _cuerpo). Devuelve
    (packets_core, extras, nombres, params, offset_final); extras incluye el
    nombre de IA (packet 163) y el clásico embebido (packet 231, que aquí solo
    se guarda crudo: se parsea fuera para poder reportar el error sin morir).
    """
    lec = _Lector(data)
    nombres, params, _ = _leer_cabecera_comun(lec, h)
    packets = []
    extras = {"ai_name": None, "embedded": None, "turnos_contados": 0}
    while lec.off + 5 <= len(data):
        pid = lec.u8()
        ln = lec.u32()
        payload = lec.take(ln)
        if pid == MSG_AI_NAME and len(payload) >= 2:
            nlen = struct.unpack_from("<H", payload)[0]
            extras["ai_name"] = payload[2:2 + nlen].decode("utf-8", "replace")
            continue
        if pid == 40:  # MSG_NEW_TURN: el core lo cuenta como turno
            extras["turnos_contados"] += 1
        if pid == OLD_REPLAY_MODE:
            extras["embedded"] = payload   # bytes crudos del .yrp embebido
            continue
        packets.append((pid, payload))
    return packets, extras, nombres, params, lec.off


def _fecha(ts):
    """timestamp u32 del header → ISO 8601 UTC (None si es 0/vacío)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  Replay: dataclass pública + leer_replay (incluye el par .yrp + .yrpX)       #
# --------------------------------------------------------------------------- #

@dataclass
class Replay:
    """Replay de EDOPro ya parseado (yrp clásico, yrpX o el par combinado).

    Campos públicos:
      formato       'yrp' | 'yrpX'
      version       u32 de versión del header
      flag          entero de flags; flags_activos nombres legibles
      timestamp     u32 unix del header; fecha ISO 8601 UTC derivada
      hash          u32 del header
      seeds         [4×u64] si el header es extendido, si no None
      params        {start_lp, start_hand, draw_count, duel_flags}
      jugadores     [nombre equipo 0, nombre equipo 1, ...]
      decks         [{main:[codes], extra:[codes]} × jugadores] (puede ser [])
      stream        bytes descomprimidos del cuerpo (yrpX); None en .yrp
      respuestas    cola de respuestas del clásico (bytes, se emiten como hex)
      reglas_custom reglas custom del clásico (NEWREPLAY)
      ai_name       nombre del rival vía packet 163 (yrpX), si vino
    Campos privados: packets del stream yrpX y el clásico embebido/hermano.
    """
    formato: str
    version: int
    flag: int
    flags_activos: list
    timestamp: int
    fecha: object = None            # str | None (evita tipo parametrizado 3.9+)
    hash: int = 0
    seeds: object = None            # list[int] | None
    params: dict = field(default_factory=dict)
    jugadores: list = field(default_factory=list)
    decks: list = field(default_factory=list)
    stream: object = None           # bytes | None
    respuestas: list = field(default_factory=list)
    reglas_custom: list = field(default_factory=list)
    ai_name: object = None          # str | None
    datasize: int = 0
    header_version: object = None   # int | None
    comprimido: bool = False
    ruta: object = None             # str | None

    # ---- internos (no forman parte del contrato público) ----
    _packets: list = field(default_factory=list, repr=False, compare=False)
    _clasico: dict = field(default=None, repr=False, compare=False)
    _turnos_contados: int = field(default=0, repr=False, compare=False)
    _bytes_stream: int = field(default=0, repr=False, compare=False)
    _cache_eventos: object = field(default=None, repr=False, compare=False)

    # ---- conveniencias que reusan las funciones globales ----
    def eventos(self):
        return eventos(self)

    def timeline(self, nombres=None):
        return timeline(self.eventos(), self.params, self.decks,
                        self.jugadores, nombres)

    def analizar(self, nombres=None):
        return analizar(self.eventos(), self.timeline(nombres), self.decks,
                        params=self.params, jugadores=self.jugadores,
                        nombres=nombres)

    def a_json(self, incluir_analisis=False, nombres=None):
        return a_json(self, incluir_analisis=incluir_analisis, nombres=nombres)


def _parsear_buffer(buf):
    """Parsea UN buffer completo de replay (header + cuerpo) a un dict.

    Se usa para el `.yrp` clásico embebido (packet 231 del yrpX) y para el
    hermano `.yrp` en disco. Devuelve {formato, nombres, params, decks,
    reglas_custom, respuestas}; lanza ReplayError si el magic/estructura es
    inválida (el llamador decide ignorar el error).
    """
    h = _parsear_header(buf)
    data = _cuerpo(buf, h)
    base = {
        "formato": "yrp" if h["id"] == REPLAY_YRP1 else "yrpX",
        "meta": {"version": h["version"], "flag": h["flag"],
                 "timestamp": h["timestamp"], "hash": h["hash"]},
    }
    if h["id"] == REPLAY_YRP1:
        datos = _parsear_clasico(data, h)
        base["nombres"] = datos["nombres"]
        base["params"] = datos["params"]
        base["decks"] = datos["decks"]
        base["reglas_custom"] = datos["reglas_custom"]
        base["respuestas"] = datos["respuestas"]
    else:   # un yrpX embebido no aporta decks; se conserva lo esencial
        packets, extras, nombres, params, _ = _packets_yrpx(data, h)
        base["nombres"] = nombres
        base["params"] = params
        base["decks"] = []
        base["reglas_custom"] = []
        base["respuestas"] = []
    return base


def _clasico_de_par(rep, ruta_yrpx, embedded_payload):
    """Resuelve el clásico efectivo para un yrpX: el packet 231 embebido o el
    hermano .yrp en disco (GetPlayerDecks de replay.cpp usa el embebido para
    decks/jugadores; el hermano cumple el mismo papel si no lo trajo)."""
    if embedded_payload is not None:
        try:
            emb = _parsear_buffer(embedded_payload)
            if emb.get("formato") == "yrp":
                return emb
        except ReplayError:
            pass  # embebido corrupto: se cae al hermano
    hermano = Path(str(ruta_yrpx)).with_suffix(".yrp")   # x.yrpX -> x.yrp
    if hermano.exists():
        try:
            buf = hermano.read_bytes()
            emb = _parsear_buffer(bytearray(buf))
            if emb.get("formato") == "yrp":
                return emb
        except (ReplayError, OSError):
            pass
    return None


def leer_replay(ruta):
    """Abre un replay de EDOPro (.yrp clásico, .yrpX extendido o el par).

    * ``.yrp``  → decks (códigos) + respuestas; sin stream de eventos.
    * ``.yrpX`` → stream de packets del core; si el extendido no trae decks
      (sin packet 231 embebido) se busca el hermano ``.yrp`` en disco y se
      COMBINAN (decks/params/jugadores del clásico, eventos del extendido).

    Devuelve :class:`Replay`; lanza ReplayError con magic/estructura inválida.
    """
    ruta = Path(ruta)
    buf = ruta.read_bytes()
    h = _parsear_header(buf)
    cuerpo = _cuerpo(buf, h)
    comprimido = bool(h["flag"] & REPLAY_COMPRESSED)

    rep = Replay(
        formato="yrp" if h["id"] == REPLAY_YRP1 else "yrpX",
        version=h["version"],
        flag=h["flag"],
        flags_activos=flags_legibles(h["flag"]),
        timestamp=h["timestamp"],
        fecha=_fecha(h["timestamp"]),
        hash=h["hash"],
        seeds=h["seeds"],
        datasize=h["datasize"],
        header_version=h["header_version"],
        comprimido=comprimido,
        ruta=str(ruta),
    )

    if rep.formato == "yrp":
        datos = _parsear_clasico(cuerpo, h)
        rep.params = datos["params"]
        rep.jugadores = list(datos["nombres"]["todos"])
        rep.decks = datos["decks"]
        rep.reglas_custom = datos["reglas_custom"]
        rep.respuestas = datos["respuestas"]
        rep.stream = None
        return rep

    # ---- yrpX: packets del core + clásico embebido/hermano ----
    packets, extras, nombres, params, off = _packets_yrpx(cuerpo, h)
    rep._packets = packets
    rep._turnos_contados = extras["turnos_contados"]
    rep._bytes_stream = off
    rep.stream = bytes(cuerpo)
    rep.ai_name = extras["ai_name"]
    rep.params = params
    rep.jugadores = list(nombres["todos"])

    clasico = _clasico_de_par(rep, ruta, extras["embedded"])
    if clasico is not None:
        rep._clasico = clasico
        # decks/respuestas/reglas SOLO existen en el clásico
        rep.decks = clasico.get("decks", [])
        rep.reglas_custom = clasico.get("reglas_custom", [])
        rep.respuestas = clasico.get("respuestas", [])
        # nombres y lp/hand/draw preferentes del clásico si los trae
        if clasico.get("nombres", {}).get("todos"):
            rep.jugadores = list(clasico["nombres"]["todos"])
        cp = clasico.get("params", {}) or {}
        if cp.get("start_lp") is not None:
            rep.params = {
                "start_lp": cp.get("start_lp"),
                "start_hand": cp.get("start_hand"),
                "draw_count": cp.get("draw_count"),
                "duel_flags": cp.get("duel_flags", params.get("duel_flags")),
            }
    # MSG_AI_NAME (163) sobreescribe players[1] (replay.cpp ParseStream)
    if rep.ai_name is not None and len(rep.jugadores) >= 2:
        rep.jugadores = list(rep.jugadores)
        rep.jugadores[1] = rep.ai_name
    return rep


# --------------------------------------------------------------------------- #
#  Decoder COMPACTO de mensajes del core v11                                   #
#  (port de pywindbot/parser.py; layout tomado del fuente del core)            #
# --------------------------------------------------------------------------- #
# Nota de nomenclatura: los ids 13/14/15/16 del core son SELECT_YESNO /
# SELECT_OPTION / SELECT_CARD / SELECT_CHAIN (playerop.cpp); aquí se decodifican
# TODOS, con el nombre canónico, para que el framing nunca se rompa.

class _MsgReader:
    """Cursor del stream de mensajes: [u32 tamaño][u8 id][payload] × N."""

    __slots__ = ("buf", "off")

    def __init__(self, buf, off=0):
        self.buf = buf
        self.off = off

    def take(self, n):
        if self.off + n > len(self.buf):
            raise ReplayError(
                f"stream truncado: quiero {n} bytes en offset {self.off}, "
                f"hay {len(self.buf) - self.off}")
        b = self.buf[self.off:self.off + n]
        self.off += n
        return b

    def u8(self):
        return self.take(1)[0]

    def i8(self):
        return struct.unpack("<b", self.take(1))[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def loc(self):
        """loc_info completo: con u8, loc u8, seq u32, pos u32."""
        return {"con": self.u8(), "loc": self.u8(), "seq": self.u32(),
                "pos": self.u32()}

    def loc_ns(self):
        """loc_info sin pos (listas de idlecmd/confirm): con, loc, seq."""
        return {"con": self.u8(), "loc": self.u8(), "seq": self.u32(), "pos": 0}

    def loc_s8(self):
        """repos/attacks: con u8, loc u8, seq u8."""
        return {"con": self.u8(), "loc": self.u8(), "seq": self.u8(), "pos": 0}

    def done(self):
        return self.off >= len(self.buf)


def _cartas(r, n):
    """n × (u32 code + loc_info): p. ej. SELECT_CARD."""
    out = []
    for _ in range(n):
        code = r.u32()
        inf = r.loc()
        inf["code"] = code
        out.append(inf)
    return out


def _parsear_mensaje(mid, r):
    """Decodifica UN mensaje del core a un dict compacto.

    Layouts extraídos del fuente del core v11 (playerop.cpp / processor.cpp /
    operations.cpp / field.cpp / duel.cpp) y portados de pywindbot/parser.py.
    Un mensaje desconocido devuelve {"id": mid, "skip": True} SIN consumir el
    frame completo: el llamador salta el frame entero y el framing se conserva.
    """
    m = {"id": mid}
    if mid == 1:            # RETRY: sin payload
        pass
    elif mid == 2:          # HINT
        m["type"] = r.u8(); m["player"] = r.u8(); m["value"] = r.u64()
    elif mid == 3:          # WAITING: sin payload
        pass
    elif mid == 4:          # MSG_START (lo genera el servidor en red; en el
        # replay puede venir del core). DOS variantes: EDOPro real = 17 bytes
        # [u8 equipo][i32 lp0][i32 lp1][u16 deck0][u16 extra0][u16 deck1][u16 extra1];
        # legacy = 18 bytes con [u8 duel_rule] detrás del byte de equipo.
        disp = len(r.buf) - r.off
        m["player"] = r.u8()
        m["duel_rule"] = r.u8() if disp >= 18 else None
        m["lp0"] = r.i32(); m["lp1"] = r.i32()
        m["deck0"] = r.u16(); m["extra0"] = r.u16()
        m["deck1"] = r.u16(); m["extra1"] = r.u16()
    elif mid == 5:          # WIN
        m["winner"] = r.u8(); m["reason"] = r.u8()
    elif mid == 10:         # SELECT_BATTLECMD
        m["player"] = r.u8()
        m["chains"] = []
        for _ in range(r.u32()):
            m["chains"].append({"code": r.u32(), **r.loc_ns(),
                                "desc": r.u64(), "mode": r.u8()})
        m["attacks"] = []
        for _ in range(r.u32()):
            m["attacks"].append({"code": r.u32(), **r.loc_s8(),
                                 "direct": r.u8()})
        m["to_m2"] = r.u8(); m["to_ep"] = r.u8()
    elif mid == 11:         # SELECT_IDLECMD
        m["player"] = r.u8()
        m["summon"] = [{"code": r.u32(), **r.loc_ns()} for _ in range(r.u32())]
        m["spsummon"] = [{"code": r.u32(), **r.loc_ns()} for _ in range(r.u32())]
        m["repos"] = [{"code": r.u32(), **r.loc_s8()} for _ in range(r.u32())]
        m["mset"] = [{"code": r.u32(), **r.loc_ns()} for _ in range(r.u32())]
        m["sset"] = [{"code": r.u32(), **r.loc_ns()} for _ in range(r.u32())]
        m["activate"] = []
        for _ in range(r.u32()):
            m["activate"].append({"code": r.u32(), **r.loc_ns(),
                                  "desc": r.u64(), "mode": r.u8()})
        m["to_bp"] = r.u8(); m["to_ep"] = r.u8(); m["can_shuffle"] = r.u8()
    elif mid == 12:         # SELECT_EFFECTYN
        m["player"] = r.u8(); m["code"] = r.u32()
        m["loc"] = r.loc(); m["desc"] = r.u64()
    elif mid == 13:         # SELECT_YESNO
        m["player"] = r.u8(); m["desc"] = r.u64()
    elif mid == 14:         # SELECT_OPTION
        m["player"] = r.u8()
        m["options"] = [r.u64() for _ in range(r.u8())]
    elif mid == 15:         # SELECT_CARD
        m["player"] = r.u8(); m["cancelable"] = r.u8()
        m["min"] = r.u32(); m["max"] = r.u32()
        m["cards"] = _cartas(r, r.u32())
    elif mid == 16:         # SELECT_CHAIN
        m["player"] = r.u8(); m["spe_count"] = r.u8(); m["forced"] = r.u8()
        m["timing_p"] = r.u32(); m["timing_o"] = r.u32()
        m["chains"] = []
        for _ in range(r.u32()):
            m["chains"].append({"code": r.u32(), **r.loc(),
                                "desc": r.u64(), "mode": r.u8()})
    elif mid in (18, 24):   # SELECT_PLACE / SELECT_DISFIELD
        m["player"] = r.u8(); m["count"] = r.u8(); m["flag"] = r.u32()
    elif mid == 19:         # SELECT_POSITION
        m["player"] = r.u8(); m["code"] = r.u32(); m["positions"] = r.u8()
    elif mid == 20:         # SELECT_TRIBUTE
        m["player"] = r.u8(); m["cancelable"] = r.u8()
        m["min"] = r.u32(); m["max"] = r.u32()
        m["cards"] = []
        for _ in range(r.u32()):
            code = r.u32(); con = r.u8(); loc = r.u8(); seq = r.u32(); rel = r.u8()
            m["cards"].append({"code": code, "con": con, "loc": loc,
                               "seq": seq, "pos": 0, "release": rel})
    elif mid in (21, 25):   # SORT_CHAIN / SORT_CARD (loc u32 aquí, no u8:
        # playerop.cpp field::process(SortCard&): u8 player, u32 count,
        # count × { u32 code; u8 controler; u32 location; u32 sequence })
        m["player"] = r.u8()
        n = r.u32()
        m["cards"] = []
        for _ in range(n):
            code = r.u32(); con = r.u8(); loc = r.u32(); seq = r.u32()
            m["cards"].append({"code": code, "con": con, "loc": loc,
                               "seq": seq, "pos": 0})
    elif mid == 22:         # SELECT_COUNTER
        m["player"] = r.u8(); m["countertype"] = r.u16(); m["count"] = r.u16()
        m["cards"] = []
        for _ in range(r.u32()):
            code = r.u32(); con = r.u8(); loc = r.u8(); seq = r.u8(); ct = r.u16()
            m["cards"].append({"code": code, "con": con, "loc": loc,
                               "seq": seq, "pos": 0, "counters": ct})
    elif mid == 23:         # SELECT_SUM
        m["player"] = r.u8(); m["mode"] = r.u8()
        m["acc"] = r.u32(); m["min"] = r.u32(); m["max"] = r.u32()
        m["must"] = [{"code": r.u32(), **r.loc(), "param": r.u32()}
                     for _ in range(r.u32())]
        m["cards"] = [{"code": r.u32(), **r.loc(), "param": r.u32()}
                      for _ in range(r.u32())]
    elif mid == 26:         # SELECT_UNSELECT_CARD
        m["player"] = r.u8(); m["finishable"] = r.u8(); m["cancelable"] = r.u8()
        m["min"] = r.u32(); m["max"] = r.u32()
        m["select"] = _cartas(r, r.u32())
        m["unselect"] = _cartas(r, r.u32())
    elif mid == 30:         # CONFIRM_DECKTOP
        m["player"] = r.u8(); m["cards"] = _cartas(r, r.u8())
    elif mid == 31:         # CONFIRM_CARDS
        m["player"] = r.u8()
        m["cards"] = [{"code": r.u32(), **r.loc_ns()} for _ in range(r.u32())]
    elif mid == 32:         # SHUFFLE_DECK
        m["player"] = r.u8()
    elif mid in (33, 39):   # SHUFFLE_HAND / SHUFFLE_EXTRA
        m["player"] = r.u8()
        m["codes"] = [r.u32() for _ in range(r.u32())]
    elif mid == 38:         # DECK_TOP
        m["player"] = r.u8(); m["code"] = r.u32()
    elif mid == 40:         # NEW_TURN
        m["player"] = r.u8()
    elif mid == 41:         # NEW_PHASE
        m["phase"] = r.u16()
    elif mid == 50:         # MOVE
        m["code"] = r.u32(); m["from"] = r.loc(); m["to"] = r.loc()
        m["reason"] = r.u32()
    elif mid == 53:         # POS_CHANGE
        m["code"] = r.u32(); m["con"] = r.u8(); m["loc"] = r.u8()
        m["seq"] = r.u8(); m["prev_pos"] = r.u8(); m["cur_pos"] = r.u8()
    elif mid == 54:         # SET (colocar boca abajo)
        m["code"] = r.u32(); m["loc"] = r.loc()
    elif mid in (60, 62, 64):   # SUMMONING / SPSUMMONING / FLIPSUMMONING
        m["code"] = r.u32(); m["loc"] = r.loc()
    elif mid in (61, 63, 65, 74, 112, 113, 114):   # sin payload
        # SUMMONED/SPSUMMONED/FLIPSUMMONED/CHAIN_END/ATTACK_DISABLED/...
        pass
    elif mid == 70:         # CHAINING
        m["code"] = r.u32(); m["loc"] = r.loc()
        m["tcon"] = r.u8(); m["tloc"] = r.u8(); m["tseq"] = r.u32()
        m["desc"] = r.u64(); m["chaincount"] = r.u32()
    elif mid in (71, 72, 73, 75, 76):  # CHAINED/SOLVING/SOLVED/NEGATED/DISABLED
        m["chaincount"] = r.u8()
    elif mid == 83:         # BECOME_TARGET
        m["targets"] = [r.loc() for _ in range(r.u32())]
    elif mid == 90:         # DRAW
        m["player"] = r.u8()
        m["cards"] = [{"code": r.u32(), "pos": r.u32()} for _ in range(r.u32())]
    elif mid in (91, 92):   # DAMAGE / RECOVER
        m["player"] = r.u8(); m["amount"] = r.u32()
    elif mid == 94:         # LPUPDATE
        m["player"] = r.u8(); m["lp"] = r.u32()
    elif mid == 100:        # PAY_LPCOST
        m["player"] = r.u8(); m["cost"] = r.u32()
    elif mid in (101, 102):  # ADD_COUNTER / REMOVE_COUNTER (card.cpp v11)
        m["countertype"] = r.u16(); m["con"] = r.u8(); m["loc"] = r.u8()
        m["seq"] = r.u8(); m["count"] = r.u16()
    elif mid == 110:        # ATTACK
        m["attacker"] = r.loc(); m["target"] = r.loc()
    elif mid == 111:        # BATTLE (paso de daño)
        m["attacker"] = r.loc()
        m["aa"] = r.u32(); m["ad"] = r.u32(); m["bd0"] = r.u8()
        m["target"] = r.loc()
        m["da"] = r.u32(); m["dd"] = r.u32(); m["bd1"] = r.u8()
    elif mid == 120:        # MISSED_EFFECT
        m["loc"] = r.loc(); m["code"] = r.u32()
    elif mid in (130, 131):  # TOSS_COIN / TOSS_DICE
        m["player"] = r.u8(); m["results"] = [r.u8() for _ in range(r.u8())]
    elif mid == 132:        # ROCK_PAPER_SCISSORS (playerop.cpp: u8 orden)
        m["player"] = r.u8()
    elif mid == 133:        # HAND_RES: u8 = hand0 | (hand1 << 2)
        m["value"] = r.u8()
    elif mid == 140:        # ANNOUNCE_RACE: u8 player, u8 count, u64 razas
        m["player"] = r.u8(); m["count"] = r.u8(); m["available"] = r.u64()
    elif mid == 141:        # ANNOUNCE_ATTRIB: u8 player, u8 count, u32 atrib
        m["player"] = r.u8(); m["count"] = r.u8(); m["available"] = r.u32()
    elif mid == 142:        # ANNOUNCE_CARD: u8 player, u8 n, n × u64 opcodes
        m["player"] = r.u8()
        m["opcodes"] = [r.u64() for _ in range(r.u8())]
    elif mid == 143:        # ANNOUNCE_NUMBER: u8 player, u8 n, n × u64 opc.
        m["player"] = r.u8()
        m["options"] = [r.u64() for _ in range(r.u8())]
    else:
        # mensaje no implementado (core más nuevo): se marca y el frame se
        # salta COMPLETO en parsear_stream, sin romper el framing.
        m["skip"] = True
        return m
    return m


def _parsear_stream(buf):
    """Parsea un stream completo con framing del core [u32 tamaño][u8 id][payload].

    El tamaño cuenta el byte de id + payload (duel::generate_buffer de duel.cpp).
    Mensajes desconocidos → {"id": .., "skip": True}; el frame se salta entero.
    Devuelve lista de dicts.
    """
    r = _MsgReader(buf)
    msgs = []
    while not r.done():
        size = r.u32()
        frame = _MsgReader(r.take(size))
        mid = frame.u8()
        m = _parsear_mensaje(mid, frame)
        if not frame.done() and not m.get("skip"):
            raise ReplayError(
                f"sobran bytes en el mensaje id={mid} "
                f"({MSG_NAMES.get(mid, '?')}); formato del core no soportado")
        msgs.append(m)
    return msgs


def _a_framing_red(packets):
    """Packets del replay [u8 id][u32 len][payload] → framing de red del core
    [u32 tamaño][u8 id][payload], con tamaño = len(payload) + 1 (el byte de id)."""
    out = bytearray()
    for pid, payload in packets:
        out += struct.pack("<I", len(payload) + 1)
        out.append(pid)
        out += payload
    return bytes(out)


def _msg_start_sintetico(lp0, lp1, deck0, extra0, deck1, extra1):
    """MSG_START variante EDOPro (17B) en framing de red, para streams que no
    lo traen: [u8 player][i32 lp0][i32 lp1][u16 deck0][u16 extra0]
    [u16 deck1][u16 extra1]."""
    payload = struct.pack("<BiiHHHH", 0, lp0, lp1, deck0, extra0, deck1, extra1)
    return struct.pack("<I", len(payload) + 1) + bytes([4]) + payload


def _stream_red(rep):
    """Buffer del stream de mensajes en framing de red para un Replay yrpX.
    Inyecta un MSG_START sintético (LP/conteos del header) solo si el stream no
    trae el suyo, igual que hacía pywindbot (sin MSG_START duplicado)."""
    if rep.formato != "yrpX":
        return None   # el .yrp clásico no lleva stream (lleva respuestas)
    buf = _a_framing_red(rep._packets)
    p = rep.params or {}
    if p.get("start_lp") is not None and not any(pid == 4 for pid, _ in rep._packets):
        decks = rep.decks
        d0 = decks[0] if len(decks) > 0 else {"main": [], "extra": []}
        d1 = decks[1] if len(decks) > 1 else {"main": [], "extra": []}
        buf = (_msg_start_sintetico(p["start_lp"], p["start_lp"],
                                    len(d0["main"]), len(d0["extra"]),
                                    len(d1["main"]), len(d1["extra"])) + buf)
    return buf


def eventos(replay_or_stream):
    """Decodifica los mensajes del core a una lista de dicts.

    Acepta:
      * un :class:`Replay` (de :func:`leer_replay`): usa el stream yrpX
        (con MSG_START sintético si el stream no lo trae). Un .yrp clásico
        devuelve [] (ese formato trae respuestas, no eventos).
      * ``bytes`` con framing de red del core [u32 tamaño][u8 id][payload].

    Ids desconocidos → ``{"id": .., "skip": True}`` sin romper el framing.
    """
    if isinstance(replay_or_stream, (bytes, bytearray, memoryview)):
        return _parsear_stream(bytes(replay_or_stream))
    rep = replay_or_stream
    if rep._cache_eventos is not None:
        return rep._cache_eventos
    buf = _stream_red(rep)
    rep._cache_eventos = _parsear_stream(buf) if buf else []
    return rep._cache_eventos


# --------------------------------------------------------------------------- #
#  Tracker: espejo de campo (port compacto de pywindbot/net/tracker.py)        #
# --------------------------------------------------------------------------- #
# REGLA DE ORO (ClientCard.SetId del WindBot): una carta oculta NUNCA se
# adivina; code 0 se guarda como None y solo se conoce lo que el core revela.

def _carta_nueva(code=None, pos=0):
    return {"code": code, "pos": pos, "overlay": [], "atacado": False}


class _Campo:
    """Campo de un equipo: mano, zonas, cementerio, destierro y conteos."""

    __slots__ = ("hand", "mzone", "szone", "grave", "desterradas",
                 "deck_count", "extra_count", "lp")

    def __init__(self, lp=8000):
        self.hand = []        # [code | None] por índice de mano (seq)
        self.mzone = {}       # seq -> carta
        self.szone = {}       # seq -> carta
        self.grave = []       # [code | None]
        self.desterradas = []
        self.deck_count = 0   # el mazo es opaco: solo conteo
        self.extra_count = 0
        self.lp = lp

    def monstruos(self):
        return sorted(self.mzone.items())


class _Tracker:
    """Espejo del duelo: campos de ambos equipos + LP/turno/fase/cadenas.

    Port compacto del Tracker de PyWindBot (que a su vez replica los handlers
    OnStart/OnMove/OnDraw/... del GameBehavior.cs del WindBot oficial). Sin la
    parte de red (mazo propio del bot), que no aplica a replays.
    """

    def __init__(self, lp=8000):
        self.fields = {0: _Campo(lp), 1: _Campo(lp)}
        self.lp = {0: lp, 1: lp}
        self.turno = 0
        self.turn_player = 0
        self.fase = 0
        self.cadenas = []          # [{code, con, loc, seq, desc, negada}]
        self.atacante = None       # (con, loc, seq) del MSG_ATTACK en curso
        self.ganador = None        # equipo ganador del MSG_WIN
        self.motivo_win = None

    # ---- acceso a cartas (Duel.GetCard / RemoveCard / AddCard) ----
    def _obtener(self, con, loc, seq):
        campo = self.fields.get(con)
        if campo is None:
            return None
        if loc == LOCATION_MZONE:
            return campo.mzone.get(seq)
        if loc == LOCATION_SZONE:
            return campo.szone.get(seq)
        return None

    def _quitar(self, con, loc, seq, code=None):
        campo = self.fields[con]
        if loc == LOCATION_MZONE:
            return campo.mzone.pop(seq, None)
        if loc == LOCATION_SZONE:
            return campo.szone.pop(seq, None)
        if loc == LOCATION_HAND:
            if 0 <= seq < len(campo.hand):
                return _carta_nueva(campo.hand.pop(seq))
            return _carta_nueva(code)
        if loc == LOCATION_GRAVE:
            if 0 <= seq < len(campo.grave):
                return _carta_nueva(campo.grave.pop(seq))
            if campo.grave:
                return _carta_nueva(campo.grave.pop(0))
            return _carta_nueva(code)
        if loc == LOCATION_REMOVED:
            if 0 <= seq < len(campo.desterradas):
                return _carta_nueva(campo.desterradas.pop(seq))
            if campo.desterradas:
                return _carta_nueva(campo.desterradas.pop(0))
            return _carta_nueva(code)
        return _carta_nueva(code)

    def _poner(self, con, loc, seq, code, pos):
        campo = self.fields[con]
        code = code or None   # 0 -> oculta (NUNCA adivinar)
        if loc == LOCATION_MZONE:
            c = campo.mzone.get(seq) or _carta_nueva()
            if code:
                c["code"] = code
            c["pos"] = pos
            campo.mzone[seq] = c
        elif loc == LOCATION_SZONE:
            c = campo.szone.get(seq) or _carta_nueva()
            if code:
                c["code"] = code
            c["pos"] = pos
            campo.szone[seq] = c
        elif loc == LOCATION_HAND:
            while len(campo.hand) <= seq:
                campo.hand.append(None)
            campo.hand[seq] = code
        elif loc == LOCATION_GRAVE:
            while len(campo.grave) <= seq:
                campo.grave.append(None)
            campo.grave[seq] = code
        elif loc == LOCATION_REMOVED:
            while len(campo.desterradas) <= seq:
                campo.desterradas.append(None)
            campo.desterradas[seq] = code

    # ---- OnMove (GameBehavior.cs): el caballo de batalla ----
    def _aplicar_move(self, m):
        prev = m["from"]
        cur = m["to"]
        code = m["code"] or 0
        ploc = prev["loc"]
        cloc = cur["loc"]

        # origen OVERLAY (0x80): se desacopla del anfitrión y es carta nueva
        if ploc & LOCATION_OVERLAY:
            host = self._obtener(prev["con"], ploc & 0x7f, prev["seq"])
            if host is not None and code in host["overlay"]:
                host["overlay"].remove(code)
            ploc = 0

        carta = None
        if ploc == LOCATION_DECK:
            if self.fields[prev["con"]].deck_count > 0:
                self.fields[prev["con"]].deck_count -= 1
        elif ploc == LOCATION_EXTRA:
            if self.fields[prev["con"]].extra_count > 0:
                self.fields[prev["con"]].extra_count -= 1
        elif ploc:
            carta = self._quitar(prev["con"], ploc, prev["seq"], code)

        # destino: OVERLAY acopla al anfitrión; resto coloca la carta
        if cloc & LOCATION_OVERLAY:
            host = self._obtener(cur["con"], cloc & 0x7f, cur["seq"])
            if host is not None:
                host["overlay"].append(code or None)
        elif cloc == LOCATION_DECK:
            self.fields[cur["con"]].deck_count += 1
        elif cloc == LOCATION_EXTRA:
            self.fields[cur["con"]].extra_count += 1
        else:
            if carta is not None and carta["code"]:
                code = carta["code"]   # conservar la identidad ya conocida
            self._poner(cur["con"], cloc, cur["seq"], code, cur["pos"])

    # ---- dispatcher principal (una rama por handler del GameBehavior) ----
    def aplicar(self, m):
        mid = m["id"]

        if mid == 4:        # OnStart: LP y conteos iniciales (equipos core 0/1)
            for t in (0, 1):
                c = self.fields[t]
                c.hand = []; c.mzone = {}; c.szone = {}; c.grave = []
                c.desterradas = []
            self.fields[0].lp = m["lp0"]; self.fields[1].lp = m["lp1"]
            self.lp = {0: m["lp0"], 1: m["lp1"]}
            self.fields[0].deck_count = m["deck0"]
            self.fields[0].extra_count = m["extra0"]
            self.fields[1].deck_count = m["deck1"]
            self.fields[1].extra_count = m["extra1"]
            self.turno = 0
            self.cadenas = []
            return

        if mid == 40:       # OnNewTurn
            self.turno += 1
            self.turn_player = m["player"]
            for campo in self.fields.values():
                for _, c in campo.monstruos():
                    c["atacado"] = False
            return

        if mid == 41:       # OnNewPhase
            self.fase = m["phase"]
            return

        if mid == 90:       # OnDraw: deck.pop + hand.add (code visible o None)
            campo = self.fields[m["player"]]
            for info in m["cards"]:
                if campo.deck_count > 0:
                    campo.deck_count -= 1
                campo.hand.append(info["code"] or None)
            return

        if mid == 33:       # OnShuffleHand: la mano queda en el orden dado
            self.fields[m["player"]].hand = [c or None for c in m["codes"]]
            return

        if mid in (32, 39):  # OnShuffleDeck/Extra: el mazo/extra es opaco
            return

        if mid == 50:       # OnMove
            self._aplicar_move(m)
            return

        if mid == 53:       # OnPosChange
            c = self._obtener(m["con"], m["loc"], m["seq"])
            if c is not None:
                c["pos"] = m["cur_pos"]
            return

        if mid == 54:       # OnSet (S/T boca abajo)
            self._poner(m["loc"]["con"], m["loc"]["loc"], m["loc"]["seq"],
                        m["code"], m["loc"]["pos"])
            return

        if mid in (60, 62, 64):   # OnSummoning/SpSummoning/FlipSummoning
            loc = m["loc"]
            self._poner(loc["con"], loc["loc"], loc["seq"], m["code"], loc["pos"])
            return

        if mid == 31:       # OnConfirm: revela códigos
            for info in m["cards"]:
                con, loc, seq, code = (info["con"], info["loc"],
                                       info["seq"], info["code"])
                if loc in (LOCATION_MZONE, LOCATION_SZONE):
                    c = self._obtener(con, loc, seq)
                    if c is not None and code:
                        c["code"] = code
                elif loc == LOCATION_HAND:
                    campo = self.fields[con]
                    if 0 <= seq < len(campo.hand) and code:
                        campo.hand[seq] = code
                elif loc == LOCATION_GRAVE and code:
                    campo = self.fields[con]
                    while len(campo.grave) <= seq:
                        campo.grave.append(None)
                    campo.grave[seq] = code
            return

        if mid == 70:       # OnChaining: CurrentChain.Add
            loc = m["loc"]
            self.cadenas.append({"code": m["code"], "con": loc["con"],
                                 "loc": loc["loc"], "seq": loc["seq"],
                                 "desc": m["desc"], "negada": False})
            return

        if mid in (72, 73):  # OnChainSolving (no altera la ventana)
            return

        if mid in (75, 76):  # OnChainNegated/Disabled: marca la cadena
            i = m["chaincount"] - 1
            if 0 <= i < len(self.cadenas):
                self.cadenas[i]["negada"] = True
            return

        if mid == 74:       # OnChainEnd: limpia la ventana
            self.cadenas = []
            return

        if mid == 83:       # OnBecomeTarget (no afecta el espejo aquí)
            return

        if mid == 110:      # OnAttack
            a = m["attacker"]
            self.atacante = (a["con"], a["loc"], a["seq"])
            c = self._obtener(a["con"], a["loc"], a["seq"])
            if c is not None:
                c["atacado"] = True
            return

        if mid in (91, 92, 94, 100):   # OnDamage/Recover/LpUpdate/PayLpCost
            p = m["player"]
            if mid == 91:
                self.lp[p] = max(0, self.lp[p] - m["amount"])
            elif mid == 92:
                self.lp[p] = self.lp[p] + m["amount"]
            elif mid == 94:
                self.lp[p] = m["lp"]
            else:
                self.lp[p] = max(0, self.lp[p] - m["cost"])
            self.fields[p].lp = self.lp[p]
            return

        if mid == 5:        # OnWin
            self.ganador = m["winner"]
            self.motivo_win = m["reason"]
            return

        # el resto (SELECT_*, TOSS, ...) no altera el espejo de campo


def _snapshot_campo(tr):
    """Estado legible del campo en un instante (contexto de las negaciones)."""
    out = {}
    for t in (0, 1):
        c = tr.fields[t]
        out[f"lp_{t}"] = tr.lp[t]
        out[f"monstruos_{t}"] = [
            {"code": cc["code"], "pos": cc["pos"], "seq": seq}
            for seq, cc in c.monstruos() if cc["code"]
        ]
        out[f"magias_trampas_{t}"] = sum(
            1 for _, cc in sorted(c.szone.items()) if cc["code"] or cc["pos"])
        out[f"mano_{t}"] = len(c.hand)
        out[f"deck_{t}"] = c.deck_count
        out[f"extra_{t}"] = c.extra_count
        out[f"cementerio_{t}"] = len(c.grave)
    return out


# --------------------------------------------------------------------------- #
#  Timeline: turnos/fases con acciones por equipo                              #
# --------------------------------------------------------------------------- #

def _nombre_equipo(equipo, jugadores):
    if jugadores and 0 <= equipo < len(jugadores):
        return jugadores[equipo]
    return f"equipo{equipo}"


def timeline(eventos_lista, params, decks=None, jugadores=None, nombres=None):
    """Agrupa los eventos por turno/fase con acciones por equipo.

    ``params`` es el dict del header ({start_lp, ...}): el LP inicial del
    duelo. ``decks`` (opcional) inicializa los conteos de mazo si el stream
    no trae MSG_START. ``nombres`` (opcional) es un dict {code: nombre} para
    leer las cartas; por defecto se emiten códigos ``#12345``.

    Devuelve una lista de turnos:
        [{turno, jugador, nombre_jugador, fases: [{fase, fase_str, acciones}],
          campo_final}]
    """
    tr = _Tracker((params or {}).get("start_lp") or 8000)
    if decks:
        for t, d in enumerate(decks[:2]):
            tr.fields[t].deck_count = len(d.get("main", []))
            tr.fields[t].extra_count = len(d.get("extra", []))

    def nom(code):
        if not code:
            return None
        if nombres and code in nombres:
            return nombres[code]
        return f"#{code}"

    def loc_txt(loc):
        return f"{loc['con']}:{loc['loc']}:{loc['seq']}"

    def accion_de(m):
        """Convierte UN evento en acción de timeline (o None si es ruido)."""
        mid = m["id"]
        if mid == 90:       # DRAW
            cartas = [nom(c["code"]) or "?" for c in m["cards"]]
            return {"tipo": "roba", "equipo": m["player"],
                    "quien": _nombre_equipo(m["player"], jugadores),
                    "cartas": cartas}
        if mid in (60, 62, 64):   # SUMMONING / SPSUMMONING / FLIPSUMMONING
            tipo = {60: "invoca", 62: "invoca_especial", 64: "invoca_flip"}[mid]
            loc = m["loc"]
            return {"tipo": tipo, "equipo": loc["con"],
                    "quien": _nombre_equipo(loc["con"], jugadores),
                    "carta": nom(m["code"]) or "?", "code": m["code"],
                    "pos": loc["pos"], "zona": loc_txt(loc)}
        if mid == 54:       # SET
            loc = m["loc"]
            return {"tipo": "coloca_boca_abajo", "equipo": loc["con"],
                    "quien": _nombre_equipo(loc["con"], jugadores),
                    "carta": nom(m["code"]) or "(boca abajo)",
                    "code": m["code"] or None, "zona": loc_txt(loc)}
        if mid == 70:       # CHAINING
            loc = m["loc"]
            return {"tipo": "activa", "equipo": loc["con"],
                    "quien": _nombre_equipo(loc["con"], jugadores),
                    "carta": nom(m["code"]) or "?", "code": m["code"],
                    "desc": m["desc"], "cadena": m["chaincount"],
                    "zona": loc_txt(loc)}
        if mid in (75, 76):  # CHAIN_NEGATED / CHAIN_DISABLED
            tipo = "niega_efecto" if mid == 75 else "niega_activacion"
            i = m["chaincount"] - 1
            negada = tr.cadenas[i] if 0 <= i < len(tr.cadenas) else None
            negador = None
            # el negador: la cadena posterior (inmediata típica) del rival
            if negada is not None:
                for c in tr.cadenas[m["chaincount"]:]:
                    if c["con"] != negada["con"]:
                        negador = c
                        break
            acc = {"tipo": tipo,
                   "equipo": negada["con"] if negada else None,
                   "cadena": m["chaincount"]}
            if negada is not None:
                acc["quien"] = _nombre_equipo(negada["con"], jugadores)
                acc["carta_intentada"] = nom(negada["code"]) or "?"
                acc["code_intentado"] = negada["code"]
                acc["negador"] = nom(negador["code"]) if negador else None
                acc["code_negador"] = negador["code"] if negador else None
            return acc
        if mid == 110:      # ATTACK
            a = m["attacker"]
            t = m["target"]
            c = tr._obtener(a["con"], a["loc"], a["seq"])
            code_a = c["code"] if c else None
            acc = {"tipo": "ataca", "equipo": a["con"],
                   "quien": _nombre_equipo(a["con"], jugadores),
                   "carta": nom(code_a) or "?", "code": code_a,
                   "atacante": loc_txt(a)}
            if t["loc"]:
                acc["objetivo"] = loc_txt(t)
                acc["equipo_objetivo"] = t["con"]
            else:
                acc["objetivo"] = "directo"
                acc["equipo_objetivo"] = 1 - a["con"]
            return acc
        if mid == 111:      # BATTLE: resultado del paso de daño
            a = m["attacker"]
            t = m["target"]
            c = tr._obtener(a["con"], a["loc"], a["seq"])
            acc = {"tipo": "batalla", "equipo": a["con"],
                   "quien": _nombre_equipo(a["con"], jugadores),
                   "atk_atacante": m["aa"], "def_atacante": m["ad"],
                   "carta": nom(c["code"]) if (c and c["code"]) else "?"}
            if t["loc"]:
                d = tr._obtener(t["con"], t["loc"], t["seq"])
                acc["carta_objetivo"] = (nom(d["code"]) if (d and d["code"])
                                         else "(boca abajo)")
                acc["atk_objetivo"] = m["da"]
                acc["def_objetivo"] = m["dd"]
            else:
                acc["carta_objetivo"] = None
            if m["bd0"] == 1:
                acc["nota"] = "atacante destruido"
            if m["bd1"] == 1:
                acc["nota"] = (acc.get("nota", "") + " objetivo destruido").strip()
            return acc
        if mid == 112:      # ATTACK_DISABLED
            a = tr.atacante
            acc = {"tipo": "ataque_cancelado"}
            if a:
                c = tr._obtener(*a)
                acc["equipo"] = a[0]
                acc["quien"] = _nombre_equipo(a[0], jugadores)
                acc["carta"] = nom(c["code"]) if (c and c["code"]) else "?"
            return acc
        if mid == 91:       # DAMAGE
            return {"tipo": "dano", "equipo": m["player"],
                    "quien": _nombre_equipo(m["player"], jugadores),
                    "cantidad": m["amount"], "lp": tr.lp.get(m["player"])}
        if mid == 92:       # RECOVER
            return {"tipo": "recupera", "equipo": m["player"],
                    "quien": _nombre_equipo(m["player"], jugadores),
                    "cantidad": m["amount"], "lp": tr.lp.get(m["player"])}
        if mid == 5:        # WIN
            return {"tipo": "victoria", "equipo": m["winner"],
                    "quien": _nombre_equipo(m["winner"], jugadores),
                    "motivo": m["reason"],
                    "motivo_str": WIN_REASONS.get(m["reason"], f"?{m['reason']}")}
        return None

    def snapshot():
        out = {}
        for t in (0, 1):
            campo = tr.fields[t]
            mons = []
            for seq, c in campo.monstruos():
                mons.append({
                    "seq": seq,
                    "carta": (nom(c["code"]) or "(boca abajo)"),
                    "pos": c["pos"],
                    "materiales": len(c["overlay"]),
                })
            out[f"lp_{t}"] = tr.lp.get(t)
            out[f"mano_{t}"] = len(campo.hand)
            out[f"monstruos_{t}"] = mons
            out[f"magias_{t}"] = len(campo.szone)
            out[f"cementerio_{t}"] = len(campo.grave)
            out[f"deck_{t}"] = campo.deck_count
            out[f"extra_{t}"] = campo.extra_count
        return out

    turnos = []
    cur_turno = None
    cur_fase = None
    turno_n = 0   # el core numera los turnos desde 1 (NEW_TURN)
    for m in eventos_lista:
        mid = m["id"]
        if mid == 40:     # NEW_TURN: cierra el turno anterior y abre otro
            if cur_turno is not None:
                cur_turno["campo_final"] = snapshot()
            turno_n += 1
            cur_turno = {"turno": turno_n, "jugador": m["player"],
                         "nombre_jugador": _nombre_equipo(m["player"], jugadores),
                         "fases": [], "campo_final": None}
            turnos.append(cur_turno)
            cur_fase = None
        elif mid == 41:   # NEW_PHASE: abre fase dentro del turno
            if cur_turno is None:
                cur_turno = {"turno": 0, "jugador": 0,
                             "nombre_jugador": _nombre_equipo(0, jugadores),
                             "fases": [], "campo_final": None}
                turnos.append(cur_turno)
            cur_fase = {"fase": m["phase"],
                        "fase_str": PHASE_NAMES.get(m["phase"], f"?{m['phase']}"),
                        "acciones": []}
            cur_turno["fases"].append(cur_fase)
        acc = accion_de(m)
        if acc is not None:
            if cur_turno is None:
                # acciones de preparación previas al primer NEW_TURN
                cur_turno = {"turno": 0, "jugador": acc.get("equipo", 0),
                             "nombre_jugador": _nombre_equipo(
                                 acc.get("equipo", 0), jugadores),
                             "fases": [], "campo_final": None}
                turnos.append(cur_turno)
            if cur_fase is None:
                cur_fase = {"fase": 0, "fase_str": "inicio", "acciones": []}
                cur_turno["fases"].append(cur_fase)
            cur_fase["acciones"].append(acc)
        tr.aplicar(m)
    if cur_turno is not None:
        cur_turno["campo_final"] = snapshot()
    return turnos


# --------------------------------------------------------------------------- #
#  Análisis: activaciones, negaciones, ataques, combos, estadísticas           #
#  (port de pywindbot/replay.py::analizar)                                     #
# --------------------------------------------------------------------------- #

# Negadores conocidos (códigos canónicos TCG). CUALQUIER cadena negada (75/76)
# se detecta aunque el negador no esté aquí; este diccionario solo etiqueta
# `negador_es_conocido`. Ampliable por el usuario (dict público mutable).
NEGADORES_CONOCIDOS = {
    14558127: "Ash Blossom & Joyous Spring",
    23434538: 'Maxx "C"',
    59438930: "Ghost Ogre & Snow Rabbit",
    10045474: "Infinite Impermanence",
    97268675: "Effect Veiler",
    81439173: "Solemn Judgment",
    84749824: "Solemn Warning",
    40605147: "Solemn Strike",
}


def analizar(eventos_lista, tl, decks, params=None, jugadores=None,
             nombres=None):
    """Patrones del duelo para estudio/entrenamiento.

    ``tl`` es la timeline (de :func:`timeline`): se usa para los LP finales
    (campo_final del último turno). ``decks`` da el tamaño main/extra inicial.
    ``params`` (opcional) aporta el LP inicial si el stream no trae MSG_START.

    Devuelve: activaciones (cada CHAINING con contexto), negaciones (con
    contexto de campo y negador conocido según NEGADORES_CONOCIDOS), ataques
    (con daño), combos (secuencias A→B→C del mismo equipo/turno con
    frecuencia), estadísticas por jugador y resumen (con ganador).
    """
    tr = _Tracker((params or {}).get("start_lp") or 8000)
    decks = decks or []
    for t, d in enumerate(decks[:2]):
        tr.fields[t].deck_count = len(d.get("main", []))
        tr.fields[t].extra_count = len(d.get("extra", []))

    def nom(code):
        if not code:
            return None
        if nombres and code in nombres:
            return nombres[code]
        return f"#{code}"

    activaciones = []
    negaciones = []
    ataques = []
    combos_contador = {}
    ventana = []   # activaciones recientes del mismo equipo/turno (combos)
    turno, fase = 0, 0
    cadena_actual = None

    for m in eventos_lista:
        mid = m["id"]
        if mid == 40:
            turno += 1
            fase = 0
            ventana = []
            tr.aplicar(m)
            continue
        if mid == 41:
            fase = m["phase"]
            ventana = []
            tr.aplicar(m)
            continue

        if mid == 70:   # CHAINING
            loc = m["loc"]
            act = {
                "carta": nom(m["code"]) or "?", "code": m["code"],
                "equipo": loc["con"],
                "jugador": _nombre_equipo(loc["con"], jugadores),
                "turno": turno, "fase": fase,
                "fase_str": PHASE_NAMES.get(fase, str(fase)),
                "cadena": m["chaincount"], "desc": m["desc"],
                "origen": {"con": loc["con"], "loc": loc["loc"],
                           "seq": loc["seq"]},
                "targets": [],
            }
            activaciones.append(act)
            ventana.append((loc["con"], m["code"], turno))
            if len(ventana) >= 2:
                # combos de 2 y 3 cartas del MISMO equipo en el MISMO turno
                for k in (2, 3):
                    seq = ventana[-k:]
                    if len(seq) == k and all(
                            s[0] == seq[0][0] and s[2] == seq[0][2] for s in seq):
                        clave = tuple(s[1] for s in seq)
                        combos_contador[clave] = combos_contador.get(clave, 0) + 1
            cadena_actual = act
            tr.aplicar(m)
            continue

        if mid == 83 and cadena_actual is not None:   # BECOME_TARGET
            for loc in m["targets"]:
                cadena_actual["targets"].append(
                    {"con": loc["con"], "loc": loc["loc"], "seq": loc["seq"]})
            tr.aplicar(m)
            continue

        if mid in (75, 76):   # negación (76 activación / 75 efecto)
            i = m["chaincount"] - 1
            negada = tr.cadenas[i] if 0 <= i < len(tr.cadenas) else None
            negador = None
            if negada is not None:
                for c in tr.cadenas[m["chaincount"]:]:
                    if c["con"] != negada["con"]:
                        negador = c
                        break
            negaciones.append({
                "tipo": "negacion_activacion" if mid == 76 else "negacion_efecto",
                "turno": turno, "fase": fase,
                "fase_str": PHASE_NAMES.get(fase, str(fase)),
                "equipo_intentado": negada["con"] if negada else None,
                "jugador_intentado": (_nombre_equipo(negada["con"], jugadores)
                                      if negada else None),
                "carta_intentada": nom(negada["code"]) if negada else None,
                "code_intentado": negada["code"] if negada else None,
                "desc_intentado": negada["desc"] if negada else None,
                "negador": nom(negador["code"]) if negador else None,
                "code_negador": negador["code"] if negador else None,
                "negador_es_conocido": bool(
                    negador and negador["code"] in NEGADORES_CONOCIDOS),
                "negador_jugador": (_nombre_equipo(negador["con"], jugadores)
                                    if negador else None),
                "campo_en_ese_momento": _snapshot_campo(tr),
            })
            tr.aplicar(m)
            continue

        if mid == 110:   # ATTACK
            a = m["attacker"]
            c = tr._obtener(a["con"], a["loc"], a["seq"])
            code_a = c["code"] if c else None
            ataques.append({
                "turno": turno, "fase": fase,
                "fase_str": PHASE_NAMES.get(fase, str(fase)),
                "equipo": a["con"],
                "jugador": _nombre_equipo(a["con"], jugadores),
                "atacante": nom(code_a) or "?", "code_atacante": code_a,
                "atk": None, "objetivo": None, "carta_objetivo": None,
                "danio": None,
            })
            tr.aplicar(m)
            continue

        if mid == 111:   # BATTLE: completa el ataque con valores reales
            if ataques:
                at = ataques[-1]
                m_a = m["attacker"]
                at["atk"] = m["aa"]
                t = m["target"]
                if t["loc"]:
                    d = tr._obtener(t["con"], t["loc"], t["seq"])
                    at["objetivo"] = {"con": t["con"], "loc": t["loc"],
                                      "seq": t["seq"]}
                    at["carta_objetivo"] = (nom(d["code"]) if (d and d["code"])
                                            else "(boca abajo)")
                    at["def_objetivo"] = m["dd"]
                else:
                    at["objetivo"] = "directo"
                # daño: directo = aa; con objetivo = aa - dd si destruye con aa>dd
                if at["objetivo"] == "directo":
                    at["danio"] = m["aa"]
                elif m["bd1"] == 1 and m["aa"] > m["dd"]:
                    at["danio"] = m["aa"] - m["dd"]
            tr.aplicar(m)
            continue

        tr.aplicar(m)

    # ---- combos A→B→C legibles, ordenados por frecuencia ----
    combos = []
    for secuencia, frecuencia in sorted(combos_contador.items(),
                                        key=lambda x: -x[1]):
        combos.append({
            "secuencia": [nom(c) or "?" for c in secuencia],
            "codes": list(secuencia),
            "frecuencia": frecuencia,
        })

    # ---- LP finales: de la timeline (campo_final del último turno) o tracker
    def lp_final(t):
        for turno_tl in reversed(tl or []):
            cf = turno_tl.get("campo_final") or {}
            if cf.get(f"lp_{t}") is not None:
                return cf[f"lp_{t}"]
        return tr.lp.get(t)

    # ---- estadísticas por deck/jugador ----
    stats = []
    nombres_eq = list(jugadores[:2]) if jugadores and len(jugadores) >= 2 \
        else ["equipo0", "equipo1"]
    for t, nombre_j in enumerate(nombres_eq):
        acts = [a for a in activaciones if a["equipo"] == t]
        usadas = {}
        for a in acts:
            usadas[a["carta"]] = usadas.get(a["carta"], 0) + 1
        danio_recibido = sum(x["amount"] for x in eventos_lista
                             if x["id"] == 91 and x["player"] == t)
        danio_hecho = sum(x["amount"] for x in eventos_lista
                          if x["id"] == 91 and x["player"] == (1 - t))
        deck_info = decks[t] if len(decks) > t else {"main": [], "extra": []}
        stats.append({
            "jugador": nombre_j, "equipo": t,
            "main": len(deck_info.get("main", [])),
            "extra": len(deck_info.get("extra", [])),
            "activaciones": len(acts),
            "cartas_mas_usadas": sorted(usadas.items(),
                                        key=lambda x: -x[1])[:10],
            "danio_recibido": danio_recibido,
            "danio_infligido_al_rival": danio_hecho,
            "lp_final": lp_final(t),
        })

    win_ev = next((x for x in reversed(eventos_lista) if x["id"] == 5), None)
    return {
        "activaciones": activaciones,
        "negaciones": negaciones,
        "ataques": ataques,
        "combos": combos[:50],
        "estadisticas": stats,
        "resumen": {
            "eventos": len(eventos_lista),
            "turnos": turno,
            "activaciones": len(activaciones),
            "negaciones": len(negaciones),
            "ataques": len(ataques),
            "combos_distintos": len(combos_contador),
            "ganador": tr.ganador,
            "ganador_nombre": (_nombre_equipo(tr.ganador, jugadores)
                               if tr.ganador is not None else None),
            "motivo_win": (WIN_REASONS.get(win_ev["reason"],
                                           win_ev["reason"])
                           if win_ev else None),
        },
    }


# --------------------------------------------------------------------------- #
#  Nombres de cartas: .cdb offline (sqlite3) o API online (opcional)           #
# --------------------------------------------------------------------------- #

def nombres_desde_cdb(ruta_cdb, codes):
    """Lee {code: nombre} de un ``cards.cdb`` de EDOPro (sqlite3 stdlib).

    El nombre vive en la tabla ``texts`` (id, name) unida a ``datas`` por id;
    algunas variantes raras lo traen en ``datas`` mismo: se prueban ambas.
    Modo read-only para no crear archivos por accidente (nota: en Windows las
    rutas URI necesitan slashes posix, igual que en pywindbot/carddb.py).
    """
    codes = sorted({int(c) for c in codes if c})
    out = {}
    if not codes:
        return out
    uri = "file:" + Path(ruta_cdb).as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        tablas = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "texts" in tablas:
            sql = "SELECT t.id, t.name FROM texts t WHERE t.id IN ({})"
        elif "datas" in tablas:
            sql = "SELECT d.id, d.name FROM datas d WHERE d.id IN ({})"
        else:
            raise ReplayError(
                "el .cdb no tiene tabla 'texts' ni 'datas' con nombres")
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            q = sql.format(",".join("?" * len(chunk)))
            for cid, nombre in con.execute(q, chunk):
                if nombre:
                    out[int(cid)] = nombre
    finally:
        con.close()
    return out


_CACHE_ONLINE = {}   # caché en memoria {code: nombre} de la sesión
_API_YGOPRODECK = "https://db.ygoprodeck.com/api/v7/cardinfo.php"


def nombres_online(codes, timeout=15):
    """Resuelve {code: nombre} faltantes con la API pública de YGOPRODeck.

    SOLO se llama si el usuario pasa ``--online`` (jamás automático). Usa
    urllib stdlib y una caché en memoria de la sesión; los lotes van de 100
    ids y si el lote falla se reintenta carta por carta (la API responde 400
    si UN id del lote no existe).
    """
    pedidos = sorted({int(c) for c in codes if c})
    faltantes = [c for c in pedidos if c not in _CACHE_ONLINE]
    for i in range(0, len(faltantes), 100):
        chunk = faltantes[i:i + 100]
        url = _API_YGOPRODECK + "?" + urllib.parse.urlencode(
            {"id": ",".join(map(str, chunk))})
        req = urllib.request.Request(
            url, headers={"User-Agent": f"edopro-replay2json/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                datos = json.loads(resp.read().decode("utf-8"))
            for carta in datos.get("data", []):
                _CACHE_ONLINE[int(carta["id"])] = carta["name"]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError):
            # lote rechazado (algún id inexistente / sin red): intento unitario
            for c in chunk:
                if c in _CACHE_ONLINE:
                    continue
                url1 = _API_YGOPRODECK + "?" + urllib.parse.urlencode({"id": c})
                req1 = urllib.request.Request(
                    url1, headers={"User-Agent":
                                   f"edopro-replay2json/{__version__}"})
                try:
                    with urllib.request.urlopen(req1, timeout=timeout) as resp:
                        datos = json.loads(resp.read().decode("utf-8"))
                    for carta in datos.get("data", []):
                        _CACHE_ONLINE[int(carta["id"])] = carta["name"]
                except (urllib.error.URLError, urllib.error.HTTPError,
                        OSError, ValueError):
                    pass   # sin red o carta desconocida: queda solo el código
    return {c: _CACHE_ONLINE[c] for c in pedidos if c in _CACHE_ONLINE}


def _codes_del_replay(rep):
    """Todos los códigos de carta citados en el replay (decks + eventos),
    para resolver nombres de una sola vez con --cdb/--online."""
    codes = set()
    for d in rep.decks:
        codes.update(c for c in d.get("main", []) if c)
        codes.update(c for c in d.get("extra", []) if c)

    def visitar(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("code", "codes") and isinstance(v, int) and v:
                    codes.add(v)
                elif isinstance(v, (dict, list)):
                    visitar(v)
        elif isinstance(x, list):
            for v in x:
                visitar(v)

    visitar(eventos(rep))
    return sorted(codes)


# --------------------------------------------------------------------------- #
#  a_json: todo junto, listo para json.dump                                    #
# --------------------------------------------------------------------------- #

def _resumen(ev, jugadores):
    """Resumen corto: nº de eventos, turnos (NEW_TURN) y ganador (MSG_WIN)."""
    turnos = sum(1 for m in ev if m["id"] == 40)
    win = next((m for m in reversed(ev) if m["id"] == 5), None)
    ganador = win["winner"] if win else None
    return {
        "eventos": len(ev),
        "turnos": turnos,
        "ganador": ganador,
        "ganador_nombre": (_nombre_equipo(ganador, jugadores)
                           if ganador is not None else None),
        "motivo_win": (WIN_REASONS.get(win["reason"], win["reason"])
                       if win else None),
    }


def a_json(rep, incluir_analisis=False, nombres=None):
    """Convierte un :class:`Replay` en un dict JSON-serializable completo.

    Incluye header (formato/flag/seeds/params...), jugadores, decks (códigos),
    eventos del core, timeline y resumen; ``analisis`` solo si
    ``incluir_analisis``. ``nombres`` (opcional) es {code: nombre} y se emite
    como mapa ``nombres_cartas`` además de usarse en timeline/análisis.
    Las respuestas del clásico se emiten como cadenas hex.
    """
    ev = eventos(rep)
    tl = timeline(ev, rep.params, rep.decks, rep.jugadores, nombres)
    datos = {
        "formato": rep.formato,
        "version": rep.version,
        "flag": rep.flag,
        "flags_activos": list(rep.flags_activos),
        "timestamp": rep.timestamp,
        "fecha": rep.fecha,
        "hash": rep.hash,
        "seeds": rep.seeds,
        "comprimido": rep.comprimido,
        "datasize": rep.datasize,
        "header_version": rep.header_version,
        "params": dict(rep.params),
        "jugadores": list(rep.jugadores),
        "ai_name": rep.ai_name,
        "decks": [{"main": list(d["main"]), "extra": list(d["extra"])}
                  for d in rep.decks],
        "reglas_custom": list(rep.reglas_custom),
        "respuestas": [bytes(r).hex() for r in rep.respuestas],
        "nombres_cartas": dict(nombres) if nombres else {},
        "eventos": ev,
        "timeline": tl,
        "resumen": _resumen(ev, rep.jugadores),
    }
    if incluir_analisis:
        datos["analisis"] = analizar(ev, tl, rep.decks, params=rep.params,
                                     jugadores=rep.jugadores, nombres=nombres)
    return datos


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv=None):
    """CLI: ``edopro-replay2json <replay.yrp[.X]> [-o salida.json] [--analisis]
    [--cdb ruta.cdb] [--online]``. Sin -o, escribe junto al original .json."""
    import argparse

    p = argparse.ArgumentParser(
        prog="edopro-replay2json",
        description="Extrae replays de Yu-Gi-Oh! EDOPro (.yrp / .yrpX) a JSON.")
    p.add_argument("replay", help="replay .yrp o .yrpX de EDOPro")
    p.add_argument("-o", "--salida", default=None,
                   help="JSON de salida (default: junto al original, .json)")
    p.add_argument("--analisis", action="store_true",
                   help="incluye análisis (activaciones, negaciones, ataques, "
                        "combos y estadísticas)")
    p.add_argument("--cdb", default=None, metavar="RUTA.cdb",
                   help="cards.cdb para resolver nombres de cartas (offline)")
    p.add_argument("--online", action="store_true",
                   help="resolver nombres faltantes via API pública de "
                        "YGOPRODeck (requiere red; jamás es automático)")
    p.add_argument("--indent", type=int, default=2,
                   help="sangría del JSON (default 2; 0 = compacto)")
    args = p.parse_args(argv)

    try:
        rep = leer_replay(args.replay)
    except (ReplayError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    nombres = None
    if args.cdb or args.online:
        try:
            codes = _codes_del_replay(rep)
            nombres = {}
            if args.cdb:
                nombres.update(nombres_desde_cdb(args.cdb, codes))
            faltan = [c for c in codes if c not in nombres]
            if args.online and faltan:
                nombres.update(nombres_online(faltan))
        except (ReplayError, OSError, sqlite3.Error) as e:
            print(f"aviso: nombres de cartas sin resolver ({e}); "
                  f"se emiten solo códigos", file=sys.stderr)
            nombres = None

    datos = a_json(rep, incluir_analisis=args.analisis, nombres=nombres)
    indent = args.indent or None
    texto = json.dumps(datos, ensure_ascii=False, indent=indent)
    salida = Path(args.salida) if args.salida else Path(args.replay).with_suffix(".json")
    salida.write_text(texto + "\n", encoding="utf-8")

    r = datos["resumen"]
    ganador = r.get("ganador_nombre") or "?"
    print(f"{rep.formato} · eventos={r['eventos']} · turnos={r['turnos']} · "
          f"ganador={ganador} -> {salida}")
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
