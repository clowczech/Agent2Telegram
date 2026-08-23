# Deník skoro-nehod

Podle leteckého vzoru (Petr, 2026-07-31): zapisují se i případy, které **nezpůsobily škodu**.
Havárie si vynutí pozornost sama; skoro-nehoda je varování zdarma – a právě proto se zapomíná.

Ke každé položce patří odpověď na otázku **„co ji příště chytne samo?"**. Bez ní je záznam
jen historka.

---

## 2026-07-31 · Zámek proti dvěma instancím existoval, ale nikdy se nevolal

**Co se stalo:** `single_instance_lock` byl napsaný, otestovaný a hlášený jako hotový. Nikde
v produkční cestě se ale nevolal, takže ochrana proti dvěma pollerům (409 Conflict) neexistovala.
Nahlásila jsem Petrovi „dvě instance se nespustí" – nepravdivě.

**Proč to prošlo:** test ověřoval **helper**, ne **jeho zapojení**. Zelený test svedl k závěru,
že funkce je v provozu.

**Škoda:** žádná, nasazení ještě neproběhlo.

**Co to příště chytne:** u každé nové ochrany musí existovat test, který jde **produkční cestou**
(`run()`), ne přes pomocnou funkci. Otázka do checklistu: *volá tohle vůbec někdo?*
Levná kontrola: `grep -rn "<jméno>" --include=*.py | grep -v tests`.

---

## 2026-07-31 · Test procházel proto, že testovaný proces vůbec neexistoval

**Co se stalo:** test měl ověřit, že se do hlídání procesů nezapočítá cizí shell. Shell se spouštěl
jako `sh -c "sleep 30  # marker"`, jenže shell se v tomhle tvaru **exec-nahradí** za `sleep`
a marker z příkazové řádky zmizí. Test tedy procházel proto, že proces v seznamu nebyl vůbec –
ne proto, že by ho filtr vyloučil.

**Proč to prošlo:** zelená barva. Nikdo se neptal, *proč* je zelená.

**Škoda:** žádná – ale ta samá past chytla dva lidi nezávisle (mě i Fable) během jedné hodiny.

**Co to příště chytne:** u testu, který tvrdí „X se nezapočítá", musí být i tvrzení, že X
v seznamu **doopravdy je**. Jinak se testuje prázdno.

---

## 2026-07-31 · Mutační kontrola odhalila nepokrytou větev

**Co se stalo:** mutace `if doruceno is False:` → `if False:` prošla. Testy pokrývaly jen pád
zpracování výjimkou, ne tichý nezdar – tedy ten **častější** případ (zamrzlé tmux okno).

**Proč to prošlo:** obě větve vypadaly symetricky, takže se zdálo, že test pokrývá obě.

**Škoda:** žádná, mutační kontrola proběhla před nasazením.

**Co to příště chytne:** mutační kontrola jako **brána před nasazením**, ne jako jednorázová akce.
Skript: `scratchpad/mutace2.sh`.

---

## 2026-07-31 · Stop hook sliboval pojistku, kterou 50 řádků mrtvého kódu neplnilo

**Co se stalo:** `stop_forward.py` má `return`, za kterým leží ~50 řádků nedosažitelného kódu.
Docstring **i Petrovo CLAUDE.md** přitom tvrdily, že hook přepošle zapomenutou odpověď. Vypnutí
bylo v červnu záměrné, dokumentace se neaktualizovala.

**Škoda:** žádná přímá, ale oba jsme se spoléhali na záchrannou síť, která tam nebyla.

**Co to příště chytne:** když se chování vypne, musí ve stejném commitu zmizet i slib o něm.
Mrtvý kód se maže, ne komentuje – jinak se z něj po měsících stane dokumentace.

---

## 2026-07-31 · Oprava ucpané fronty vyrobila tichou ztrátu

**Co se stalo:** oprava „vadná příloha nesmí ucpat frontu" po vyčerpání pokusů volala
`mark_file_sent` – tedy zapsala do vlastní evidence, že příloha dorazila, ačkoli nedorazila.
Řešila jsem jeden způsob selhání a vyrobila jiný, horší.

