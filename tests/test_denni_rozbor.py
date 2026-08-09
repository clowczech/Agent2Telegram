"""Tests for the daily traffic analysis: what it counts and what it reports as a change."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "denni_rozbor", Path(__file__).resolve().parents[1] / "tools" / "denni_rozbor.py")
rozbor_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rozbor_mod)


class SitoveUdalostiTests(unittest.TestCase):
    """A long poll ends ~1700× a day and the peer usually closes the connection. Reporting that
    as an outage buries the real errors in noise — Petr called it out on 2026-08-04."""

    def test_connection_reset_counts_as_routine_not_as_an_error(self):
        radky = ["12:00:00 WARNING telegram: getUpdates: <urlopen error [Errno 54] "
                 "Connection reset by peer>, retry 1/5 (backoff)"] * 3

        v = rozbor_mod.rozbor(radky)

        self.assertEqual(v["sit_bezne"], 3)
        self.assertEqual(v["sit_chyby"], 0, "běžné přenavázání se počítá jako chyba")

    def test_real_failures_are_counted_separately(self):
        radky = [
            "12:00:00 WARNING telegram: getUpdates: HTTP 409 Conflict",
            "12:00:01 WARNING telegram: getUpdates: HTTP 502, retry 1/5",
            "12:00:02 WARNING telegram: getUpdates: <urlopen error [Errno 8] nodename nor servname>",
            "12:00:03 WARNING telegram: getUpdates: The read operation timed out",
        ]

        v = rozbor_mod.rozbor(radky)

        self.assertEqual(v["sit_chyby"], 4)
        self.assertEqual(v["sit_bezne"], 0)


class ZmenyTests(unittest.TestCase):
    """Without a comparison against the previous run the report looks the same every day and
    stops being read. Same idea as "Změny oproti poslednímu auditu" in the security report."""

    def test_first_run_says_it_has_nothing_to_compare_with(self):
        vysledek = rozbor_mod.zmeny({"backstop": 2}, 0, {})

        self.assertEqual(len(vysledek), 1)
        self.assertIn("srovnávat", vysledek[0])

    def test_a_metric_appearing_from_zero_is_reported_as_new(self):
        vysledek = rozbor_mod.zmeny({"backstop": 2}, 0, {"backstop": 0})

        self.assertTrue(any("Nové" in r and "pojistky" in r for r in vysledek), vysledek)

    def test_a_metric_dropping_to_zero_is_reported_as_resolved(self):
        vysledek = rozbor_mod.zmeny({"backstop": 0}, 0, {"backstop": 4})

        self.assertTrue(any("Vyřešeno" in r for r in vysledek), vysledek)

    def test_a_move_below_the_threshold_is_not_reported(self):
        # restarty mají práh 5 – dva restarty navíc nejsou zpráva
        vysledek = rozbor_mod.zmeny({"restarty": 12}, 0, {"restarty": 10})

        self.assertEqual(vysledek, ["- Nic nového, stav odpovídá včerejšku."])

    def test_a_growing_dead_letter_queue_is_always_reported(self):
        vysledek = rozbor_mod.zmeny({}, 3, {"dead_letter": 1})

        self.assertTrue(any("odkladišti přibylo" in r for r in vysledek), vysledek)


class StavTests(unittest.TestCase):
    def test_state_survives_a_write_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            puvodni = rozbor_mod.STAV_SOUBOR
            rozbor_mod.STAV_SOUBOR = str(Path(d) / "stav.json")
            try:
                rozbor_mod.uloz_stav({"backstop": 5, "restarty": 2}, dl_pocet=1)
                nacteno = rozbor_mod.nacti_stav()
            finally:
                rozbor_mod.STAV_SOUBOR = puvodni

            self.assertEqual(nacteno["backstop"], 5)
            self.assertEqual(nacteno["dead_letter"], 1)

    def test_a_corrupted_state_file_does_not_crash_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            cesta = Path(d) / "stav.json"
            cesta.write_text("{tohle není JSON", "utf-8")
            puvodni = rozbor_mod.STAV_SOUBOR
            rozbor_mod.STAV_SOUBOR = str(cesta)
            try:
                self.assertEqual(rozbor_mod.nacti_stav(), {})
            finally:
                rozbor_mod.STAV_SOUBOR = puvodni


class VyberDnuTests(unittest.TestCase):
    """Do 2026-08-04 log neměl datum a "denní" rozbor sčítal celé okno – u Petra zhruba měsíc.
    Číslo vydávané za denní musí být buď opravdu denní, nebo hlasitě označené jako jiné."""

    def test_undated_lines_are_flagged_loudly(self):
        radky = ["09:00:00 INFO x", "09:00:01 INFO y"]

        vybrane, popis = rozbor_mod.vyber_dny(radky, dny=1)

        self.assertEqual(vybrane, radky)
        self.assertIn("NENÍ denní", popis)

    def test_only_the_last_day_is_measured(self):
        radky = [
            "2026-08-02 09:00:00 INFO stary",
            "2026-08-03 09:00:00 INFO vcerejsi",
            "2026-08-04 09:00:00 INFO dnesni",
            "2026-08-04 10:00:00 INFO dnesni2",
        ]

        vybrane, popis = rozbor_mod.vyber_dny(radky, dny=1)

        self.assertEqual(len(vybrane), 2, "do denního čísla se dostaly i jiné dny")
        self.assertIn("2026-08-04", popis)

    def test_more_days_can_be_requested(self):
        radky = [f"2026-08-0{d} 09:00:00 INFO r" for d in (1, 2, 3, 4)]

        vybrane, popis = rozbor_mod.vyber_dny(radky, dny=3)

        self.assertEqual(len(vybrane), 3)
        self.assertIn("3 dny", popis)

    def test_a_specific_day_can_be_picked(self):
        radky = ["2026-08-03 09:00:00 INFO a", "2026-08-04 09:00:00 INFO b"]

        vybrane, popis = rozbor_mod.vyber_dny(radky, dny=1, den="2026-08-03")

        self.assertEqual(vybrane, [radky[0]])
        self.assertIn("2026-08-03", popis)

    def test_undated_lines_are_dropped_once_dated_ones_exist(self):
        radky = ["09:00:00 INFO bez data", "2026-08-04 09:00:00 INFO s datem"]

        vybrane, popis = rozbor_mod.vyber_dny(radky, dny=1)

        self.assertEqual(vybrane, [radky[1]], "řádek bez data se přimíchal do denního čísla")
        self.assertIn("vynechala", popis)

    def test_time_is_still_parsed_from_a_dated_line(self):
        self.assertEqual(rozbor_mod._cas("2026-08-04 01:02:03 INFO x"), 3723)
        self.assertEqual(rozbor_mod._cas("01:02:03 INFO x"), 3723)


if __name__ == "__main__":
    unittest.main()


class PocitaniOdpovediTests(unittest.TestCase):
    """Report hlásil „0 zpráv" ve dnech, kdy jich prošlo přes padesát.

    Počítal řetězec `FWD (send)`, jenže bridge přešel na `FWD (delivered)` a starou
    variantu píše jako `FWD (send, legacy path)` – tedy s čárkou, takže nesedělo ani to.
    Řádky níž jsou VÝŘEZY SKUTEČNÉHO LOGU, ne vymyšlený formát; přesně na tomhle
    rozdílu spadlo 2026-08-06 i měření Lynisu (audit).
    """

    REALNE_RADKY = [
        "2026-08-09 09:02:09 INFO    agent2telegram.attach: FWD (delivered) id=out-017 1 parts, 0 attachments 'ahoj'",
        "13:34:03 INFO    agent2telegram.attach: FWD (send, legacy path) '**Prepnuto a bezi.'",
        "2026-08-09 08:11:00 INFO    agent2telegram.attach: FWD (re-delivered) 'neco'",
        "2026-08-09 08:12:00 INFO    agent2telegram.attach: FWD (voice) 'hlasovka'",
    ]

    def test_vsechny_varianty_odeslani_se_pocitaji(self):
        napocteno = sum(1 for r in self.REALNE_RADKY if rozbor_mod.FWD in r)

        self.assertEqual(napocteno, len(self.REALNE_RADKY),
                         "některá varianta FWD se nepočítá – report bude hlásit míň, než prošlo")

    def test_radek_bez_odeslani_se_nepocita(self):
        cizi = "2026-08-09 09:00:00 INFO    agent2telegram.attach: TURN START t=123"

        self.assertNotIn(rozbor_mod.FWD, cizi)


class ZdraviTests(unittest.TestCase):
    """Skóre musí umět spadnout a nesmí chválit ticho.

    Petr 2026-08-09: chce každý den vidět v procentech, jak bridge jede, a při stu
    procentech ho nasadí na GitHub a použije na webináři. Číslo, které ukazuje 100 %
    i ve dnech, kdy se nic nedělo, je proto horší než žádné.
    """

    CISTY = {"odpovedi": 50, "restarty": 0, "turny_nad_2min": 0, "sit_chyby": 0,
             "inject_selhal": 0, "duplicity_fronta": 0, "vzdane_prilohy": 0}

    def test_bezvadny_den_ma_sto_procent(self):
        pct, duvody = rozbor_mod.zdravi(self.CISTY, dl_pocet=0, sti=0)

        self.assertEqual(pct, 100)
        self.assertEqual(duvody, [])

    def test_tichy_den_se_nehodnoti(self):
        pct, duvody = rozbor_mod.zdravi({"odpovedi": 1}, dl_pocet=0, sti=0)

        self.assertIsNone(pct, "málo provozu se vydávalo za stoprocentní zdraví")
        self.assertTrue(duvody)

    def test_ztracena_zprava_srazi_nejvic(self):
        pct, _ = rozbor_mod.zdravi(self.CISTY, dl_pocet=1, sti=0)

        self.assertLess(pct, 70, "ztráta zprávy musí být vidět na první pohled")

    def test_petrova_stiznost_srazi_skore(self):
        pct, duvody = rozbor_mod.zdravi(self.CISTY, dl_pocet=0, sti=1)

        self.assertLessEqual(pct, 75)
        self.assertTrue(any("nedorazilo" in d for d in duvody))

    def test_skore_nikdy_nejde_pod_nulu(self):
        hrozne = dict(self.CISTY, restarty=99, turny_nad_2min=99, sit_chyby=99,
                      inject_selhal=99, duplicity_fronta=99, vzdane_prilohy=99)

        pct, _ = rozbor_mod.zdravi(hrozne, dl_pocet=99, sti=99)

        self.assertEqual(pct, 0)

    def test_jednotlive_srazky_maji_strop(self):
        """Jeden opakující se jev nesmí sám o sobě přebít všechno ostatní."""
        pct, _ = rozbor_mod.zdravi(dict(self.CISTY, sit_chyby=1000), dl_pocet=0, sti=0)

        self.assertGreaterEqual(pct, 90)
