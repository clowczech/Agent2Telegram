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