**Proč to prošlo:** testy ověřovaly, že fronta pokračuje, ale ne **za jakou cenu**. Zelené byly.

**Škoda:** žádná, nasazení ještě neproběhlo. Našel Sol ve třetí revizi.

**Co to příště chytne:** u každé opravy typu „nesmí to zablokovat" musí být i tvrzení, **co se
stalo s tím, co jsme přeskočili**. Přeskočit smí jen věc, která je někde dohledatelně uložená.
Test teď kontroluje dead-letter i to, že vzdaná příloha není vedená mezi odeslanými.

---

## 2026-07-31 · Mutační skript smazal rozdělanou opravu

**Co se stalo:** `mutace2.sh` vrací soubory přes `git checkout --`. Pustila jsem ho na
NEcommitnutém stavu, takže mi čerstvou opravu přepsal poslední commit.

**Škoda:** pár minut práce; oprava se dala napsat znovu z kontextu.

**Co to příště chytne:** mutační kontrola patří **až za commit**, nikdy před něj. Doplnit do
checklistu; ideálně ať skript sám odmítne běžet, když `git status` není čistý.

---

## 2026-07-31 · Test rollbacku vypadal jako výpadek

**Co se stalo:** při plánovaném testu návratu na starou verzi byl bridge minutu dole. Petr mezitím
napsal „Je tam chyba!! Nereagujes" – z jeho strany k nerozeznání od skutečného výpadku.

**Proč:** varování znělo „na chvíli mi možná vypadne odpověď". To je příliš měkké. Chybělo
konkrétní číslo a jistota.

**Škoda:** žádná technická, ale zbytečné leknutí – u nástroje, jehož celý smysl je „nikdy nemlčet",
je to horší, než to vypadá.

