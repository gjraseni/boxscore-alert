"""
Box Score Alert — MAX RASENI PLAYERS GROUP
------------------------------------------
Controlla le partite di Tulane, San Diego State e USC Upstate (basket NCAA maschile)
tramite l'API pubblica di ESPN. Quando una partita è finita e non è ancora stata
inviata, prepara un'email con recap + focus sul giocatore + box score e la manda.

Struttura:
  1. CONFIGURAZIONE   -> squadre, giocatori, email
  2. LETTURA DATI     -> fetch_* (parla con ESPN)
  3. TRASFORMAZIONE   -> parse_game (trasforma il JSON di ESPN in un dizionario semplice)
  4. REPORT           -> render_email (costruisce l'HTML dell'email)
  5. INVIO            -> send_email (Gmail via SMTP)
  6. MEMORIA          -> sent_games.json (per non inviare due volte la stessa partita)
"""

import json
import os
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. CONFIGURAZIONE
# ---------------------------------------------------------------------------
# ID ESPN delle squadre (si leggono nell'URL della pagina squadra su espn.com)
TEAMS = {
    "2655": {"name": "Tulane Green Wave", "player": "Dame Salane"},
    "21":   {"name": "San Diego State Aztecs", "player": "Luca Vincini"},
    "2908": {"name": "USC Upstate Spartans", "player": "Alessandro Perotti"},
}

# Mittente e destinatario arrivano dai "Secrets" di GitHub (non sono scritti nel codice).
# Fase di test:  mittente gjraseni@gmail.com  ->  destinatario gajaraseni@gmail.com
# Fase finale:   destinatario maxraseni@gmail.com (basta cambiare il secret RECIPIENT_EMAIL)
SENDER = (os.environ.get("SENDER_EMAIL") or "").strip()
RECIPIENT = (os.environ.get("RECIPIENT_EMAIL") or "").strip()
# segreto: mai scritto nel codice. Togliamo eventuali spazi/a capo incollati per sbaglio.
APP_PASSWORD = "".join((os.environ.get("GMAIL_APP_PASSWORD") or "").split())

# TEST_MODE=1 -> invia subito l'ultima partita finita di ogni squadra (ultimi 30 giorni),
# anche se era già stata inviata, e non aggiorna la memoria. Serve per le prove.
TEST_MODE = os.environ.get("TEST_MODE") == "1"

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
STATE_FILE = Path(__file__).with_name("sent_games.json")


