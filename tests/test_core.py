# -*- coding: utf-8 -*-
"""Tests del paquete edopro_replay2json (unittest stdlib, sin dependencias).

Genera replays SINTÉTICOS válidos (.yrp clásico y .yrpX extendido, con y sin
LZMA/padding) siguiendo la misma especificación que el lector (header 32/72 B,
nombres UTF-16LE de 40 B, decks, respuestas, packets del core v11), verifica el
round-trip, el padding LZMA de EDOPro, los errores de magic y el framing de
mensajes desconocidos. La validación contra replays REALES se hace por CLI
(ver README/PUBLISH).
"""
import json
import lzma
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import edopro_replay2json as er2j

# ---- ids para los builders -------------------------------------------------
YRP1 = 0x31707279
YRPX = 0x58707279
TS = 1700000000                      # 2023-11-14T22:13:20+00:00


# --------------------------------------------------------------------------- #
#  Builders de replays sintéticos (espejo exacto del lector)                   #
# --------------------------------------------------------------------------- #

def header(id_magic, version=9007, flag=0, timestamp=TS, datasize=0,
           hash_=0xDEADBEEF, seeds=(1, 2, 3, 4), header_version=1):
    """Header de replay: 32 bytes base + 40 de extensión si flag & 0x200.

    props LZMA de 5 bytes reales: 0x5D (lc=3,lp=0,pb=2) + dict 1<<24 LE.
    """
    b = struct.pack("<IIIIII", id_magic, version, flag, timestamp, datasize,
                    hash_)
    b += b"\x5d" + struct.pack("<I", 1 << 24) + b"\x00\x00\x00"   # props[8]
    if flag & 0x200:
        b += struct.pack("<Q", header_version) + struct.pack("<4Q", *seeds)
    return b


def nombre(s):
    """Campo de nombre: 40 bytes UTF-16LE terminados en NUL."""
    return s.encode("utf-16-le")[:40].ljust(40, b"\x00")


def loc(con, l, seq, pos=0):
    """loc_info del core: con u8, loc u8, seq u32, pos u32."""
    return struct.pack("<BBII", con, l, seq, pos)


def pkt(pid, payload):
    """Framing de packets del replay yrpX: [u8 id][u32 len][payload]."""
    return struct.pack("<BI", pid, len(payload)) + bytes(payload)


def frame(pid, payload):
    """Framing de red del core: [u32 tamaño][u8 id][payload], tamaño=len+1."""
    return struct.pack("<IB", len(payload) + 1, pid) + bytes(payload)


def cuerpo_clasico(flag=0, nombres=("Alice", "Bob"), lp=8000, hand=5, draw=1,
                   duel_flags=0x20, decks=(([1, 2, 3], [7]), ([9], [8, 9, 10])),
                   respuestas=(b"\xaa", b"\xbb\xcc"), reglas=(5,)):
    """Cuerpo de un .yrp clásico: nombres + params + decks + reglas + respuestas."""
    b = b""
    for i in range(2):
        if flag & 0x20:                      # NEWREPLAY: conteo u32 por lado
            b += struct.pack("<I", 1)
        b += nombre(nombres[i])
    b += struct.pack("<III", lp, hand, draw)
    b += struct.pack("<Q" if flag & 0x100 else "<I", duel_flags)
    for main, extra in decks:
        b += struct.pack("<I", len(main)) + struct.pack(f"<{len(main)}I", *main)
        b += struct.pack("<I", len(extra)) + struct.pack(f"<{len(extra)}I", *extra)
    if flag & 0x20 and not flag & 0x40:      # reglas custom del NEWREPLAY
        b += struct.pack("<I", len(reglas)) + struct.pack(f"<{len(reglas)}I", *reglas)
    for r in respuestas:
        b += bytes([len(r)]) + r
    b += b"\x00"                              # terminador de respuestas
    return b


