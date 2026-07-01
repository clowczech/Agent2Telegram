
## Push na GitHub ~2026-07-04 (po 3denním testu)
- Commit 9504c76: fix getUpdates přechodné síťové/DNS chyby → WARNING+backoff místo ERROR (nespouští monitoring alert), ERROR až po 5× v řadě. +regresní test. attach.py+bridge.py.
- Petr (2026-07-01): "testovat ještě 3 dny, pushnout až za 3 dny". Push do petrludwig-collab/Agent2Telegram až po 3 čistých dnech.

## UPDATE 2026-07-01: fix dořešen (commit f518fac)
- První fix (9504c76) byl neúplný: síťové chyby jdou reálně jako TelegramError (ne OSError), takže je `except OSError` nechytal → padaly do `except Exception` = starý ERROR.
- f518fac: `is_network_error()` v telegram.py rozpozná síťovou příčinu i zabalenou v TelegramError; attach.py+bridge.py: ERROR jen JEDNOU za výpadek (threshold 10) + INFO při obnově; skutečné HTTP chyby (400 apod.) dál ERROR. 116 testů zelených.
- Push do petrludwig-collab/Agent2Telegram za ~3 dny (Petr 2026-07-01), commity 9504c76 + f518fac.
