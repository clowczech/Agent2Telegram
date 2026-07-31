"""Reprodukce nálezů z auditu 2026-07-31 – doručovací záruky bridge.

Každý test tady odpovídá jednomu nálezu a je ZÁMĚRNĚ napsaný tak, aby na současném kódu
selhal. Teprve pak se opravuje. Pojmenování odkazuje na značky ze `SOUHRN.md`:

  A – příchozí zpráva se ztratí, když zápis do tmuxu selže
  B – Telegramu se potvrdí přijetí dřív, než je zpráva bezpečně zpracovaná
  C – konec turnu po dlouhém tichu obchází pojistku proti nezodpovězenému turnu
  D – selže-li síť uprostřed dlouhé zprávy, už doručené části se pošlou znovu
  F – přílohy nejsou v durable frontě, takže se ztratí nebo dorazí k cizí odpovědi
  K – stav není oddělený podle bota, odděluje ho jen proměnná prostředí

Pozadí: 30. 7. Petrovi mizely zprávy beze stopy. Monitor tehdy bridge zabíjel 4× denně
a nález B říká, že každý takový kill mohl sežrat právě přicházející zprávu natrvalo.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.compat import AlreadyRunning, single_instance_lock
from agent2telegram.config import Config, _state_dir
from agent2telegram.session import SessionError
from agent2telegram.telegram import TelegramError, split_message


class _Client:
    """Minimální fake Telegram klient – zaznamenává, co reálně odešlo."""

    def __init__(self):
        self.sent = []
        self.files = []
        self.actions = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)

    def send_file(self, chat_id, path, caption=None, **kw):
        self.files.append(path)

    def delete_message(self, chat_id, message_id):
        pass


class _ChunkingClient(_Client):
    """Věrný model dělení dlouhé zprávy.

    Telegram dostává chunky postupně a **už doručené chunky nezruší**, když pozdější selže.
    Přesně tohle chování dělá z „pošli to celé znovu" duplicitu.
    """

    def __init__(self, fail_on_chunk=2):
        super().__init__()
        self.delivered = []
        self.fail_on_chunk = fail_on_chunk
        self.fail_armed = True

    def send_message(self, chat_id, text, parse_mode=None):
        for i, chunk in enumerate(split_message(text) or [text], start=1):
            if self.fail_armed and i == self.fail_on_chunk:
                self.fail_armed = False          # síť se po výpadku zotaví
                raise TelegramError("connection reset by peer")
            self.delivered.append(chunk)


class _DeadSession:
    """tmux pane, do kterého se nedá psát – zamrzlé TUI, plný buffer."""

    def __init__(self, exc=None):
        self.exc = exc or SessionError(
            "Command ['tmux', 'send-keys', ...] timed out after 10 seconds"
        )
        self.injected = []

    def inject(self, text):
        raise self.exc


class _OkSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def _bridge(state_dir, client=None, session=None):
    """Bridge poskládaný ručně – stejný postup jako v test_attach_queue.py."""
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = client or _Client()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = session or _OkSession()
    b._sent_keys = set()
    b._pending_send = []
    b._pending_files = []
    b._turn_active = threading.Event()
    b._turn_from_tg = False
    b._transcript = None
    b._last_activity = 0.0
    b._status = {"mid": None, "shown": ""}
    b._last_typing = 0.0
    b._typing_count = 0
    b._turn_started = 0.0
    b._max_gap = 0.0
    b._last_pane_warning = 0.0
    b._status_path = None
    b._seen_tools = set()
    b._tui_seen = set()
    b._turn_text_sent = True
    b._pending_turn_end = False
    b._marker = "[tg]"
    b._stop = threading.Event()
    state = Path(state_dir)
    b._offset_file = state / "offset"
    b._processed_updates_file = state / "processed_updates"
    b._queue_path = state / "outbox.json"
    b._processed_update_ids, b._processed_update_order = b._read_processed_updates()
    return b


def _msg(update_id, text="ahoj"):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "from": {"id": 7}, "chat": {"id": 7}, "text": text},
    }


# --------------------------------------------------------------------------------------
# A – zápis do tmuxu selže
# --------------------------------------------------------------------------------------
class InjectFailureTests(unittest.TestCase):
    def test_failed_inject_does_not_silently_drop_the_message(self):
        """Když zápis do okna selže, zpráva NESMÍ zmizet bez jediné stopy pro uživatele.

        Doloženo z provozu 31. 7.:
          19:04:41 ERROR inject failed: '[TG] Jsi tam tedy?' timed out after 10 seconds
        Petrova zpráva se ztratila a on se to nikdy nedozvěděl.

        Přijatelné je obojí: buď se zpráva uchová k opakování, nebo dostane upozornění.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td, session=_DeadSession())

            ok = b._inject("[TG] Jsi tam tedy?")
            self.assertFalse(ok, "inject měl selhat – to je vstupní podmínka testu")

            notified = any("nedoruč" in s.lower() or "selhal" in s.lower() or "failed" in s.lower()
                           for s in b.tg.sent)
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(
                notified or retained,
                "zpráva zmizela: nikam se neuložila k opakování a uživatel nedostal upozornění",
            )


