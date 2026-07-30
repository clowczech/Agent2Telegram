Prošel jsem uvedené soubory read-only. Nenašel jsem `shell=True`; hlavní riziko není shell quoting, ale posílání skutečných kláves do živého terminálu.

**Nálezy**

1. **Vysoká** — [session.py:100](/Users/asistent/Agent2Telegram/agent2telegram/session.py:100), [session.py:104](/Users/asistent/Agent2Telegram/agent2telegram/session.py:104)  
   `send-keys -l --` brání tmux key-name/option injection a newliny se skládají do jedné řádky, ale nefiltrují se C0/C1 kontrolní znaky. Telegram/API text s `\x03`, `\x04`, `\x1b...`, `\x15` apod. může přerušit TUI, měnit editovaný řádek nebo poslat escape sekvence.  
   Oprava: před `_send_keys()` normalizovat vstup a odmítnout/escapovat všechny kontrolní znaky kromě bezpečné mezery/tabu; zavést max délku. Pro nebezpečný obsah poslat agentovi cestu k uloženému textovému souboru místo raw kláves.

2. **Vysoká** — [attach.py:301](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:301), [session.py:88](/Users/asistent/Agent2Telegram/agent2telegram/session.py:88)  
   Attach ověřuje jen existenci tmux session, ne že v pane stále běží očekávaný agent. Pokud agent spadne nebo skončí do shellu, další Telegram zpráva se přes `Enter` provede jako shell příkaz.  
   Oprava: před každou injekcí kontrolovat `pane_current_command`/process tree proti povolenému agentovi, shell prompt fail-closed, případně session restartovat.

3. **Vysoká** — [attach.py:491](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:491), [attach.py:520](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:520)  
   Attach režim nefrontuje vstupy podle turnů; další zpráva, edit nebo reakce se injektuje i během běžící odpovědi. `C-u`, text a `Enter` pak mohou dopadnout do nestabilního TUI stavu.  
   Oprava: inbound FIFO fronta a pouze jedna aktivní injekce do potvrzeného prompt-ready stavu; další zprávy držet do `turn_end`.

4. **Vysoká** — [stream.py:178](/Users/asistent/Agent2Telegram/agent2telegram/stream.py:178), [stream.py:181](/Users/asistent/Agent2Telegram/agent2telegram/stream.py:181), [stream.py:165](/Users/asistent/Agent2Telegram/agent2telegram/stream.py:165)  
   Stream režim označí zprávu jako odeslanou před `send_message()`. Pád nebo Telegram chyba znamená trvalou ztrátu. Navíc `StreamBridge` dědí `_finish_turn()`, ale neinicializuje `_turn_text_sent`; ověřeno: `_finish_turn()` padá na `AttributeError`.  
   Oprava: používat stejnou durable send cestu jako attach, ledger zapisovat až po potvrzeném sendu, inicializovat/override `_finish_turn()` pro stream.

5. **Vysoká** — [config.py:92](/Users/asistent/Agent2Telegram/agent2telegram/config.py:92), [config.py:113](/Users/asistent/Agent2Telegram/agent2telegram/config.py:113), [__main__.py:36](/Users/asistent/Agent2Telegram/agent2telegram/__main__.py:36), [attach.py:569](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:569)  
   Token/API key z env se při `load()` vloží do `cfg` a následné `save(cfg)` ho zapíše do configu. To může porušit env-only secret model.  
   Oprava: držet metadata o zdroji secretů a při save zachovat původní hodnoty ze souboru; env secrety nikdy neserializovat.

6. **Vysoká** — [attach.py:435](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:435)  
   Attach poller začíná vždy `offset = 0` a offset nepersistuje. Crash po přijetí update, ale před potvrzením novým `getUpdates(offset)`, může znovu injektovat starou zprávu do tmux.  
   Oprava: persistovat offset/update_id ledger atomicky před side-effectem nebo deduplikovat update_id po restartu.

