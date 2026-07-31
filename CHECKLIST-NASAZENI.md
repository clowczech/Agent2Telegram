# Checklist: přepnutí Masteru na v2

Postup podle seznamu, ne podle paměti – i po desáté (Petrův letecký princip, 2026-07-31).
Odškrtávat po jednom. **Když kterýkoli bod selže, jde se rovnou na ROLLBACK**, nedopisuje se.

## Před přepnutím

- [ ] `git status` v `~/Agent2Telegram-v2` je čistý, vše commitnuto
- [ ] celá baterie zelená na macOS
- [ ] celá baterie zelená na Ubuntu (py3.12 **i** py3.10)
- [ ] mutační kontrola projde – každá oprava má test, který zčervená, když ji vrátím
- [ ] `main` je netknutý, nic nepushnuto
- [ ] Genius a Sol běží ze **stabilní** složky (záložní kanál, kdyby Master selhal)
- [ ] zapsán čas přepnutí (kvůli pozdějšímu rozboru logů)

## Přepnutí

⚠️ **`ps` NEROZLIŠÍ starou a novou verzi** – obě mají shodné argv a obě logují `Attach bridge
live`. Poznat je jde jen podle pracovního adresáře procesu (Sol, finální kontrola).

- [ ] zapsat PID běžícího Masteru: `ps -axww -o pid=,args= | grep "config.json"`
- [ ] ověřit jeho cwd: `/usr/sbin/lsof -a -p <PID> -d cwd` → musí být `~/Agent2Telegram` (stará)
- [ ] `MASTER_DIR="$HOME/Agent2Telegram-v2"` v `bridge_boot.sh`
- [ ] `bash -n bridge_boot.sh` (syntaxe)
- [ ] ukončit Master bridge **podle PID**, ne podle vzoru
- [ ] počkat, až ho keepalive nahodí (do minuty)
- [ ] **ověřit cwd nového procesu** – teprve to dokazuje, že běží v2
- [ ] běží **právě jedna** instance
- [ ] offset navazuje (nezačal od nuly) – jinak hrozí smršť starých zpráv

## Ověření v provozu (ne teorií)

- [ ] poslat si zprávu z Telegramu → **přišla odpověď**
- [ ] poslat dlouhou odpověď (přes 4000 znaků) → **nepřišla dvakrát**
- [ ] poslat přílohu → **dorazila**
- [ ] poslat hlasovku → **přepsala se**
- [ ] `notify_petr.sh "test"` → **dorazilo** (cesta pro zprávy z pozadí)
- [ ] `python3 tools/denni_rozbor.py --radku 200` → běží a nehlásí ztráty

## ROLLBACK (musí být vyzkoušený DŘÍV, než ho budeme potřebovat)

⚠️ **Rollback není zadarmo.** Stará verze NEČTE fronty v2. Když se vrátíme se zprávou uvnitř,
uvízne tam: Telegram ji už znovu nepošle a stará verze o ní neví (Sol, finální kontrola).

- [ ] **fronty jsou prázdné**: `ls <state>/inbox/*.json <state>/queue/outbox/*.json` → nic
- [ ] neprobíhá turn (v logu není otevřený `TURN START` bez `TURN END`)
- [ ] teprve pak `MASTER_DIR=""` v `bridge_boot.sh`
- [ ] ukončit Master bridge → keepalive nahodí ze stabilní složky
- [ ] ověřit, že běží ze staré cesty a odpovídá na zprávu

**Rollback, který nikdo nezkusil, není rollback.** Vyzkoušet ho jako součást nasazení, ne až
v okamžiku problému.

## Týdenní provoz

- [ ] denní rozbor logů, výsledek Petrovi přes `notify_petr.sh` (ne `[tg]` – z cronu nefunguje)
- [ ] každý nález zapsat do `SKORO-NEHODY.md` i s odpovědí „co to příště chytne samo"
- [ ] po týdnu: rozhodnout o smazání staré fronty (podmínka: ani jednou se do ní nespadlo)
- [ ] teprve pak zvážit push na GitHub – a jen na Petrovo výslovné slovo