def packets_duelo():
    """Packets del core para un mini-duelo sintético yrpX.

    START(4) + NEW_TURN(40) + NEW_PHASE(41) + DRAW(90) + CHAINING(70)×2 +
    CHAIN_DISABLED(76) + WIN(5): el equipo 1 niega con Ash Blossom y el
    equipo 0 gana por LP a 0.
    """
    out = b""
    out += pkt(4, struct.pack("<BiiHHHH", 0, 8000, 8000, 5, 2, 5, 2))
    out += pkt(40, b"\x00")                    # turno del equipo 0
    out += pkt(41, struct.pack("<H", 0x04))    # Main 1
    out += pkt(90, struct.pack("<BI", 0, 2)
               + struct.pack("<II", 111, 1) + struct.pack("<II", 222, 1))
    # cadena 1: equipo 0 activa #12345 desde la mano
    out += pkt(70, struct.pack("<I", 12345) + loc(0, 0x02, 3, 0x1)
               + struct.pack("<BBIQI", 0, 0, 0, 0, 1))
    # cadena 2: equipo 1 encadena Ash Blossom (14558127)
    out += pkt(70, struct.pack("<I", 14558127) + loc(1, 0x02, 2, 0x1)
               + struct.pack("<BBIQI", 0, 0, 0, 0, 2))
    out += pkt(76, b"\x01")                    # cadena 1 deshabilitada
    out += pkt(5, bytes([0, 1]))               # gana equipo 0 (LP a 0)
    return out


def archivo_yrpx(con_lzma=False, packets=None, nombres=("Alice", "Bob"),
                 duel_flags=0x338C):
    """Escribe un .yrpX sintético completo y devuelve (bytes, cuerpo)."""
    cuerpo = nombre(nombres[0]) + nombre(nombres[1])
    cuerpo += struct.pack("<I", duel_flags)    # yrpX: solo duel_flags
    cuerpo += packets if packets is not None else packets_duelo()
    if con_lzma:
        # padding de EDOPro: 3 bytes de relleno que el core no consume
        datos = cuerpo + b"\x00\x00\x00"
        filtros = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 24,
                    "lc": 3, "lp": 0, "pb": 2}]
        comp = lzma.compress(datos, format=lzma.FORMAT_RAW, filters=filtros)
        return header(YRPX, flag=0x200 | 0x1, datasize=len(cuerpo)) + comp, cuerpo
    return header(YRPX, flag=0x200) + cuerpo, cuerpo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def escribir(self, nombre_arch, datos):
        ruta = self.dir / nombre_arch
        ruta.write_bytes(datos)
        return ruta


# --------------------------------------------------------------------------- #
#  1) Round-trip .yrp clásico                                                  #
# --------------------------------------------------------------------------- #

class TestYrpClasico(Base):
    def test_roundtrip_basico(self):
        ruta = self.escribir("duelo.yrp",
                             header(YRP1) + cuerpo_clasico())
        rep = er2j.leer_replay(ruta)
        self.assertEqual(rep.formato, "yrp")
        self.assertEqual(rep.version, 9007)
        self.assertEqual(rep.jugadores, ["Alice", "Bob"])
        self.assertEqual(rep.params["start_lp"], 8000)
        self.assertEqual(rep.params["start_hand"], 5)
        self.assertEqual(rep.params["draw_count"], 1)
        self.assertEqual(rep.params["duel_flags"], 0x20)
        self.assertEqual(rep.decks[0], {"main": [1, 2, 3], "extra": [7]})
        self.assertEqual(rep.decks[1], {"main": [9], "extra": [8, 9, 10]})
        self.assertEqual(rep.respuestas, [b"\xaa", b"\xbb\xcc"])
        self.assertIsNone(rep.stream)
        self.assertEqual(er2j.eventos(rep), [])   # el clásico no trae eventos
        self.assertIn("2023-11-14", rep.fecha)

    def test_newreplay_y_64bit(self):
        flag = 0x20 | 0x100
        ruta = self.escribir(
            "nr.yrp", header(YRP1, flag=flag)
            + cuerpo_clasico(flag=flag, duel_flags=0x123456789ABCDEF0,
                             respuestas=(b"\x05",), reglas=(7, 8)))
        rep = er2j.leer_replay(ruta)
        self.assertIn("NEWREPLAY", rep.flags_activos)
        self.assertIn("64BIT_DUELFLAG", rep.flags_activos)
        self.assertEqual(rep.params["duel_flags"], 0x123456789ABCDEF0)
        self.assertEqual(rep.reglas_custom, [7, 8])
        self.assertEqual(rep.respuestas, [b"\x05"])

    def test_yrp_comprimido(self):
        cuerpo = cuerpo_clasico()
        filtros = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 24,
                    "lc": 3, "lp": 0, "pb": 2}]
        comp = lzma.compress(cuerpo, format=lzma.FORMAT_RAW, filters=filtros)
        ruta = self.escribir("c.yrp",
                             header(YRP1, flag=0x1, datasize=len(cuerpo)) + comp)
        rep = er2j.leer_replay(ruta)
        self.assertTrue(rep.comprimido)
        self.assertIn("COMPRESSED", rep.flags_activos)
        self.assertEqual(rep.decks[0], {"main": [1, 2, 3], "extra": [7]})


