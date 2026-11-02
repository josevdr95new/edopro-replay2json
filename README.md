# edopro-replay2json

[![PyPI](https://img.shields.io/pypi/v/edopro-replay2json)](https://pypi.org/project/edopro-replay2json/)
[![Python](https://img.shields.io/pypi/pyversions/edopro-replay2json)](https://pypi.org/project/edopro-replay2json/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Extrae **replays de Yu-Gi-Oh! EDOPro** (`.yrp` clásico y `.yrpX` extendido) a **JSON**:
header, jugadores, decks (códigos), eventos del core (`ocgcore` v11), timeline por
turno/fase y análisis opcional (activaciones, **negaciones**, ataques, combos, estadísticas).

- **Módulo único y autocontenido**: solo stdlib (`struct`, `lzma`, `sqlite3`, `urllib`, `json`).
  Cero dependencias. Funciona en Python 3.9 → 3.14 (64 y 32 bits).
- **Formatos**: `.yrp` (header 32 B + decks + respuestas), `.yrpX` (header extendido 72 B
  con seeds, stream de packets del core, LZMA con padding, nombre de IA via packet 163,
  `.yrp` clásico embebido via packet 231) y **el par combinado** (`.yrpX` + `.yrp` hermano).
- **Nombres de cartas opcionales**: por defecto solo códigos (offline); `--cdb` lee un
  `cards.cdb` con sqlite3; `--online` consulta la API pública de YGOPRODeck (jamás automático).

## Instalación

```bash
pip install edopro-replay2json
```

## Uso CLI

```bash
# JSON junto al original (duelo.yrpX -> duelo.json)
edopro-replay2json "C:\ProjectIgnis\replay\_LastReplay.yrpX"

# Con salida explícita y análisis
edopro-replay2json duelo.yrpX -o salida.json --analisis

# Nombres de cartas offline desde la DB de EDOPro
edopro-replay2json duelo.yrpX --cdb "C:\ProjectIgnis\expansions\cards.cdb"

# Nombres faltantes via API pública (requiere red; opt-in)
edopro-replay2json duelo.yrpX --cdb cards.cdb --online
```

## Uso como librería

```python
from edopro_replay2json import leer_replay, eventos, timeline, analizar, a_json

rep = leer_replay("duelo.yrpX")     # .yrp, .yrpX o el par combinado
rep.formato        # 'yrpX'
rep.jugadores      # ['Yo', 'Iron Man']
rep.decks          # [{'main': [codes], 'extra': [codes]}, ...]
rep.params         # {'start_lp': 8000, 'start_hand': 5, 'draw_count': 1, 'duel_flags': ...}

ev = eventos(rep)                   # mensajes del core -> lista de dicts
tl = timeline(ev, rep.params, rep.decks, rep.jugadores)
an = analizar(ev, tl, rep.decks, jugadores=rep.jugadores)

datos = a_json(rep, incluir_analisis=True)   # todo junto, listo para json.dump
```

## Ejemplo de esquema JSON (resumido)

```jsonc
{
  "formato": "yrpX",
  "version": 9007,
  "flag": 545,                      // entero de flags del header
  "flags_activos": ["COMPRESSED", "NEWREPLAY", "EXTENDED_HEADER"],
  "timestamp": 1762000000,
  "fecha": "2025-11-01T12:26:40+00:00",
  "hash": 305419896,
  "seeds": [1, 2, 3, 4],            // solo header extendido (yrpX)
  "params": {"start_lp": 8000, "start_hand": 5, "draw_count": 1, "duel_flags": 12948},
  "jugadores": ["Jose", "Iron Man"],
  "decks": [{"main": [65470086], "extra": [44508094]}],
  "respuestas": ["0400..."],        // hex (solo yrp clásico)
  "nombres_cartas": {"14558127": "Ash Blossom & Joyous Spring"},
  "eventos": [                      // mensajes del core, en orden
    {"id": 4, "player": 0, "lp0": 8000, "lp1": 8000, "deck0": 40, ...},
    {"id": 40, "player": 0},        // NEW_TURN
    {"id": 70, "code": 14558127, "chaincount": 2, ...},   // CHAINING
    {"id": 200, "skip": true}       // desconocido: frame saltado sin romper el stream
  ],
  "timeline": [{
    "turno": 1, "jugador": 0, "nombre_jugador": "Jose",
    "fases": [{"fase": 4, "fase_str": "Main 1", "acciones": [
      {"tipo": "roba", "equipo": 0, "cartas": ["#111"]},
      {"tipo": "activa", "carta": "Ash Blossom & Joyous Spring", ...},
      {"tipo": "niega_activacion", "code_intentado": 12345, "negador": "Ash Blossom & Joyous Spring"},
      {"tipo": "ataca", "objetivo": "directo"},
      {"tipo": "batalla", "atk_atacante": 1900, "danio": 1900},
      {"tipo": "victoria", "motivo_str": "LP llegaron a 0"}
    ]}],
    "campo_final": {"lp_0": 6100, "mano_0": 3, "monstruos_0": [...], ...}
  }],
  "resumen": {"eventos": 1343, "turnos": 9, "ganador": 1, "ganador_nombre": "Iron Man",
              "motivo_win": "LP llegaron a 0"},
  "analisis": {                     // solo con --analisis
    "activaciones": [...], "negaciones": [{"negador_es_conocido": true, ...}],
    "ataques": [...], "combos": [{"secuencia": [...], "frecuencia": 3}],
    "estadisticas": [{"jugador": "Jose", "lp_final": 0, "cartas_mas_usadas": [...]}],
    "resumen": {...}
  }
}
```

## Notas de formato (por qué funciona)

La especificación se tomó del fuente C++ oficial (EDOPro `gframe/replay.h/.cpp`, core
`ocgcore` v11 y el parser de referencia de DyXel) y el lector es un port de una
implementación probada contra replays reales, con los casos raros ya resueltos:

- LZMA1 raw con las props del header; la salida puede traer **padding** que excede
  `datasize` → se recorta como hace el core; si sale MENOS que `datasize`, se rechaza.
- Header extendido de 72 B solo si `flag & 0x200`; nombres UTF-16LE de 40 B; `duel_flags`
  de 32/64 bits según `flag & 0x100`; decks/reglas solo en el clásico (NEWREPLAY).
- Packets especiales del yrpX: `163` = nombre del rival (UTF-8), `231` = `.yrp` clásico
  embebido (fuente de decks/respuestas); `40` (NEW_TURN) numeriza los turnos.
- Mensajes del core desconocidos → `{"id": .., "skip": true}` **sin romper el framing**
  (tolerancia a cores futuros).

## English (short)

Extracts Yu-Gi-Oh! **EDOPro replays** (`.yrp` / `.yrpX`) to **JSON**: header, players,
decks (card codes), core message events, per-turn timeline and optional analysis
(activations, negations, attacks, combos, stats). Single self-contained module,
**stdlib only**, Python 3.9–3.14. Card names are optional (offline `cards.cdb` via
sqlite3, or opt-in YGOPRODeck API). CLI: `edopro-replay2json duelo.yrpX -o out.json --analisis`.

## Publicar (dueño del paquete)

Ver [PUBLISH.md](PUBLISH.md) — build + `twine check` + upload a PyPI (y TestPyPI).

## Limitaciones

- El `.yrp` clásico no trae stream de mensajes: `eventos()` devuelve `[]` (para eventos
  se necesita el `.yrpX`, o el par).
- Los nombres via `--cdb` no resuelven alias de artes alternativos (cartas con `alias`).
- El análisis de negaciones etiqueta `negador_es_conocido` con `NEGADORES_CONOCIDOS`
  (diccionario público ampliable); detecta cualquier cadena negada aunque el negador
  no esté en la lista.
- Se usa el protocolo del core v11 de EDOPro; mensajes de cores futuros no implementados
  se saltan sin romper el framing.
