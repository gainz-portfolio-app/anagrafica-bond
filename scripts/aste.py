"""Titoli di Stato ordinari dai comunicati delle aste del Tesoro (BTP, BTP Green, CTZ, BTP€i): data di emissione
(godimento) e prima cedola irregolare ("Prima cedola corta … tasso lordo pari a 0,750000%"), che conta finché non è
pagata. Si leggono i comunicati dell'ultimo anno, una volta sola ciascuno (cache in data/aste/)."""

import hashlib
import html
import json
import os
import re
import time

import mef

PAGES = [
    "/it/debito_pubblico/emissioni_titoli_di_stato_interni/comunicazioni_emissioni_medio_lungo_termine/",
    "/it/debito_pubblico/emissioni_titoli_di_stato_interni/comunicazioni_emissioni_btp_green/",
    "/it/debito_pubblico/emissioni_titoli_di_stato_interni/comunicazioni_emissioni_ctz_e_btpei/",
]

PROMPT = """Dal comunicato del Ministero dell'Economia che annuncia un'asta di titoli di Stato, estrai in JSON tutti i
titoli della tabella:

{"titles": [{
  "type": "BTP 3 Anni",                // tipologia come scritta
  "isin": "IT..." | null,              // null se "da attribuire"
  "tranche": 3,
  "issue_date": "AAAA-MM-GG",          // riga "Emissione" (godimento)
  "maturity": "AAAA-MM-GG",
  "coupon_rate": 3.00 | null,          // cedola annuale in %, null per zero coupon (CTZ)
  "first_coupon_date": "AAAA-MM-GG" | null,   // riga "Data pagamento cedola"
  "first_coupon_rate": 0.75 | null,    // SOLO se la nota dice "prima cedola corta/lunga": tasso lordo del periodo
  "first_coupon_kind": "corta" | "lunga" | null
}]}

I numeri con il punto decimale (0,750000% -> 0.75). Non inventare: se un dato non c'è, null. Rispondi solo con il JSON."""


def documents(since):
    """Comunicati d'asta dal giorno `since` (data nel nome del file)."""
    links = set()
    for page in PAGES:
        body = mef.fetch(mef.SITE + page).decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+\.pdf)"', body):
            name = href.rsplit("/", 1)[1]
            day = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", name)
            if day and f"{day.group(3)}-{day.group(2)}-{day.group(1)}" >= since:
                links.add(mef.SITE + html.unescape(href))
    return sorted(links)


def first_page(text):
    """La tabella e le note stanno prima delle regole dell'asta."""
    end = text.find("meccanismo di collocamento")
    return re.sub(r"[ \t]+", " ", text[:end if end > 0 else 5000])[:5000]


def valid(title):
    date = re.compile(r"\d{4}-\d{2}-\d{2}$")
    if not date.match(str(title.get("issue_date") or "")) or not date.match(str(title.get("maturity") or "")):
        return False
    if title.get("isin") and not mef.isin_ok(title["isin"]):
        return False
    rate = title.get("first_coupon_rate")
    return rate is None or (isinstance(rate, (int, float)) and 0 < rate < 10 and date.match(str(title.get("first_coupon_date") or "")))


def load(folder, api_key, since):
    """Titoli letti dai comunicati (dalla cache o nuovi). Restituisce (titoli, avvisi)."""
    os.makedirs(folder, exist_ok=True)
    titles, warnings = [], []
    for link in documents(since):
        name = link.rsplit("/", 1)[1]
        path = os.path.join(folder, re.sub(r"\.pdf$", ".json", name))
        if os.path.exists(path):
            titles += json.load(open(path))["titles"]
            continue
        if not api_key:
            warnings.append(f"Asta {name}: nessuna chiave per leggerla")
            continue
        try:
            text = first_page(mef.pdf_text(mef.fetch(link)))
            answer = mef.extract([text], api_key, prompt=PROMPT)
        except Exception as error:  # un comunicato non letto non ferma gli altri
            warnings.append(f"Asta {name}: non letta ({error})")
            continue
        finally:
            time.sleep(mef.PAUSE)
        good = [t for t in answer.get("titles") or [] if valid(t)]
        if len(good) != len(answer.get("titles") or []):
            warnings.append(f"Asta {name}: {len(answer.get('titles') or []) - len(good)} titoli scartati")
        with open(path, "w") as handle:
            json.dump({"document": link, "titles": good}, handle, ensure_ascii=False, indent=2)
        titles += good
    return titles, warnings