# --------------------------------------------------------------------------- #
#  2) .yrpX sintético: eventos, timeline y análisis                            #
# --------------------------------------------------------------------------- #

class TestYrpX(Base):
    def test_eventos_timeline_analisis(self):
        ruta = self.escribir("duelo.yrpX", archivo_yrpx()[0])
        rep = er2j.leer_replay(ruta)
        self.assertEqual(rep.formato, "yrpX")
        self.assertIn("EXTENDED_HEADER", rep.flags_activos)
        self.assertEqual(rep.seeds, [1, 2, 3, 4])
        self.assertEqual(rep.decks, [])            # sin 231 ni hermano: sin decks

        ev = er2j.eventos(rep)
        self.assertEqual(len(ev), 8)
        self.assertEqual(ev[0]["id"], 4)           # START
        self.assertEqual(ev[0]["lp0"], 8000)
        self.assertEqual(ev[1], {"id": 40, "player": 0})
        self.assertEqual(ev[2]["phase"], 0x04)
        self.assertEqual(ev[3]["player"], 0)       # DRAW
        self.assertEqual([c["code"] for c in ev[3]["cards"]], [111, 222])
        self.assertEqual(ev[6]["chaincount"], 1)   # CHAIN_DISABLED
        self.assertEqual(ev[7]["winner"], 0)

        tl = er2j.timeline(ev, rep.params, rep.decks, rep.jugadores)
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["turno"], 1)
        self.assertEqual(tl[0]["jugador"], 0)
        tipos = [a["tipo"] for f in tl[0]["fases"] for a in f["acciones"]]
        self.assertEqual(tipos, ["roba", "activa", "activa",
                                 "niega_activacion", "victoria"])
        niega = next(a for a in tl[0]["fases"][0]["acciones"]
                     if a["tipo"] == "niega_activacion")
        self.assertEqual(niega["code_intentado"], 12345)
        self.assertEqual(niega["code_negador"], 14558127)
        self.assertIsNotNone(tl[0]["campo_final"])

        an = er2j.analizar(ev, tl, rep.decks, jugadores=rep.jugadores)
        self.assertEqual(len(an["activaciones"]), 2)
        self.assertEqual(len(an["negaciones"]), 1)
        neg = an["negaciones"][0]
        self.assertEqual(neg["code_intentado"], 12345)
        self.assertEqual(neg["code_negador"], 14558127)
        self.assertTrue(neg["negador_es_conocido"])
        self.assertEqual(neg["negador"], "#14558127")   # sin nombres: códigos
        self.assertEqual(an["resumen"]["ganador"], 0)
        self.assertEqual(an["resumen"]["turnos"], 1)

    def test_negadores_conocidos_canonicos(self):
        for code, nombre_esperado in (
                (14558127, "Ash Blossom & Joyous Spring"),
                (23434538, 'Maxx "C"'),
                (59438930, "Ghost Ogre & Snow Rabbit"),
                (10045474, "Infinite Impermanence"),
                (97268675, "Effect Veiler"),
                (81439173, "Solemn Judgment"),
                (84749824, "Solemn Warning"),
                (40605147, "Solemn Strike")):
            self.assertEqual(er2j.NEGADORES_CONOCIDOS[code], nombre_esperado)


# --------------------------------------------------------------------------- #
#  3) LZMA: padding de EDOPro, variante .lzma y truncado                       #
# --------------------------------------------------------------------------- #