7. **Střední** — [attach.py:364](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:364), [attach.py:373](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:373), [attach.py:419](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:419)  
   `outbound_queue` je neomezená, jeden poškozený JSONL řádek zahodí celou frontu a crash po úspěšném Telegram sendu před `pop/persist` může způsobit duplicitní odeslání. Ledger je append-only a attach ledger je globální `attach_sent.txt`.  
   Oprava: per-bridge queue/ledger, file lock, limity velikosti, salvage validních řádků, kompakce, fsync; u crash okna počítat s idempotencí nebo explicitním “possibly sent” stavem.

8. **Střední** — [config.py:77](/Users/asistent/Agent2Telegram/agent2telegram/config.py:77), [attach.py:108](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:108), [attach.py:770](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:770), [stop_hook.py:54](/Users/asistent/Agent2Telegram/agent2telegram/stop_hook.py:54)  
   `signal_file`, stavové soubory a config cesta jsou plně důvěřované. Pokud míří do group/world-writable adresáře, lokální uživatel může podvrhnout odpověď, `turn_end`, queue nebo symlink.  
   Oprava: validovat privátní adresáře `0700`, soubory `0600`, odmítat group/world-writable parent, použít `O_NOFOLLOW`/atomic create.

9. **Střední** — [__main__.py:58](/Users/asistent/Agent2Telegram/agent2telegram/__main__.py:58), [__main__.py:83](/Users/asistent/Agent2Telegram/agent2telegram/__main__.py:83)  
   `notify` nemá vlastní autentizaci odesílatele; kdokoliv se stejným lokálním uživatelem nebo čitelným configem může poslat zprávu ownerovi. Token se zde přímo nevypisuje.  
   Oprava: vynutit owner/mode configu v `load()`, případně `notify_enabled` nebo lokální capability file s `0600`.

10. **Střední** — [bridge.py:129](/Users/asistent/Agent2Telegram/agent2telegram/bridge.py:129), [bridge.py:224](/Users/asistent/Agent2Telegram/agent2telegram/bridge.py:224), [bridge.py:282](/Users/asistent/Agent2Telegram/agent2telegram/bridge.py:282)  
   One-shot `Bridge` obsluhuje `/help`, `/id`, `/status` před autorizací a odpovědi posílá do `chat_id`. Pokud owner píše v group chatu, agent output vidí celá skupina.  
   Oprava: defaultně povolit jen private chat; přidat `allowed_chat_ids`; před auth obsloužit jen `/id`.

11. **Střední** — [attach.py:511](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:511), [attach.py:857](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:857), [telegram.py:200](/Users/asistent/Agent2Telegram/agent2telegram/telegram.py:200)  
   Attach poller blokuje na downloadu/STT v hlavním polling vlákně; download má až 120s timeout s retry. To zhorší DoS a zpozdí další updates.  
   Oprava: přesunout media/STT do worker fronty, limity na voice/audio velikost a prompt délku, rate limiting.

12. **Nízká až střední** — [attach.py:414](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:414), [attach.py:669](/Users/asistent/Agent2Telegram/agent2telegram/attach.py:669), [config.py:66](/Users/asistent/Agent2Telegram/agent2telegram/config.py:66)  
   Logují se začátky agent odpovědí a `doctor` tiskne částečný token. To může stačit k úniku citlivého obsahu v logách.  
   Oprava: nelogovat obsah zpráv, jen délku/hash/event id; token redigovat jako konstantu `"<redacted>"`.

**Návrhy na zdokonalení**

- Zavést centrální `InputSanitizer` pro Telegram text, captions, STT výstup i reakce.
- Přidat single-instance lock na config/tmux session, aby neběžely dva pollery/forwardery nad stejnou frontou.
- Queue/ledger ukládat do privátní state dir s kvótou, rotací a repair režimem.
- Pro attach routing nepoužívat veřejný prefix `Telegram:` jako jediný důkaz původu; lepší je per-turn nonce/mapa injektovaných update_id.
- Přidat testy na kontrolní znaky, crash před/po sendu, corrupt queue, env-secret save, group chat chování a stream `_finish_turn()`.