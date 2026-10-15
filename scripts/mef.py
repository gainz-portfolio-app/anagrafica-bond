"""Emissioni retail del Tesoro (BTP Valore, Valore Insieme, Più, Futura, Italia, Italia Sì) dai documenti ufficiali
del MEF: decreti di emissione e di accertamento, schede informative e tassi minimi dell'emissione in corso.

I PDF sono documenti pubblici: li legge un modello AI (Groq, chiave del progetto nei segreti dell'Action), che
restituisce i campi in una struttura fissa; ogni risposta si controlla (`validate`) e si conserva in data/mef/, così un
documento già letto non si rilegge."""

import hashlib
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

SITE = "https://www.dt.mef.gov.it"
PAGES = [
    "/it/debito_pubblico/emissioni_retail/btp_valore/comunicazioni_btp_valore/",
    "/it/debito_pubblico/emissioni_retail/btp_valore/doc_legale_btp_valore/decreti_emissione/",
    "/it/debito_pubblico/emissioni_retail/btp_valore/doc_legale_btp_valore/decreti_accertamento/",
    "/it/debito_pubblico/emissioni_retail/btp_futura/doc_legale_btp_futura/decreti_emissione/",
    "/it/debito_pubblico/emissioni_retail/btp_futura/doc_legale_btp_futura/decreti_accertamento/",
    "/it/debito_pubblico/normativa/doc_legale_btp_italia/decreti_di_emissione.html",
    "/it/debito_pubblico/normativa/doc_legale_btp_italia/decreti_di_accertamento.html",
    "/it/debito_pubblico/emissioni_retail/btp_italia_si/doc_legale_btp_italia_si/decreti_di_emissione.html",
    "/it/debito_pubblico/emissioni_retail/btp_italia_si/doc_legale_btp_italia_si/decreti_di_accertamento.html",
]
# Documenti utili; FAQ, note per gli operatori e information memorandum no. I nomi dei file non sono uniformi
# ("Decreto-di-emissione", "Decreto_di_Emissione", "Decreto-accertamento").
USEFUL = re.compile(r"(Decreto[-_ ](di[-_ ])?(emissione|accertamento)|Scheda-Informativa|Tassi-cedolari)", re.I)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
PAUSE = float(os.environ.get("MEF_PAUSE", "20"))


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "gainz-anagrafica (+https://github.com/gainz-portfolio-app)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def product_of(name):
    if re.search(r"Italia[-_ ]Si", name, re.I):
        return "BTP Italia Sì"
    for word, product in (("Insieme", "BTP Valore Insieme"), ("Futura", "BTP Futura"), ("Italia", "BTP Italia"),
                          ("Valore", "BTP Valore")):
        if word.lower() in name.lower():
            return product
    return "BTP Più" if re.search(r"Pi[uù]", name) else None


def documents(today):
    """Link ai PDF utili, raggruppati per emissione (prodotto + scadenza scritti nel nome del file); le emissioni già
    scadute si saltano."""
    links = set()
    for page in PAGES:
        body = fetch(SITE + page).decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+\.pdf)"', body):
            if USEFUL.search(href):
                links.add(SITE + html.unescape(href))
    issues = {}
    for link in sorted(links):
        name = link.rsplit("/", 1)[1]
        maturity = re.search(r"(\d{2})[._](\d{2})[._](\d{4})", name)
        product = product_of(name)
        if not maturity or not product:
            continue
        iso = f"{maturity.group(3)}-{maturity.group(2)}-{maturity.group(1)}"
        if iso < today:
            continue
        key = f"{product}|{iso}"
        issues.setdefault(key, []).append(link)
    return issues


def pdf_text(data):
    """Testo del PDF con pdftotext (poppler)."""
    result = subprocess.run(["pdftotext", "-", "-"], input=data, capture_output=True, check=True)
    return result.stdout.decode("utf-8", "replace")


