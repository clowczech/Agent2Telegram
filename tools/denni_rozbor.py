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
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

LOG = os.path.expanduser("~/.claude/telegram-bridge/logs/agent2telegram.out.log")
STATE = os.path.expanduser("~/.local/state/agent2telegram")

# Datum je volitelné: řádky před 2026-08-04 mají jen čas.
TS = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{2}):(\d{2}):(\d{2})\s+(\w+)\s+")
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
    h, mi, s = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600 + mi * 60 + s


def _datum(radek: str) -> str | None:
    """Datum řádku, nebo None u starších řádků, které ho ještě nemají."""
    m = TS.match(radek)
    return m.group(1) if m else None


def vyber_dny(radky: list[str], dny: int, den: str | None = None) -> tuple[list[str], str]:
    """Vybere řádky za posledních `dny` dní (nebo za konkrétní `den`).

    Vrací i větu o tom, co se doopravdy měřilo. Do 2026-08-04 log datum neměl, takže
    starší řádky vybrat nejdou – radši to řekneme nahlas, než abychom je tiše přimíchali
    a vydávali měsíční součty za denní (přesně to se dělo).
    """
    s_datem = [r for r in radky if _datum(r)]
    if not s_datem:
        return radky, ("⚠️ Log ještě nemá datum, takže tohle NENÍ denní číslo – "
                       f"je to součet za posledních {len(radky)} řádků.")
    dostupne = sorted({_datum(r) for r in s_datem})
    if den:
        chtene = {den}
        popis = f"Měřený den: {den}."
    else:
        chtene = set(dostupne[-dny:])
        popis = (f"Měřený úsek: {min(chtene)} až {max(chtene)}"
                 + (f" ({len(chtene)} dny)." if len(chtene) > 1 else "."))
    vybrane = [r for r in s_datem if _datum(r) in chtene]
    if not vybrane:
        return [], f"Za {den or 'zvolený úsek'} nejsou v logu žádné řádky."
    if len(s_datem) < len(radky):
        popis += f" Starší řádky bez data ({len(radky) - len(s_datem)}) jsem vynechala."
    return vybrane, popis


