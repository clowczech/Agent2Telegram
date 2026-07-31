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

    # Formát je ZÁMĚRNĚ bez tabulek: Telegram je nezobrazuje a rozsypou se do kaše.
    # A hlavně: tohle je pracovní nástroj pro mě, ne výpis čísel pro Petra – proto na konci
    # vždycky stojí, co s tím udělám. (Petr 2026-07-31)
    ztraty = dl_pocet + v["vzdane_prilohy"]
    r = []
    if problem:
        r.append("🔴 Něco k řešení")
    else:
        r.append("🟢 Čistý den, nic se neztratilo")
    r.append("")
    r.append(f"Zpráv: {v['odpovedi']} · restartů: {v['restarty']}")

    if ztraty:
        r.append("")
        r.append(f"❌ NEDORUČENO: {ztraty}")
        if dl_pocet:
            r.append(f"   • {dl_pocet} v odkladišti")
        if v["vzdane_prilohy"]:
            r.append(f"   • {v['vzdane_prilohy']} vzdaných příloh")
    if v["inject_selhal"]:
        zachraneno = v["inject_po_opakovani"]
        r.append("")
        r.append(f"⚠️ Nešlo zapsat do okna: {v['inject_selhal']}×"
                 + (f", z toho {zachraneno}× zachránilo opakování" if zachraneno else ""))
    if v["backstop"]:
        r.append(f"⚠️ Pojistka musela zaskočit: {v['backstop']}×")

    r.append("")
    r.append(f"Odezva: obvykle {v.get('median_turn', 0):.0f} s, nejhorší {v['nejdelsi_turn']:.0f} s")
    if v["sit_vypadky"]:
        r.append(f"Výpadků sítě: {v['sit_vypadky']} (bridge je přežil)")

    if sti:
        r.append("")
        r.append(f"🔔 {len(sti)}× jsi hlásil, že něco nedorazilo – to má přednost před vším ostatním")

    # Co s tím – tohle je jádro celého rozboru
    r.append("")
    if ztraty:
        r.append("→ Jdu se podívat na ty nedoručené a najít příčinu.")
    elif sti:
        r.append("→ Projdu, co se dělo v tu chvíli, cos psal.")
    elif v["inject_selhal"] and not v["inject_po_opakovani"]:
        r.append("→ Zápis do okna selhává a opakování nepomáhá. Podívám se na to.")
    elif v["restarty"] > 2:
        r.append(f"→ {v['restarty']} restartů je moc, zjistím proč.")
    elif problem:
        r.append("→ Prohlédnu si detaily v logu.")
    else:
        r.append("→ Nic nedělám, jen sleduju dál.")
    print("\n".join(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