class TestLzma(Base):
    def test_padding_se_recorta(self):
        datos, cuerpo = archivo_yrpx(con_lzma=True)
        self.assertEqual(len(cuerpo) + 3, len(cuerpo) + 3)
        ruta = self.escribir("pad.yrpX", datos)
        rep = er2j.leer_replay(ruta)
        # stream recortado EXACTO a datasize (padding de 3 bytes fuera)
        self.assertEqual(rep.stream, cuerpo)
        self.assertEqual(len(er2j.eventos(rep)), 8)

    def test_variante_alone_con_end_marker(self):
        cuerpo = (nombre("Alice") + nombre("Bob") + struct.pack("<I", 0x10)
                  + packets_duelo())
        comp = lzma.compress(cuerpo, format=lzma.FORMAT_ALONE)
        props = comp[:5]            # props REALES del compresor (byte+dict)
        stream = comp[13:]          # .lzma = props(1)+dict(4)+size(8)+stream
        h = bytearray(header(YRPX, flag=0x200 | 0x1, datasize=len(cuerpo)))
        h[24:29] = props            # el header base declara las MISMAS props
        ruta = self.escribir("alone.yrpX", bytes(h) + stream)  # el cuerpo ya
        # NO lleva props: solo el stream tras el header de 13 bytes del .lzma
        rep = er2j.leer_replay(ruta)
        self.assertEqual(rep.stream, cuerpo)
        self.assertEqual(len(er2j.eventos(rep)), 8)

    def test_truncado_se_rechaza(self):
        # declaramos datasize mayor que lo que el stream puede dar
        datos, cuerpo = archivo_yrpx(con_lzma=True)
        h = bytearray(datos[:72])
        struct.pack_into("<I", h, 16, len(cuerpo) + 10)   # datasize en offset 16
        ruta = self.escribir("trunc.yrpX", bytes(h) + datos[72:])
        with self.assertRaises(er2j.ReplayError):
            er2j.leer_replay(ruta)


# --------------------------------------------------------------------------- #
#  4) Errores de magic / headers truncados                                     #
# --------------------------------------------------------------------------- #

class TestErrores(Base):
    def test_magic_desconocido(self):
        ruta = self.escribir("malo.yrp", b"NOPE" + b"\x00" * 28)
        with self.assertRaises(er2j.ReplayError):
            er2j.leer_replay(ruta)

    def test_archivo_corto(self):
        ruta = self.escribir("corto.yrp", b"\x00" * 10)
        with self.assertRaises(er2j.ReplayError):
            er2j.leer_replay(ruta)

    def test_header_extendido_truncado(self):
        ruta = self.escribir("ext.yrpX", header(YRPX, flag=0x200)[:40])
        with self.assertRaises(er2j.ReplayError):
            er2j.leer_replay(ruta)


# --------------------------------------------------------------------------- #
#  5) Framing: mensajes desconocidos no rompen el stream                       #
# --------------------------------------------------------------------------- #

class TestFraming(Base):
    def test_id_desconocido_skip(self):
        buf = frame(200, b"\x01\x02") + frame(5, bytes([1, 0]))
        ev = er2j.eventos(buf)
        self.assertEqual(ev[0], {"id": 200, "skip": True})
        self.assertEqual(ev[1], {"id": 5, "winner": 1, "reason": 0})

    def test_stream_directo_bytes(self):
        ev = er2j.eventos(frame(40, b"\x01") + frame(41, struct.pack("<H", 0x200)))
        self.assertEqual(ev[0]["player"], 1)
        self.assertEqual(ev[1]["phase"], 0x200)


# --------------------------------------------------------------------------- #
#  6) El par .yrpX + .yrp (hermano en disco y 231 embebido)                    #
# --------------------------------------------------------------------------- #

class TestPar(Base):
    def test_hermano_en_disco(self):
        # yrpX SIN decks (sin 231) + hermano .yrp con decks
        yrpx = self.escribir("par.yrpX", archivo_yrpx()[0])
        clasico = header(YRP1) + cuerpo_clasico(nombres=("Ana", "Luis"))
        self.escribir("par.yrp", clasico)
        rep = er2j.leer_replay(yrpx)
        self.assertEqual(rep.decks[0], {"main": [1, 2, 3], "extra": [7]})
        self.assertEqual(rep.jugadores, ["Ana", "Luis"])   # nombres del clásico
        self.assertEqual(rep.params["start_lp"], 8000)     # params del clásico
        self.assertEqual(len(er2j.eventos(rep)), 8)        # eventos del yrpX

    def test_embebido_231(self):
        clasico = header(YRP1) + cuerpo_clasico(nombres=("Ana", "Luis"))
        packets = pkt(231, clasico) + packets_duelo()
        ruta = self.escribir("emb.yrpX",
                             archivo_yrpx(packets=packets,
                                          nombres=("Ana", "Luis"))[0])
        rep = er2j.leer_replay(ruta)
        self.assertEqual(rep.decks[0], {"main": [1, 2, 3], "extra": [7]})
        self.assertEqual(rep.respuestas, [b"\xaa", b"\xbb\xcc"])
        ev = er2j.eventos(rep)
        self.assertEqual(len(ev), 8)        # el 231 NO es un evento
        self.assertTrue(all(m["id"] != 231 for m in ev))