# Righe con i dati che servono; i decreti sono lunghi (premesse "VISTO…", commissioni, regolamento) e il piano gratuito
# di Groq accetta poche migliaia di token a richiesta.
RELEVANT = re.compile(r"(%|godimento|scadenz|cedol|tass|ISIN|IT0\d|premio|fedelt|period|pagabil|rimborso|indicizz|"
                      r"inflazion|FOI|HICP|PIL)", re.I)


def condense(text, limit=6000):
    """Solo le righe con dati utili, con la riga prima e quella dopo per il contesto; senza le premesse."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    keep = set()
    for i, line in enumerate(lines):
        if line.upper().startswith(("VISTO", "VISTA", "VISTI", "CONSIDERATO", "RITENUTO")):
            continue
        if RELEVANT.search(line):
            keep.update({i - 1, i, i + 1})
    return "\n".join(lines[i] for i in sorted(keep) if 0 <= i < len(lines))[:limit]


PROMPT = """Sei un esperto di titoli di Stato italiani. Dai documenti ufficiali del Ministero dell'Economia (decreti,
schede informative, comunicati) di UNA emissione retail, estrai i dati in JSON con esattamente queste chiavi:

{
  "product": "BTP Valore" | "BTP Valore Insieme" | "BTP Più" | "BTP Futura" | "BTP Italia" | "BTP Italia Sì",
  "issue_date": "AAAA-MM-GG",          // data di godimento
  "settlement_date": "AAAA-MM-GG" | null,
  "maturity": "AAAA-MM-GG",
  "frequency": 4,                     // cedole all'anno (trimestrali = 4, semestrali = 2)
  "coupon_dates": ["MM-GG", ...],     // giorni dell'anno in cui si pagano le cedole
  "rates_final": true | false,        // true se i tassi sono quelli DEFINITIVI (decreto di accertamento)
  "steps": [                          // tassi annui lordi per periodo, in ordine
    {"first_coupon": "AAAA-MM-GG", "last_coupon": "AAAA-MM-GG", "rate": 4.10}
  ],
  "inflation_index": "FOI senza tabacchi" | "HICP senza tabacchi" | null,  // titoli indicizzati: i tassi sono reali
  "loyalty_premium_percent": 0.5 | null,   // premio finale fisso, % del nominale (null se dipende da altro)
  "loyalty_premium_rule": "testo" | null,  // se il premio non è fisso (es. legato alla crescita del PIL), come si calcola
  "maxi_coupon": {"date": "AAAA-MM-GG", "rate": 0.0} | null,  // cedola o premio straordinario, se c'è
  "isin_with_premium": "IT..." | null,     // codice ISIN con premio (collocamento)
  "isin_without_premium": "IT..." | null,  // codice ISIN di mercato
  "notes": "testo breve, solo se c'è qualcosa di particolare"
}

Regole: i tassi sono numeri con il punto decimale (4,10% -> 4.10). Se ci sono sia i tassi minimi garantiti sia quelli
definitivi, usa i definitivi e metti rates_final true. Non inventare: se un dato non c'è, null. Rispondi solo con il JSON."""


def extract(texts, api_key, attempts=4, prompt=None):
    """Una richiesta a Groq; con "troppe richieste" (429) aspetta quanto chiede il servizio e riprova."""
    for attempt in range(attempts):
        try:
            return request_extraction(texts, api_key, prompt or PROMPT)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(min(float(error.headers.get("retry-after") or 30), 90))


def request_extraction(texts, api_key, prompt):
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": "\n\n=====\n\n".join(texts)}],
    }).encode()
    request = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "User-Agent": "gainz-anagrafica"})
    with urllib.request.urlopen(request, timeout=120) as response:
        content = json.load(response)["choices"][0]["message"]["content"]
    return json.loads(content)


def isin_ok(code):
    """Cifra di controllo dell'ISIN (lettere → numeri, poi Luhn)."""
    if not code or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", code):
        return False
    digits = "".join(str(int(c, 36)) for c in code[:-1])
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(code[-1])


