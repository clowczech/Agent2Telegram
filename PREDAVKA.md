# Předávka: Agent2Telegram v2 → nasazení a týdenní provoz

Stav k 2026-07-31 12:30. Píšu to proto, aby další session navázala bez ptaní a bez ztráty.

## Kde to je

```
~/Agent2Telegram-v2      větev v2-durable, 14 commitů, 169 testů zelených (macOS + Ubuntu)
~/Agent2Telegram         STABILNÍ main – odsud běží všechny tři bridge (Master, Genius, Sol)
~/tmp/audit-bridge/      audit, tři revize, plán, instalace na Ubuntu
SKORO-NEHODY.md          deník podle leteckého vzoru, 6 položek
```

Nic nepushnuto. `main` netknutý. Push jen na Petrovo výslovné slovo.

Testy: `<scratchpad>/auditvenv/bin/python -m pytest -q` (pytest NENÍ v systému, jen v tom venv).
Linux: `docker run --rm -v ~/Agent2Telegram-v2:/app -w /app python:3.12-slim python -m unittest discover -s tests`
(colima musí běžet: `colima start`).

## Petrovo zadání (doslova, 2026-07-31)

> „Udělej všechny ty vývojový věci, který ti dal Sol a Fable. Připrav to na přepnutí a přepneme
> na novou verzi a budem to týden testovat a ty každej den zanalyzuješ logy každej den."

A dřív: *„vybrušovat jako diamant… jednoduchý bridge, ale maximálně robustní. Jako v leteckém
průmyslu."*

---

## Co zbývá – ČÁST A: vývojové body z revizí

Zdroje: `sol-kolo3.md` (11 bodů), `fable-kolo3.md` (6), `lana-kolo3.md` (5), plus nedodělky
z `sol-recenze.md` / `fable-recenze.md`.

**Hotovo z nich už je:** živý retry za běhu (Sol #1), vzdaná příloha do dead-letter (Sol #2),
srozumitelná hláška místo tracebacku (instalace), zámek zapojen, replay po restartu, offset se
bez úložiště neposouvá.

**Zbývá, v tomhle pořadí:**

1. **Sol #3 – jeden vlastník durable outboxu.** Dnes do něj sahá inbound worker i odchozí smyčka.
   Sol výslovně **zamítá** mutex kolem celého drainu (řeší symptom víc stavem) a doporučuje
   jediného konzumenta. Souhlasím.
2. **Sol #5 – přehodit pořadí `done()` a zápisu do ledgeru.** Pád mezi nimi dnes zopakuje celou
   odpověď. Levná oprava, velký efekt.
3. **Sol #4 – „turn má odpověď" má znamenat durable uloženou odpověď**, ne odeslanou.
4. **Fable #1 – ZVÁŽIT ZAMÍTNUTÍ mé opravy K** (per-bot výchozí cesty). Fable argumentuje, že
   přináší víc rizika při upgradu než užitku a stačí zámek. **Nepřebírat automaticky ani jedno –
   rozhodnout a rozhodnutí zapsat.**
5. **Sol #7 – durable attach bez `signal_file`** (dnes se outbox v té konfiguraci nezapne).
6. **Instalace: preflight jedním `apt` řádkem + oprava `/dev/tty` v průvodci** (Fable, blokující
   pro webinář).

**Vědomě NEDĚLAT** (obojí zdůvodněné v revizích): automatická migrace starého globálního state
adresáře (z dat nelze bezpečně určit, komu patřil) a throttle na `_notify_inject_failed`.

**Po každém bodu:** celá baterie + mutační kontrola. Mutace až PO commitu (`mutace2.sh` vrací
soubory přes `git checkout` – na necommitnutém stavu smaže rozdělanou práci).

## Co zbývá – ČÁST B: přepnutí

1. **`bridge_boot.sh`: Master spouštět z `~/Agent2Telegram-v2`**, Genius a Sol nechat na
   `~/Agent2Telegram`. Dnes mají všichni společné `A2T_DIR` – musí se stát parametrem `start_a2t`.
2. **Ověřit návrat zpátky doopravdy**: přepnout → ověřit → přepnout zpět → ověřit. Rollback,
   který nikdo nezkusil, není rollback.
3. **Stav Masteru zůstává** v `~/.local/state/agent2telegram` (env má přednost před per-bot
   cestou), takže se offset nezahodí. Ověřit, že po startu v2 sedí.
4. **Checklist nasazení** sepsat předem a projít podle něj, ne podle paměti.

## Co zbývá – ČÁST C: denní rozbor provozu (Petrovo zadání místo kanárku)

Petr **odmítl umělého kanárka** s dobrým argumentem: posílá pořád tutéž krátkou zprávu, takže
projde i když je rozbité všechno zajímavé. Skutečné konverzace jsou tvrdší test.

Denní skript má z logu bridge + transkriptu spočítat:

- **každá Petrova zpráva → přišla odpověď?** Nespárované = výpadek, i nepovšimnutý
- **duplicity** (totéž doručeno dvakrát)
- **počet záznamů v dead-letter** = přímý počet nedoručených zpráv
- **`inject failed`** a kolikrát pomohlo opakování
- **doba do odpovědi** – ne průměr, ale nejhorší případy
- **Petrovy fráze značící výpadek** („jsi tam?", „nedostal jsem odpověď", „?", „nereaguješ")
  spárované s tím, co se v tu chvíli dělo. **Tohle je nejlepší detektor** – dnešek by odhalil
  ranní ticho i 50 minut odpoledne.

Spouštět denně, výsledek posílat Petrovi **přes `notify_petr.sh`** (turn z cronu nemá cestu
na Telegram přes `[tg]` – viz níže).

---

## Pasti, které stály dnes nejvíc času

- **`[tg]` funguje jen u turnů z Telegramu.** Turn probuzený hlídačem/cronem se tváří jako
  terminálový a zpráva se zahodí BEZ chyby v logu. Z pozadí posílat
  `~/.claude/telegram-bridge/notify_petr.sh "text"` (jde přes náš bridge, ne curlem).
- **Nečekat pasivně.** Před koncem tahu nasadit `Bash(run_in_background)` hlídač; interval
  5–10 min; hlásit i „pořád pracují".
- **macOS vs Linux:** `ps` bez `-ww` ořezává args, `comm` je ořezané na 16 znaků,
  `ps -o etimes=` na macOS není, `timeout` chybí.
- **Testy mohou procházet ze špatného důvodu.** `sh -c "sleep 30  # marker"` se exec-nahradí
  a marker z argv zmizí → test „prošel", protože proces neexistoval.
- **Zelená baterie nic nedokazuje.** 30. 7. byla zelená celý den, co Telegram nefungoval.

## Zásada pro celý zbytek práce

Jeden nález = jeden commit. Po každém celá baterie. Mutační kontrola jako brána, ne jednorázová
akce. A ke každé opravě typu „nesmí to zablokovat" i tvrzení, **co se stalo s tím, co se
přeskočilo** – jinak se z opravy stane tichá ztráta (stalo se dnes).