# --------------------------------------------------------------------------- #
#  7) Nombres desde .cdb (sqlite3 stdlib)                                      #
# --------------------------------------------------------------------------- #

class TestCdb(Base):
    def test_nombres_desde_cdb(self):
        cdb = self.dir / "cards.cdb"
        con = sqlite3.connect(str(cdb))
        con.execute("CREATE TABLE datas (id INTEGER PRIMARY KEY, ot INTEGER, "
                    "alias INTEGER, setcode INTEGER, type INTEGER, atk INTEGER, "
                    "def INTEGER, level INTEGER, race INTEGER, attribute INTEGER)")
        cols = ", ".join(f"str{i} TEXT" for i in range(1, 17))
        con.execute(f"CREATE TABLE texts (id INTEGER PRIMARY KEY, name TEXT, "
                    f"desc TEXT, {cols})")
        con.execute("INSERT INTO datas VALUES (14558127, 3, 0, 0, 0, 0, 0, 0, 0, 0)")
        con.execute("INSERT INTO texts (id, name) VALUES (14558127, "
                    "'Ash Blossom & Joyous Spring')")
        con.commit()
        con.close()
        nombres = er2j.nombres_desde_cdb(cdb, [14558127, 99999999])
        self.assertEqual(nombres, {14558127: "Ash Blossom & Joyous Spring"})


# --------------------------------------------------------------------------- #
#  8) a_json + CLI end-to-end                                                  #
# --------------------------------------------------------------------------- #

class TestAJsonYCli(Base):
    def test_a_json_serializable(self):
        ruta = self.escribir("duelo.yrpX", archivo_yrpx()[0])
        rep = er2j.leer_replay(ruta)
        datos = er2j.a_json(rep, incluir_analisis=True)
        texto = json.dumps(datos, ensure_ascii=False)   # no debe lanzar
        self.assertIn('"analisis"', texto)
        self.assertEqual(datos["resumen"]["eventos"], 8)
        self.assertEqual(datos["resumen"]["ganador"], 0)
        self.assertEqual(datos["decks"], [])
        self.assertIsInstance(datos["eventos"], list)
        self.assertIsInstance(datos["timeline"], list)

    def test_a_json_con_nombres(self):
        ruta = self.escribir("duelo.yrpX", archivo_yrpx()[0])
        rep = er2j.leer_replay(ruta)
        datos = er2j.a_json(rep, nombres={14558127: "Ash Blossom & Joyous Spring"})
        self.assertEqual(datos["nombres_cartas"][14558127],
                         "Ash Blossom & Joyous Spring")

    def test_cli_con_o_y_analisis(self):
        yrpx = self.escribir("duelo.yrpX", archivo_yrpx()[0])
        salida = self.dir / "salida.json"
        rc = er2j.main([str(yrpx), "-o", str(salida), "--analisis"])
        self.assertEqual(rc, 0)
        datos = json.loads(salida.read_text(encoding="utf-8"))
        self.assertEqual(datos["resumen"]["eventos"], 8)
        self.assertEqual(datos["resumen"]["ganador_nombre"], "Alice")
        self.assertIn("analisis", datos)

    def test_cli_salida_junto_al_original(self):
        yrpx = self.escribir("otro.yrpX", archivo_yrpx()[0])
        rc = er2j.main([str(yrpx)])
        self.assertEqual(rc, 0)
        esperado = yrpx.with_suffix(".json")     # otro.yrpX -> otro.json
        self.assertTrue(esperado.exists())
        datos = json.loads(esperado.read_text(encoding="utf-8"))
        self.assertEqual(datos["formato"], "yrpX")

    def test_cli_yrp_clasico(self):
        yrp = self.escribir("clasico.yrp", header(YRP1) + cuerpo_clasico())
        salida = self.dir / "clasico.json"
        rc = er2j.main([str(yrp), "-o", str(salida)])
        self.assertEqual(rc, 0)
        datos = json.loads(salida.read_text(encoding="utf-8"))
        self.assertEqual(datos["decks"][0]["main"], [1, 2, 3])
        self.assertEqual(datos["respuestas"], ["aa", "bbcc"])
        self.assertEqual(datos["eventos"], [])

    def test_cli_replay_malo(self):
        malo = self.escribir("malo.yrp", b"XXXX" + b"\x00" * 28)
        rc = er2j.main([str(malo)])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