# ---------------------------------------------------------------------------
# 2. LETTURA DATI DA ESPN
# ---------------------------------------------------------------------------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "boxscore-alert/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_finished_games(team_id, days_back=2):
    """Restituisce gli ID ESPN delle partite FINITE della squadra negli ultimi giorni."""
    data = get_json(f"{ESPN}/teams/{team_id}/schedule")
    print(f"  ESPN squadra {team_id} = {data.get('team', {}).get('displayName', '?')}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    finished = []
    for ev in data.get("events", []):
        when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        done = ev["competitions"][0]["status"]["type"]["completed"]
        if done and when >= cutoff:
            finished.append(ev["id"])
    return finished


def fetch_summary(event_id):
    """Il 'summary' di ESPN contiene punteggio, parziali e box score completo."""
    return get_json(f"{ESPN}/summary?event={event_id}")


# ---------------------------------------------------------------------------
# 3. TRASFORMAZIONE: dal JSON di ESPN a un dizionario semplice
# ---------------------------------------------------------------------------
def parse_game(summary, team_id):
    comp = summary["header"]["competitions"][0]
    teams = {}
    for c in comp["competitors"]:
        teams[c["id"]] = {
            "name": c["team"]["displayName"],
            "abbr": c["team"]["abbreviation"],
            "score": int(c.get("score", 0)),
            "home": c["homeAway"] == "home",
            "periods": [int(p.get("displayValue", 0)) for p in c.get("linescores", [])],
        }

    players = {}
    for block in summary["boxscore"]["players"]:
        tid = block["team"]["id"]
        stat = block["statistics"][0]
        labels = stat["labels"]  # MIN, FG, 3PT, FT, OREB, DREB, REB, AST, STL, BLK, TO, PF, PTS
        rows = []
        for a in stat["athletes"]:
            row = {"name": a["athlete"]["displayName"],
                   "pos": a["athlete"].get("position", {}).get("abbreviation", ""),
                   "starter": a.get("starter", False),
                   "dnp": a.get("didNotPlay", False)}
            row.update(dict(zip(labels, a.get("stats", []))))
            rows.append(row)
        totals = dict(zip(labels, stat.get("totals", [])))
        players[tid] = {"rows": rows, "totals": totals}

    opp_id = next(t for t in teams if t != team_id)
    return {
        "date": comp["date"],
        "team": teams[team_id], "opp": teams[opp_id],
        "box": players.get(team_id, {"rows": [], "totals": {}}),
        "focus_player": TEAMS[team_id]["player"],
    }


# ---------------------------------------------------------------------------
# 4. REPORT (HTML dell'email)
# ---------------------------------------------------------------------------
COLS = ["MIN", "PTS", "REB", "AST", "FG", "3PT", "FT", "STL", "BLK", "TO", "PF"]
COL_IT = {"MIN": "Min", "PTS": "Pt", "REB": "Rim", "AST": "Ass", "FG": "Tiri",
          "3PT": "Da 3", "FT": "TL", "STL": "Rub", "BLK": "Stop", "TO": "Perse", "PF": "Falli"}


def recap_text(g):
    t, o = g["team"], g["opp"]
    won = t["score"] > o["score"]
    verb = "batte" if won else "perde contro"
    where = "in casa" if t["home"] else "in trasferta"
    rows = [r for r in g["box"]["rows"] if not r["dnp"]]
    top = max(rows, key=lambda r: int(r.get("PTS", 0) or 0), default=None)
    margin = abs(t["score"] - o["score"])
    s = f"{t['name']} {verb} {o['name']} {t['score']}-{o['score']} {where}"
    s += " in volata." if margin <= 3 else "."
    if top:
        s += f" Miglior marcatore: {top['name']} con {top['PTS']} punti."
    return s


def focus_block(g):
    name = g["focus_player"]
    row = next((r for r in g["box"]["rows"] if r["name"].lower() == name.lower()), None)
    if row is None or row["dnp"]:
        return (f"<div class='focus none'><b>{name}</b> non è sceso in campo "
                f"in questa partita.</div>")
    cells = "".join(f"<div class='kpi'><span>{row.get(k, '–')}</span>{COL_IT[k]}</div>"
                    for k in ["MIN", "PTS", "REB", "AST", "FG", "3PT"])
    return f"<div class='focus'><h3>{name}</h3><div class='kpis'>{cells}</div></div>"


def render_email(g):
    t, o = g["team"], g["opp"]
    head = "".join(f"<th>{COL_IT[c]}</th>" for c in COLS)
    body = ""
    for r in g["box"]["rows"]:
        if r["dnp"]:
            continue
        hl = " class='hl'" if r["name"].lower() == g["focus_player"].lower() else ""
        body += (f"<tr{hl}><td class='n'>{r['name']} <small>{r['pos']}</small></td>"
                 + "".join(f"<td>{r.get(c, '–')}</td>" for c in COLS) + "</tr>")
    tot = g["box"]["totals"]
    body += ("<tr class='tot'><td class='n'>Totale</td>"
             + "".join(f"<td>{tot.get(c, '')}</td>" for c in COLS) + "</tr>")
    per = " · ".join(f"{i+1}° T {a}-{b}" for i, (a, b) in enumerate(zip(t["periods"], o["periods"])))
    result = "VITTORIA" if t["score"] > o["score"] else "SCONFITTA"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#f3f1ec;font-family:Helvetica,Arial,sans-serif;color:#1c1c1c}}
.wrap{{max-width:680px;margin:0 auto;background:#fff}}
.top{{background:#14213d;color:#fff;padding:22px 26px}}
.top small{{letter-spacing:.12em;opacity:.7;font-size:11px}}
.score{{display:flex;justify-content:space-between;align-items:center;margin-top:12px}}
.score .t{{font-size:15px}} .score .s{{font-size:34px;font-weight:700}}
.badge{{display:inline-block;margin-top:10px;padding:3px 10px;border-radius:3px;font-size:11px;
letter-spacing:.1em;background:{'#2e7d4f' if result=='VITTORIA' else '#a33b2b'}}}
.sec{{padding:18px 26px}} .sec h2{{font-size:12px;letter-spacing:.12em;color:#777;margin:0 0 8px}}
.focus{{border-left:4px solid #fca311;background:#fff8ec;padding:12px 16px}}
.focus h3{{margin:0 0 8px;font-size:17px}} .focus.none{{color:#555}}
.kpis{{display:flex;gap:8px;flex-wrap:wrap}}
.kpi{{background:#fff;border:1px solid #eadfc8;border-radius:4px;padding:6px 10px;font-size:11px;
color:#777;text-align:center;min-width:52px}} .kpi span{{display:block;font-size:18px;color:#1c1c1c;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#f3f1ec;font-weight:600;padding:6px 4px;text-align:center}}
td{{padding:6px 4px;text-align:center;border-bottom:1px solid #eee}}
td.n{{text-align:left;white-space:nowrap}} td small{{color:#999}}
tr.hl td{{background:#fff3d6;font-weight:700}} tr.tot td{{font-weight:700;border-top:2px solid #14213d}}
.foot{{padding:14px 26px 22px;font-size:11px;color:#999}}
</style></head><body><div class="wrap">
<div class="top"><small>BOX SCORE · NCAA MEN'S BASKETBALL · {g['date'][:10]}</small>
<div class="score"><div><div class="t">{t['name']}</div><div class="s">{t['score']}</div></div>
<div style="opacity:.5">vs</div>
<div style="text-align:right"><div class="t">{o['name']}</div><div class="s">{o['score']}</div></div></div>
<span class="badge">{result}</span> <span style="font-size:12px;opacity:.75;margin-left:8px">{per}</span></div>
<div class="sec"><h2>RECAP</h2><p style="margin:0;line-height:1.5">{recap_text(g)}</p></div>
<div class="sec"><h2>FOCUS GIOCATORE</h2>{focus_block(g)}</div>
<div class="sec"><h2>BOX SCORE · {t['name'].upper()}</h2>
<table><tr><th style="text-align:left">Giocatore</th>{head}</tr>{body}</table></div>
<div class="foot">Dati: ESPN · Report automatico per Max Raseni Players Group</div>
</div></body></html>"""


# ---------------------------------------------------------------------------
# 5. INVIO EMAIL
# ---------------------------------------------------------------------------
def send_email(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"Box Score Bot <{SENDER}>", RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))
    errors = []
    # Gmail accetta due modalità: SSL sulla porta 465 oppure STARTTLS sulla 587.
    for mode in ("ssl465", "starttls587"):
        try:
            if mode == "ssl465":
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                s.ehlo(); s.starttls(); s.ehlo()
            with s:
                s.login(SENDER, APP_PASSWORD)
                s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError("Gmail ha RIFIUTATO le credenziali: controlla SENDER_EMAIL e "
                               f"GMAIL_APP_PASSWORD ({e.smtp_code} {e.smtp_error!r})")
        except Exception as e:
            errors.append(f"{mode}: {type(e).__name__}: {e}")
    raise RuntimeError("Invio fallito -> " + " | ".join(errors))


# ---------------------------------------------------------------------------
# 6. PROGRAMMA PRINCIPALE
# ---------------------------------------------------------------------------
def main():
    if not (SENDER and RECIPIENT and APP_PASSWORD):
        raise SystemExit("Mancano SENDER_EMAIL / RECIPIENT_EMAIL / GMAIL_APP_PASSWORD nei Secrets.")
    sent = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    print("Modalità TEST" if TEST_MODE else "Modalità normale")
    print(f"Mittente: {SENDER} | Destinatario: {RECIPIENT} | "
          f"Password app: {len(APP_PASSWORD)} caratteri (devono essere 16)")
    for team_id, info in TEAMS.items():
        try:
            games = fetch_finished_games(team_id, days_back=30 if TEST_MODE else 2)
            if TEST_MODE:
                games = games[-1:]  # solo l'ultima partita
            for event_id in games:
                key = f"{event_id}-{team_id}"
                if key in sent and not TEST_MODE:
                    continue  # già inviata
                g = parse_game(fetch_summary(event_id), team_id)
                subject = (f"🏀 {g['team']['abbr']} {g['team']['score']}-{g['opp']['score']} "
                           f"{g['opp']['abbr']} · {info['player']}")
                if TEST_MODE:
                    subject = "[TEST] " + subject
                send_email(subject, render_email(g))
                sent.add(key)
                print("  Inviata:", subject)
            if not games:
                print(f"  Nessuna partita nuova per {info['name']}")
        except Exception as e:  # un errore su una squadra non blocca le altre
            print(f"  ERRORE con {info['name']}: {type(e).__name__}: {e}")
    if not TEST_MODE:
        STATE_FILE.write_text(json.dumps(sorted(sent), indent=1))


if __name__ == "__main__":
    main()