# --------------------------------------------------------------------------------------
# B – potvrzení Telegramu předbíhá zpracování
# --------------------------------------------------------------------------------------
class InboundDurabilityTests(unittest.TestCase):
    def test_crash_between_ack_and_queue_does_not_lose_the_update(self):
        """Pád po posunu offsetu, ale před zařazením do fronty.

        Telegram považuje update za vyřízený, jakmile si řekneme o vyšší offset – znovu ho
        nikdy nepošle. Když proces spadne přesně tady (což SIGTERM od monitoru reálně dělal),
        zpráva je pryč navždy.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)

            def _die(upd, record_id=None):
                raise SystemExit("kill uprostřed zpracování")

            b._submit_inbound_update = _die
            with self.assertRaises(SystemExit):
                b._handle_update_once(_msg(1000), 1000)

            # Restart nad stejným stavem:
            b2 = _bridge(td)
            offset = b2._load_offset()
            inbox = list(Path(td).glob("inbox/*"))
            self.assertTrue(
                offset <= 1000 or inbox,
                f"update 1000 je nenávratně pryč: offset={offset} a žádný durable inbox",
            )

    def test_pending_message_is_delivered_after_restart(self):
        """Uložená zpráva se po restartu musí SKUTEČNĚ doručit, ne jen ležet na disku.

        Křížová recenze (Sol #1, Fable F1): zápis fungoval, ale nikdo sklad při startu
        nečetl. Telegram zprávu znovu nepošle, takže by tam ležela až do vypršení retence.
        Test proto kontroluje `session.injected`, ne přítomnost souboru – ta nic neznamená.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)

            def _die(upd, record_id=None):
                raise SystemExit("kill uprostřed zpracování")

            b._submit_inbound_update = _die
            with self.assertRaises(SystemExit):
                b._handle_update_once(_msg(4000, "neztrať mě"), 4000)

            # restart nad stejným stavem
            session = _OkSession()
            b2 = _bridge(td, session=session)
            b2._ensure_inbound_worker_state()
            b2._replay_pending_inbound()
            for _ in range(100):
                if session.injected:
                    break
                time.sleep(0.02)
            b2._stop.set()
            time.sleep(0.3)

            self.assertTrue(session.injected, "zpráva zůstala ležet na disku a nikdy se nedoručila")
            self.assertIn("neztrať mě", "\n".join(session.injected))

    def test_handler_failure_keeps_the_message_for_retry(self):
        """Když zpracování selže, worker chybu jen zaloguje a zprávu zahodí."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            calls = []

            def _boom(upd):
                calls.append(upd)
                raise RuntimeError("tmux je mrtvý")

            b._handle = _boom
            # Přes _handle_update_once, ne přímým submitem – jinak by zpráva minula
            # rezervaci v inboxu a test by měřil něco jiného, než se v provozu děje.
            b._handle_update_once(_msg(2000), 2000)
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.3)

            self.assertTrue(calls, "handler se vůbec nespustil – špatný test")
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(retained, "zpráva se po selhání handleru zahodila bez možnosti opakování")

    def test_undelivered_message_stays_for_retry(self):
        """Zpracování proběhne bez výjimky, ale zpráva se do session nedostane.

        Doplněno po mutační kontrole 31. 7.: mutace `if doruceno is False:` → `if False:`
        prošla, protože testy pokrývaly jen pád handleru výjimkou, ne tichý nezdar. Přesně
        tenhle případ nastává, když je tmux zamrzlý – tedy ten nejčastější v provozu.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            volani = []

            def _nedoruceno(upd):
                volani.append(upd)
                return False

            b._handle = _nedoruceno
            b._handle_update_once(_msg(3000), 3000)
            for _ in range(50):
                if volani:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.3)

            self.assertTrue(volani, "handler se vůbec nespustil – špatný test")
            self.assertTrue(
                list(Path(td).glob("inbox/*")),
                "nedoručená zpráva zmizela: záznam se smazal, přestože do session nedošla",
            )
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(retained, "zpráva se po selhání handleru zahodila bez možnosti opakování")


