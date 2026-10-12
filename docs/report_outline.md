# Report outline (PRD §13.2) — fill in, then export to `report.pdf`

Placeholders in «» are yours to write; nothing here is pre-filled evidence.

1. **Title page** — «Your name», course, date, "Syndicate City — D-Bus Multiplayer OS Project".
2. **Introduction** — premise, core rules (README §4), and the communications library: file `ipc.py`, API (README §5).
3. **Screenshots (captioned)** — take each from a real run:
   - lobby (server console + `players` command showing distinct UIDs);
   - `id` in each player terminal (distinct Linux users);
   - negotiation with `/tell` / `/group` visible only to recipients;
   - movement reveal; auction open + `AuctionClosed`; `/board` after settlement;
   - final scoreboard; optional: `adversarial_client.py all` output.
4. **Dedicated D-Bus** — how the bus authenticates (EXTERNAL / SO_PEERCRED → `GetConnectionUnixUser`), routes method calls, broadcast signals, and destination-addressed signals; the policy (README §6).
5. **Library design + limitations** — README §5 table and limitations.
6. **Feedback received** — «what classmates tried, results, what you changed».
7. **Conclusion** — «what you learned»; compare unicast vs. group fan-out vs. broadcast (cost, privacy, ordering).
8. **Feedback given (separate section)** — «projects reviewed, what you ran/inspected, findings, suggestions».

Useful evidence commands:

```bash
sudo scripts/integration_multiuser.sh          # prints PASS/FAIL list + log directory
scripts/run_tests.sh -v                        # full test listing
# operator console: `status` prints request-latency p50/p95/max for the performance section
```