def nacti(cesta: str) -> list[str]:
    try:
        with open(cesta, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError as e:
        print(f"log nelze číst: {e}", file=sys.stderr)
        return []


def rozbor(radky: list[str]) -> dict:
    """Spočítá metriky nad předanými řádky. Výběr úseku dělá `vyber_dny()`."""
    v = {
        "turnu": 0, "odpovedi": 0, "turn_bez_odpovedi": 0,
        "inject_selhal": 0, "inject_po_opakovani": 0,
        "sit_bezne": 0, "sit_chyby": 0, "backstop": 0, "vzdane_prilohy": 0,
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
        elif "Connection reset by peer" in r:
            # Dlouhý dotaz (50 s) ukončený protistranou. Den má ~1700 cyklů, takže tohle je
            # BĚŽNÝ provoz protokolu, ne incident. Hlásit ho jako "výpadek sítě" znamená
            # utopit skutečné chyby v šumu (Petr 2026-08-04).
            v["sit_bezne"] += 1
        elif "urlopen error" in r or "HTTP 409" in r or "HTTP 502" in r or "timed out" in r:
            v["sit_chyby"] += 1
    if delky:
        delky.sort()
        v["median_turn"] = delky[len(delky) // 2]
        v["p90_turn"] = delky[int(len(delky) * 0.9)]
    return v


STAV_SOUBOR = os.path.join(STATE, "rozbor_stav.json")

# Metriky, u kterých má smysl hlásit ZMĚNU proti minulému rozboru. Klíč = jméno v `rozbor()`,
# hodnota = (jak se tomu říká lidsky, o kolik se to musí pohnout, aby to stálo za zmínku).
SLEDOVANE = {
    "inject_selhal": ("selhání zápisu do okna", 1),
    "backstop": ("zásah pojistky", 1),
    "duplicity_fronta": ("opakované neúspěšné odeslání", 1),
    "vzdane_prilohy": ("vzdaná příloha", 1),
    "restarty": ("restart bridge", 5),
    "sit_chyby": ("skutečná síťová chyba", 20),
    "turny_nad_2min": ("turn delší než dvě minuty", 10),
}


def nacti_stav() -> dict:
    try:
        with open(STAV_SOUBOR, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def uloz_stav(v: dict, dl_pocet: int) -> None:
    """Zápis přes dočasný soubor – nedokončený rozbor nesmí nechat rozbitý stav."""
    data = {k: v.get(k, 0) for k in SLEDOVANE}
    data["dead_letter"] = dl_pocet
    tmp = f"{STAV_SOUBOR}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(STAV_SOUBOR), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, STAV_SOUBOR)
    except OSError as e:
        print(f"stav rozboru se nepodařilo uložit: {e}", file=sys.stderr)


def zmeny(v: dict, dl_pocet: int, minule: dict) -> list[str]:
    """Co je proti minulému rozboru NOVÉ, ZHORŠENÉ nebo VYŘEŠENÉ.

    Bez tohohle vypadá rozbor každý den stejně a po týdnu ho nikdo nečte. Stejný princip
    jako sekce "Změny oproti poslednímu auditu" v security reportu (Petr 2026-08-04).
    """
    if not minule:
        return ["- První rozbor s pamětí, srovnávat nemám s čím."]
    out = []
    for klic, (nazev, prah) in SLEDOVANE.items():
        ted, drive = v.get(klic, 0), minule.get(klic, 0)
        rozdil = ted - drive
        if drive == 0 and ted > 0:
            out.append(f"- 🆕 Nové: {nazev} – {ted}× (včera nic).")
        elif ted == 0 and drive > 0:
            out.append(f"- ✅ Vyřešeno: {nazev} už se neopakuje (včera {drive}×).")
        elif rozdil >= prah:
            out.append(f"- 🔺 Zhoršeno: {nazev} {drive}× → {ted}×.")
        elif -rozdil >= prah:
            out.append(f"- 🔻 Zlepšeno: {nazev} {drive}× → {ted}×.")
    dl_drive = minule.get("dead_letter", 0)
    if dl_pocet > dl_drive:
        out.append(f"- 🆕 Nové: v odkladišti přibylo {dl_pocet - dl_drive} zpráv.")
    elif dl_pocet < dl_drive:
        out.append(f"- ✅ Vyřešeno: odkladiště se zmenšilo z {dl_drive} na {dl_pocet}.")
    return out or ["- Nic nového, stav odpovídá včerejšku."]


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
    p.add_argument("--dny", type=int, default=1,
                   help="kolik posledních dní z logu měřit (výchozí 1)")
    p.add_argument("--den", help="konkrétní den ve formátu RRRR-MM-DD")
    p.add_argument("--radku", type=int, default=200_000,
                   help="strop načtených řádků; samotný výběr dne dělá --dny/--den")
    a = p.parse_args()

    radky = nacti(a.log)[-a.radku:]
    if not radky:
        print("PRÁZDNÝ LOG – nemám co měřit (samo o sobě podezřelé)")
        return 1

    radky, popis_useku = vyber_dny(radky, a.dny, a.den)
    if not radky:
        print(popis_useku)
        return 1

    v = rozbor(radky)
    dl_pocet, dl_ukazky = dead_letter()
    minule = nacti_stav()
    sti = stiznosti(radky)

    # `turn_bez_odpovedi` ZÁMĚRNĚ nespouští poplach: log nerozlišuje, jestli turn přišel
    # z Telegramu, nebo z terminálu – a terminálový turn odpověď na Telegram mít nemá.
    # Falešný poplach je horší než žádná metrika: pár dní a nikdo mu nevěří.
    # Zůstává jako orientační číslo, poplach spouštějí jen tvrdé důkazy ztráty.
    problem = (dl_pocet or v["vzdane_prilohy"] or v["inject_selhal"] or sti
               or v["restarty"] > 2)

    # Formát: NADPIS s emoji, dvojtečka, pod tím odrážky (Petr 2026-07-31). Žádné tabulky –
    # v Telegramu se rozsypou. A hlavně: rozbor je pracovní nástroj pro mě, ne výpis čísel
    # pro Petra, proto sekce "Co s tím" na konci.
    ztraty = dl_pocet + v["vzdane_prilohy"]
    r = []

    if ztraty:
        r.append("❌ Ztracené zprávy:")
        if dl_pocet:
            r.append(f"- {dl_pocet} v odkladišti")
        if v["vzdane_prilohy"]:
            r.append(f"- {v['vzdane_prilohy']} vzdaných příloh")
        r.append("")
    else:
        r.append("✅ Bez ztrát:")
        r.append("- žádná zpráva se neztratila")
        r.append("- odkladiště prázdné")
        r.append("")

    r.append("🆕 Změny oproti minulému rozboru:")
    r.extend(zmeny(v, dl_pocet, minule))
    r.append("")

    r.append("📊 Provoz:")
    r.append(f"- {popis_useku}")
    r.append(f"- {v['odpovedi']} zpráv, {v['restarty']} restartů")
    r.append(f"- odezva obvykle {v.get('median_turn', 0):.0f} s")
    r.append(f"- nejhorší případ {v['nejdelsi_turn']:.0f} s")
    if v["sit_bezne"]:
        r.append(f"- {v['sit_bezne']}× se znovu navázalo spojení (běžné, bez dopadu)")
    if v["sit_chyby"]:
        r.append(f"- {v['sit_chyby']} skutečných síťových chyb (DNS, 409, 502, timeout)")
    r.append("")

    potize = []
    if v["inject_selhal"]:
        z = v["inject_po_opakovani"]
        potize.append(f"- {v['inject_selhal']}× nešlo zapsat do okna"
                      + (f" ({z}× zachránilo opakování)" if z else " (opakování nepomohlo)"))
    if v["backstop"]:
        potize.append(f"- {v['backstop']}× musela zaskočit pojistka")
    if v["duplicity_fronta"]:
        potize.append(f"- {v['duplicity_fronta']}× se opakovaně nedařilo odeslat")
    if potize:
        r.append("⚠️ Drhlo:")
        r.extend(potize)
        r.append("")

    if sti:
        r.append("🔔 Tys hlásil problém:")
        r.append(f"- {len(sti)}× jsi psal, že něco nedorazilo")
        r.append("- to má přednost před vším ostatním")
        r.append("")

    # Číslované, aby na ně Petr mohl odpovědět "oprav 1 a 3" – stejně jako u security
    # reportu (jeho zadání 2026-08-04). Bez čísel se dá odpovědět jen "oprav to všechno".
    akce = []
    if ztraty:
        akce.append("Najít příčinu nedoručených zpráv a vyprázdnit odkladiště.")
    if sti:
        akce.append("Projít log v časech, kdy jsi psal, že něco nedorazilo.")
    if v["inject_selhal"] and not v["inject_po_opakovani"]:
        akce.append("Zápis do okna selhává a opakování nepomáhá – zjistit proč.")
    if v["restarty"] > 5:
        akce.append(f"{v['restarty']} restartů je hodně, dohledat příčinu.")
    if v["duplicity_fronta"]:
        akce.append("Prověřit záznamy, které se opakovaně nedařilo odeslat.")
    if v["sit_chyby"] > 50:
        akce.append(f"{v['sit_chyby']} síťových chyb je nad běžnou hladinou – prověřit DNS a VPN.")
    if v["backstop"]:
        akce.append("Projít turny, kde musela zaskočit pojistka – odpověď tam chyběla.")

    if akce:
        r.append("🔧 Doporučené akce:")
        for i, text in enumerate(akce, 1):
            r.append(f"- {i}. {text}")
        r.append("- Odpověz čísly, co mám opravit.")
    else:
        r.append("🔧 Doporučené akce:")
        r.append("- Žádné, provoz je čistý. Sleduju dál.")
    r.append("")

    print("\n".join(_uprav_odrazky(r)))
    # Stav se ukládá až po vypsání – když rozbor spadne, zítřek porovná proti témuž základu.
    uloz_stav(v, dl_pocet)
    return 0


def _uprav_odrazky(radky: list[str]) -> list[str]:
    """Každá odrážka velkým písmenem a s tečkou na konci (Petrovo zadání).

    Dělá se to tady jednou pro všechny řádky, ne ručně u každého – jinak by to dřív nebo
    později někde uteklo a formát by nesedel.
    """
    hotovo = []
    for radek in radky:
        if radek.startswith("- ") and len(radek) > 2:
            text = radek[2:]
            text = text[0].upper() + text[1:]
            if text[-1] not in ".!?:":
                text += "."
            radek = "- " + text
        hotovo.append(radek)
    return hotovo


if __name__ == "__main__":
    sys.exit(main())