# --------------------------------------------------------------------------------------
# C – idle konec turnu obchází pojistku
# --------------------------------------------------------------------------------------
class TurnEndBackstopTests(unittest.TestCase):
    def test_idle_turn_end_still_runs_the_backstop(self):
        """Turn ukončený tichem musí projít stejnou pojistkou jako turn ukončený hookem.

        Dnes se ze tří větví konce turnu volá pojistka jen ve dvou. Ve třetí (90 s ticha)
        se turn tiše zavře – odpověď se neodešle a v logu po tom nezůstane ani řádek.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            finished = []
            b._finish_turn = lambda: finished.append(True)
            for name in ("_maybe_reresolve", "_flush_pending", "_drain_transcript",
                         "_drain_signal", "_beat", "_status_clear"):
                setattr(b, name, lambda *a, **k: None)

            b._turn_active.set()
            b._turn_from_tg = True
            b._turn_text_sent = False
            b._last_activity = time.monotonic() - 10_000     # dávno ticho

            orig_idle = attach_mod.IDLE_DONE
            attach_mod.IDLE_DONE = 0.01
            try:
                t = threading.Thread(target=b._outbound_loop, daemon=True)
                t.start()
                time.sleep(0.8)
                b._stop.set()
                t.join(timeout=3)
            finally:
                attach_mod.IDLE_DONE = orig_idle

            self.assertTrue(
                finished,
                "turn skončil tichem a pojistka se nespustila – odpověď se ztratila bez stopy",
            )


# --------------------------------------------------------------------------------------
# D – duplicita už doručených částí dlouhé zprávy
# --------------------------------------------------------------------------------------
class ChunkRedeliveryTests(unittest.TestCase):
    def test_confirmed_chunks_are_not_resent_after_a_mid_message_failure(self):
        """Selže-li druhá část dlouhé zprávy, první část už je doručená a nesmí přijít znovu."""
        with tempfile.TemporaryDirectory() as td:
            client = _ChunkingClient(fail_on_chunk=2)
            b = _bridge(td, client=client)
            long_text = "\n".join(f"radek {i}" for i in range(3000))
            self.assertGreater(len(split_message(long_text)), 1, "text musí být dělený")

            b._send_final(long_text)          # 1. chunk projde, 2. spadne → do fronty
            b._flush_pending()                # síť zotavená → doposlat

            first = split_message(long_text)[0]
            self.assertEqual(
                client.delivered.count(first), 1,
                f"první část dorazila {client.delivered.count(first)}×; "
                "do fronty se vrací celý text místo nedoručeného zbytku",
            )


# --------------------------------------------------------------------------------------
# F – přílohy mimo durable frontu
# --------------------------------------------------------------------------------------
class AttachmentDurabilityTests(unittest.TestCase):
    def test_attachment_survives_a_failed_text_send(self):
        """Když text selže a jde do fronty, příloha musí jít s ním – ne zůstat v paměti."""
        with tempfile.TemporaryDirectory() as td:
            payload = Path(td) / "vlna.png"
            payload.write_bytes(b"PNG")

            class _FailingOnce(_Client):
                def __init__(self):
                    super().__init__()
                    self.armed = True

                def send_message(self, chat_id, text, parse_mode=None):
                    if self.armed:
                        self.armed = False
                        raise TelegramError("connection reset by peer")
                    self.sent.append(text)

            client = _FailingOnce()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            # Bez tohohle by bridge přílohu odmítl jako cestu mimo povolené složky (správně)
            # a test by měřil tu ochranu místo durability fronty.
            b.cfg.outbox_dirs = [td]

            b._send_final(f"Tady je vlna\n[tg-file] {payload}")
            b._flush_pending()

            self.assertIn("Tady je vlna", "\n".join(client.sent), "text se nedoručil ani na druhý pokus")
            # resolve() na obou stranách: macOS má /tmp jako symlink na /private/tmp a bridge
            # si cestu resolvuje. Na Linuxu je to shodné, takže porovnání platí na obou.
            self.assertEqual(
                [Path(p).resolve() for p in client.files], [payload.resolve()],
                "příloha se s frontou neuložila – po restartu by zmizela, "
                "nebo by dorazila k úplně jiné odpovědi",
            )


# --------------------------------------------------------------------------------------
# K – stav oddělený podle bota
# --------------------------------------------------------------------------------------
class StateNamespaceTests(unittest.TestCase):
    def test_two_bots_do_not_share_one_state_dir(self):
        """Dva různí boti nesmí sdílet offset ani ledger.

        Dnes to drží jen proměnná prostředí nastavená v keepalivu. Kdo bridge spustí ručně,
        dostane sdílenou cestu – přesně to jsem si 30. 7. v 17:20 nevědomky udělala sama
        a Master pak četl cizí offset.
        """
        old = os.environ.get("AGENT2TELEGRAM_STATE")
        os.environ.pop("AGENT2TELEGRAM_STATE", None)   # výchozí cesta, jako u ručního startu
        try:
            a = _state_dir(Config(agent="generic", token="111:AAA", tmux_session="a"))
            c = _state_dir(Config(agent="generic", token="222:BBB", tmux_session="b"))
        except TypeError:
            self.fail("_state_dir() nebere identitu bota – stav je společný pro všechny boty")
        finally:
            if old is not None:
                os.environ["AGENT2TELEGRAM_STATE"] = old

        self.assertNotEqual(str(a), str(c), "dva různí boti dostali stejný state dir")
        for cesta in (str(a), str(c)):
            self.assertNotIn("111:AAA", cesta, "token nesmí být v cestě – je vidět v ps i v zálohách")
            self.assertNotIn("222:BBB", cesta)

    def test_second_process_cannot_take_the_same_state_dir(self):
        """Explicitní `AGENT2TELEGRAM_STATE` má přednost (keepalive ji nastavuje per bridge),
        takže sdílení nelze vyloučit cestou. Musí ho vyloučit zámek – jinak se dva pollery
        perou o getUpdates (409) a zprávy mizí."""
        with tempfile.TemporaryDirectory() as td:
            zamek = Path(td) / "bridge.lock"
            with single_instance_lock(zamek):
                with self.assertRaises(AlreadyRunning):
                    with single_instance_lock(zamek):
                        self.fail("druhá instance nad stejným stavem se neměla spustit")
            # po uvolnění musí jít zámek vzít znovu, jinak by restart bridge zablokoval sám sebe
            with single_instance_lock(zamek):
                pass


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------------------
# Druhé kolo – nálezy z křížové recenze (Fable F2, Sol #6)
# --------------------------------------------------------------------------------------
class OutboxBlockingTests(unittest.TestCase):
    def test_permanently_rejected_file_does_not_block_later_replies(self):
        """Příloha, kterou nemá smysl opakovat, nesmí ucpat FIFO frontu.

        Recenze Fable F2: `send_file` hází TelegramError i pro TRVALÉ případy (soubor nad
        50 MB, smazaný soubor, HTTP 400). Drain je bral jako přechodné a držel záznam na
        hlavě fronty, takže se donekonečna opakoval a VŠECHNY další odpovědi Petrovi
        nedorazily – třída „Telegram nefunguje celý den".
        """
        with tempfile.TemporaryDirectory() as td:
            velky = Path(td) / "klip.mov"
            velky.write_bytes(b"x")

            class _RejectsFile(_Client):
                def send_file(self, chat_id, path, caption=None, **kw):
                    raise TelegramError("file is too big")

            client = _RejectsFile()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            b.cfg.outbox_dirs = [td]

            b._send_final(f"[tg-file] {velky}")
            b._send_final("tahle zpráva musí dorazit i tak")
            for _ in range(6):        # strop pokusů se musí stihnout vyčerpat
                b._flush_pending()

            self.assertIn("tahle zpráva musí dorazit i tak", "\n".join(client.sent),
                          "zaseklá příloha zablokovala všechny další odpovědi")
            self.assertTrue(any("Couldn't send" in s for s in client.sent),
                            "odmítnutá příloha se má ohlásit, ne tiše zmizet")
            # Vzdaná příloha NESMÍ skončit zapsaná jako doručená – to by byla tichá ztráta
            # v naší vlastní evidenci (regrese, kterou našel Sol ve třetím kole).
            # Patří do dead-letter, kde je dohledatelná.
            dead = list((Path(td) / "dead-letter").rglob("*.json"))
            self.assertTrue(dead, "vzdaná příloha se neuložila do dead-letter")
            self.assertNotIn(str(velky), [str(p) for p in client.files],
                             "příloha se nikdy neodeslala, takže nesmí být vedená jako odeslaná")

    def test_file_only_reply_goes_through_the_durable_queue(self):
        """Odpověď složená JEN z přílohy musí jít frontou jako každá jiná.

        Recenze Sol #6: prázdný text posílal kód starou cestou ještě před frontou,
        a ta smaže cestu k souboru z paměti dřív, než se upload povede. Výpadek sítě
        pak přílohu ztratil, přestože durabilita měla být hotová.
        """
        with tempfile.TemporaryDirectory() as td:
            payload = Path(td) / "vlna.png"
            payload.write_bytes(b"PNG")

            class _FailsOnce(_Client):
                def __init__(self):
                    super().__init__()
                    self.armed = True

                def send_file(self, chat_id, path, caption=None, **kw):
                    if self.armed:
                        self.armed = False
                        raise TelegramError("connection reset by peer")
                    self.files.append(path)

            client = _FailsOnce()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            b.cfg.outbox_dirs = [td]

            b._send_final(f"[tg-file] {payload}")   # síť spadne
            b._flush_pending()                       # síť zotavená → doposlat

            self.assertEqual([Path(p).resolve() for p in client.files], [payload.resolve()],
                             "příloha bez textu se po výpadku sítě ztratila")