**Co to příště chytne:** před KAŽDÝM zásahem, který přeruší doručování, poslat předem zprávu
s konkrétní dobou („bude ticho asi minutu, je to plánované, ozvu se hned potom"). A pokud možno
poslat ji cestou, která přeruší nebude – tedy `notify_petr.sh` z pozadí, ne z turnu, který se
zásahem skončí.

---

## 2026-08-01 · Testovací nástroj otevřel DNS na všech rozhraních

**Co se stalo:** kvůli testům na Ubuntu jsem 31. 7. nastartovala Colimu. Ta otevřela
rekurzivní DNS (`limactl` na `*:53`) na **všech rozhraních**, ne jen na loopbacku. Noční
bezpečnostní audit to označil červeně.

**Proč to prošlo:** startovala jsem nástroj kvůli jedné úloze a neptala se, co dalšího otevře.
Testovací prostředí jsem posuzovala jako neškodné, protože „jen spouští testy".

**Škoda:** žádná známá, ale otevřený resolver je zneužitelný zvenčí a běžel ~18 hodin.

**Co to příště chytne:** nástroj nastartovaný kvůli jedné úloze se po ní **zastaví**, ne nechá
běžet „pro jistotu". A po spuštění čehokoli, co vytváří virtuální stroj nebo kontejnery, zkontrolovat
`lsof -nP -iTCP -sTCP:LISTEN` – co nového poslouchá a na jakém rozhraní.

Vyřešeno: `colima stop`, port ověřeně zavřený. Pro další testy na Linuxu ji nastartuju znovu
a hned potom zastavím.

---

## 2026-08-01 · Potvrzení příjmu by na Linuxu prvních 30 s nefungovalo

**Co se stalo:** cooldown potvrzování používal jako výchozí hodnotu `0.0` a porovnával ji
s `time.monotonic()`. Ten ale počítá od startu **systému**: na macOS s dlouhým během je to velké
číslo (podmínka projde), na čerstvě nastartovaném Linuxu skoro nula (podmínka neprojde).
Na Ubuntu by tedy prvních 30 sekund po startu žádné potvrzení neodešlo.

**Proč to prošlo:** všech 208 testů bylo na macOS zelených. Chyba se ukázala až v kontejneru.

**Škoda:** žádná – zachyceno před nasazením.

**Co to příště chytne:** testy na Linuxu **před** nasazením, ne až po. A u každé práce s časem
si položit otázku, od čeho se vlastně počítá – `monotonic()` není totéž na stroji běžícím
měsíc a na čerstvě spuštěném kontejneru.

Tohle je přesně ten druh chyby, kvůli kterému Petr chtěl, aby bridge fungoval i na cizím Ubuntu.

---

## 2026-08-02 · Reakce srdíčkem poslala uživateli interní poznámku agenta

**Co se stalo:** Petr dal na zprávu ❤ a přišla mu odpověď „No response requested." – tedy
interní poznámka agenta, ne zpráva pro něj. Nahlásila to Lana‑Genius ze stable v1, Petr to
vzápětí potvrdil z vlastní strany.

**Mechanika:** reakční větev `_handle` injectne „…no need to reply unless relevant." a přitom
zavolá `_begin_turn()`, čímž turn označí jako Telegram‑originated. Když agent správně mlčí,
sáhne turn‑end backstop pro poslední assistant text a pošle ho. Dvě pravidla si přímo odporovala:
reakce říká „nemusíš odpovídat", backstop říká „Telegram turn nesmí zůstat bez odpovědi".

**Proč to prošlo:** backstop vznikl proti ztraceným odpovědím a testoval se na běžných
zprávách. Reakce je jediný vstup, který odpověď **nečeká** – a na ten se nikdo nepodíval.

**Škoda:** žádná věcná, jen zmatek. Ale je to únik interního textu k uživateli, tedy přesně
ta třída chyby, která by u cizího uživatele na webináři vypadala zle.

**Co to příště chytne:** u každého nového vstupního kanálu si položit otázku „očekává tenhle
vstup odpověď?" a podle toho zkontrolovat backstop. V testech teď existuje třída
`ReactionTurnBackstopTests`, která jde přes reálné `_handle()`.

**Past, které jsem se vyhnula:** původní návrh opravy nastavoval `_turn_text_sent = True`.
To pole ale zároveň řídí TUI bubliny a hlavně – reakce, která dorazí **během** běžícího turnu,
by tím odzbrojila backstop pro skutečnou otázku pod ní. Proto vlastní příznak a podmínka
„jen když žádný turn neběžel".

---

## 2026-08-23 · Codex změnil formát logu – bridge by oněměl při první aktualizaci

**Co se stalo:** Jiří Přecechtěl (Petrův účastník webináře) nahlásil, že mu po aktualizaci na
**Codex 0.149.0** bridge přestal doručovat odpovědi. Agent viditelně odpovídal, do Telegramu
nikdy nic nepřišlo. Příčina: novější Codex zapisuje odpověď agenta už jen jako
`response_item/message` (role=assistant), zatímco `CodexReader` četl výhradně
`event_msg/agent_message`.

**Proč to prošlo:** reader byl napsaný proti **jedné konkrétní verzi** formátu, který ale patří
cizímu nástroji a mění se bez ohlášení. Náš Codex 0.144.4 zapisuje obě formy, takže o tom
z našeho provozu nešlo nic poznat.

**Škoda:** u nás žádná. U Jiřího hodiny hledání a málem zbytečná reinstalace. **Nás by to trefilo
při první aktualizaci Codexu** – umlčelo by to `Lana - Sol`.

**Co to příště chytne:** reader teď čte obě formy a dedupuje **podle obsahu**, ne podle typu
záznamu (`tests/test_readers_codex.py`, ověřeno mutací). Pravidlo do hlavy: **formát logu cizího
nástroje je API, které se mění bez ohlášení** – číst tolerantně a mít test na obě varianty.
Druhá past odhalená při opravě: nainstalovaný balíček `/opt/homebrew/bin/agent2telegram` importuje
modul z **`~/Agent2Telegram` (v1)**, takže oprava jen ve v2 by se do produkce nepropsala.
Kontrola: `python3.14 -c "import agent2telegram, os; print(os.path.dirname(agent2telegram.__file__))"`.

**Nejhorší na tom je tvar selhání:** bridge nespadne, nezaloguje chybu, jen tiše mlčí a dál
ukazuje „typing". To je přesně ta tichá chyba, kterou má tenhle deník lovit.
