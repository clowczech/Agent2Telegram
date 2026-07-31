#!/usr/bin/env python3
"""Denní rozbor provozu bridge – měří SKUTEČNÉ konverzace, ne umělou kontrolní zprávu.

Petr 2026-07-31 odmítl kanárka s dobrým argumentem: posílá pořád tutéž krátkou zprávu, takže
projde i ve chvíli, kdy je rozbité všechno zajímavé. Skutečný provoz je tvrdší test – dlouhé
odpovědi na části, přílohy, hlasovky, turny na deset minut, zprávy uprostřed rozdělané práce.

Nejcennější detektor je Petr sám: když napíše „jsi tam?" nebo „nedostal jsem odpověď", je to
přiznaný výpadek. Ty se dají v logu najít a spárovat s tím, co se v tu chvíli dělo.

Spouštět denně; výstup posílat přes notify_petr.sh (turn z cronu nemá cestu na Telegram
přes `[tg]` – ověřeno 31. 7., kdy Petr čekal 50 minut na zprávu, která se zahodila).

Použití:
    python3 denni_rozbor.py [--log CESTA] [--den YYYY-MM-DD] [--dny N]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

LOG = os.path.expanduser("~/.claude/telegram-bridge/logs/agent2telegram.out.log")
STATE = os.path.expanduser("~/.local/state/agent2telegram")

TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s+(\w+)\s+")
TURN_START = "TURN START"
TURN_END = "TURN END"
FWD = "FWD (send)"

# Fráze, kterými Petr hlásí, že něco nedorazilo. Tohle je nejpřesnější měřítko, jaké máme –
# je to jeho vlastní zkušenost, ne naše metrika.
STIZNOSTI = [
    "jsi tam", "nedostal jsem", "nepřišla", "neprisla", "nechodí", "nechodi",
    "nereaguješ", "nereagujes", "žiješ", "zijes", "uběhlo", "ubehlo",
    "se neozval", "nedorazil", "pracuješ?", "pracujes?", "tak co",
]


def _cas(radek: str):
    m = TS.match(radek)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + s


def nacti(cesta: str) -> list[str]:
    try:
        with open(cesta, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError as e:
        print(f"log nelze číst: {e}", file=sys.stderr)
        return []


def rozbor(radky: list[str]) -> dict:
    """Log nemá datum, jen čas – proto se počítá přes celý předaný úsek.
    Volající si vybere, kolik ho zajímá (typicky poslední den)."""
    v = {
        "turnu": 0, "odpovedi": 0, "turn_bez_odpovedi": 0,
        "inject_selhal": 0, "inject_po_opakovani": 0,
        "sit_vypadky": 0, "backstop": 0, "vzdane_prilohy": 0,
        "nejdelsi_turn": 0.0, "turny_nad_2min": 0,
        "restarty": 0, "duplicity_fronta": 0,
    }
    delky = []
    start = None
    videl_fwd = False
    for r in radky:
        if TURN_START in r:
            if start is not None and not videl_fwd:
                v["turn_bez_odpovedi"] += 1
            v["turnu"] += 1
            start = _cas(r)
            videl_fwd = False
        elif FWD in r:
            v["odpovedi"] += 1
            videl_fwd = True
        elif TURN_END in r:
            m = re.search(r"dur=([\d.]+)s", r)
            if m:
                d = float(m.group(1))
                delky.append(d)
                v["nejdelsi_turn"] = max(v["nejdelsi_turn"], d)
                if d > 120:
                    v["turny_nad_2min"] += 1
            if not videl_fwd:
                v["turn_bez_odpovedi"] += 1
            start = None
        elif "inject failed" in r:
            v["inject_selhal"] += 1
        elif "inject prošel až na" in r:
            v["inject_po_opakovani"] += 1
        elif "Attach bridge live" in r:
            v["restarty"] += 1
        elif "TURN END backstop" in r:
            v["backstop"] += 1
        elif "se vzdala po" in r:
            v["vzdane_prilohy"] += 1
        elif "re-delivery still failing" in r or "stále selhává" in r:
            v["duplicity_fronta"] += 1
        elif "urlopen error" in r or "Connection reset" in r:
            v["sit_vypadky"] += 1
    if delky:
        delky.sort()
        v["median_turn"] = delky[len(delky) // 2]
        v["p90_turn"] = delky[int(len(delky) * 0.9)]
    return v


def dead_letter() -> tuple[int, list[str]]:
    """Počet nedoručených zpráv = nejtvrdší číslo, jaké máme."""
    zaznamy = []
    for koren, _dirs, soubory in os.walk(os.path.join(STATE, "dead-letter")):
        for s in soubory:
            if s.endswith(".json"):
                zaznamy.append(os.path.join(koren, s))
    return len(zaznamy), zaznamy[:5]


def stiznosti(radky: list[str]) -> list[str]:
    """Petrovy vlastní hlášky o tom, že něco nedorazilo."""
    nalezene = []
    for r in radky:
        nizky = r.lower()
        if "inject" not in nizky and "fwd" not in nizky:
            continue
        for fraze in STIZNOSTI:
            if fraze in nizky:
                nalezene.append(r.strip()[:110])
                break
    return nalezene


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default=LOG)
    p.add_argument("--radku", type=int, default=4000,
                   help="kolik posledních řádků logu brát (log nemá datum)")
    a = p.parse_args()

    radky = nacti(a.log)[-a.radku:]
    if not radky:
        print("PRÁZDNÝ LOG – nemám co měřit (samo o sobě podezřelé)")
        return 1

    v = rozbor(radky)
    dl_pocet, dl_ukazky = dead_letter()
    sti = stiznosti(radky)

    # `turn_bez_odpovedi` ZÁMĚRNĚ nespouští poplach: log nerozlišuje, jestli turn přišel
    # z Telegramu, nebo z terminálu – a terminálový turn odpověď na Telegram mít nemá.
    # Falešný poplach je horší než žádná metrika: pár dní a nikdo mu nevěří.
    # Zůstává jako orientační číslo, poplach spouštějí jen tvrdé důkazy ztráty.
    problem = (dl_pocet or v["vzdane_prilohy"] or v["inject_selhal"] or sti
               or v["restarty"] > 2)

    print("🔴 NÁLEZY" if problem else "🟢 ČISTÝ DEN")
    print(f"turnů {v['turnu']} · odpovědí {v['odpovedi']} · restartů bridge {v['restarty']}")
    print()
    print("Ztráty (každé číslo nad nulou je zpráva, která nedošla):")
    print(f"  turnů bez odeslané odpovědi {v['turn_bez_odpovedi']}  (orientační – zahrnuje i terminálové)")
    print(f"  nedoručené (odkladiště) {dl_pocet}")
    print(f"  vzdané přílohy        {v['vzdane_prilohy']}")
    print(f"  selhal zápis do okna  {v['inject_selhal']}  (z toho zachránilo opakování: {v['inject_po_opakovani']})")
    print()
    print("Kvalita doručení:")
    print(f"  pojistka musela zaskočit  {v['backstop']}")
    print(f"  fronta se opakovaně nedařila {v['duplicity_fronta']}")
    print(f"  výpadky sítě              {v['sit_vypadky']}")
    print()
    print("Doba odpovědi (nezajímá průměr, ale nejhorší případy):")
    print(f"  medián {v.get('median_turn', 0):.0f}s · 90. percentil {v.get('p90_turn', 0):.0f}s "
          f"· nejdelší {v['nejdelsi_turn']:.0f}s · turnů nad 2 min: {v['turny_nad_2min']}")
    if sti:
        print()
        print(f"⚠️ Petr {len(sti)}× hlásil, že něco nedorazilo – to je nejtvrdší důkaz:")
        for s in sti[:5]:
            print(f"   {s}")
    if dl_ukazky:
        print()
        print("Nedoručené záznamy k prohlédnutí:")
        for z in dl_ukazky:
            print(f"   {z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