def validate(issue, maturity):
    """Problemi della risposta; lista vuota = buona."""
    problems = []
    date = re.compile(r"\d{4}-\d{2}-\d{2}$")
    if issue.get("maturity") != maturity:
        problems.append(f"scadenza {issue.get('maturity')} invece di {maturity}")
    if not date.match(str(issue.get("issue_date") or "")):
        problems.append("data di godimento mancante")
    if issue.get("frequency") not in (1, 2, 4):
        problems.append(f"frequenza {issue.get('frequency')}")
    steps = issue.get("steps") or []
    if not steps:
        problems.append("nessun tasso")
    previous = None
    for step in steps:
        rate = step.get("rate")
        if not isinstance(rate, (int, float)) or not 0 < rate < 15:
            problems.append(f"tasso {rate}")
        if not date.match(str(step.get("first_coupon") or "")) or not date.match(str(step.get("last_coupon") or "")):
            problems.append("date di un periodo mancanti")
        elif step["first_coupon"] > step["last_coupon"] or (previous and step["first_coupon"] <= previous):
            problems.append("periodi non in ordine")
        else:
            previous = step["last_coupon"]
    if steps and steps[-1].get("last_coupon") != maturity:
        problems.append("l'ultimo periodo non arriva alla scadenza")
    for key in ("isin_with_premium", "isin_without_premium"):
        if issue.get(key) and not isin_ok(issue[key]):
            problems.append(f"{key} non valido: {issue[key]}")
    return problems


def load(folder, api_key, today):
    """Emissioni lette, dalla cache o dai documenti nuovi. Restituisce (emissioni, avvisi)."""
    os.makedirs(folder, exist_ok=True)
    issues, warnings = [], []
    for key, links in documents(today).items():
        product, maturity = key.split("|")
        fingerprint = hashlib.sha256("\n".join(sorted(links)).encode()).hexdigest()[:16]
        path = os.path.join(folder, f"{product.replace(' ', '-')}-{maturity}.json")
        cached = json.load(open(path)) if os.path.exists(path) else None
        if cached and cached.get("fingerprint") == fingerprint:
            issues.append(cached)
            continue
        if not api_key:
            warnings.append(f"{key}: documenti nuovi ma nessuna chiave per leggerli")
            if cached:
                issues.append(cached)
            continue
        try:
            texts = [f"Documento: {link.rsplit('/', 1)[1]}\n\n{condense(pdf_text(fetch(link)))}" for link in links]
            issue = extract(texts, api_key)
        except Exception as error:  # un'emissione non letta non ferma le altre
            warnings.append(f"{key}: documenti non letti ({error})")
            if cached:
                issues.append(cached)
            continue
        finally:
            time.sleep(PAUSE)  # limite di token al minuto del piano gratuito
        problems = validate(issue, maturity)
        # Emissione annunciata ma senza tassi (il Tesoro li comunica pochi giorni prima del collocamento): in attesa,
        # non un errore; si conserva per non rileggere gli stessi documenti.
        if not issue.get("steps") and not any("ccertamento" in link for link in links):
            issue.update({"product": product, "maturity": maturity, "pending": True, "fingerprint": fingerprint,
                          "documents": sorted(links)})
            with open(path, "w") as handle:
                json.dump(issue, handle, ensure_ascii=False, indent=2)
            issues.append(issue)
            continue
        if problems:
            warnings.append(f"{key}: risposta scartata ({'; '.join(problems)})")
            if cached:
                issues.append(cached)
            continue
        issue.update({"product": product, "fingerprint": fingerprint, "documents": sorted(links)})
        with open(path, "w") as handle:
            json.dump(issue, handle, ensure_ascii=False, indent=2)
        issues.append(issue)
    # Emissioni già lette che il Tesoro non linka più (pagine riordinate, documenti archiviati): restano dalla cache
    # finché il titolo non scade, altrimenti tornerebbero ai soli dati FIRDS (senza scalini né premio).
    listed = {(issue["product"], issue["maturity"]) for issue in issues}
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        cached = json.load(open(os.path.join(folder, name)))
        key = (cached.get("product"), cached.get("maturity"))
        if None in key or key in listed or cached["maturity"] < today or cached.get("pending"):
            continue
        issues.append(cached)
        listed.add(key)
    return issues, warnings
