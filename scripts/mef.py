"""Emissioni retail del Tesoro (famiglia BTP Valore: Valore, Valore Insieme, Più) dai documenti ufficiali del MEF:
decreti di emissione e di accertamento, schede informative e tassi minimi dell'emissione in corso.

I PDF sono documenti pubblici: li legge un modello AI (Groq, chiave del progetto nei segreti dell'Action), che
restituisce i campi in una struttura fissa; ogni risposta si controlla (`validate`) e si conserva in data/mef/, così un
documento già letto non si rilegge."""

import hashlib
import html
import json
import os
import re
import subprocess
import urllib.request

SITE = "https://www.dt.mef.gov.it"
PAGES = [
    "/it/debito_pubblico/emissioni_retail/btp_valore/comunicazioni_btp_valore/",
    "/it/debito_pubblico/emissioni_retail/btp_valore/doc_legale_btp_valore/decreti_emissione/",
    "/it/debito_pubblico/emissioni_retail/btp_valore/doc_legale_btp_valore/decreti_accertamento/",
]
# Documenti utili; FAQ, note per gli operatori e information memorandum no.
USEFUL = re.compile(r"(Decreto-di-emissione|Decreto-di-Accertamento|Scheda-Informativa|Tassi-cedolari)", re.I)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "gainz-anagrafica (+https://github.com/gainz-portfolio-app)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def documents():
    """Link ai PDF utili, raggruppati per emissione (prodotto + scadenza scritti nel nome del file)."""
    links = set()
    for page in PAGES:
        body = fetch(SITE + page).decode("utf-8", "replace")
        for href in re.findall(r'href="([^"]+\.pdf)"', body):
            if USEFUL.search(href):
                links.add(SITE + html.unescape(href))
    issues = {}
    for link in sorted(links):
        name = link.rsplit("/", 1)[1]
        maturity = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", name)
        if not maturity:
            continue
        product = "BTP Valore Insieme" if "Insieme" in name else "BTP Più" if re.search(r"Pi[uù]", name) else "BTP Valore"
        key = f"{product}|{maturity.group(3)}-{maturity.group(2)}-{maturity.group(1)}"
        issues.setdefault(key, []).append(link)
    return issues


def pdf_text(data):
    """Testo del PDF con pdftotext (poppler)."""
    result = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True)
    return result.stdout.decode("utf-8", "replace")


PROMPT = """Sei un esperto di titoli di Stato italiani. Dai documenti ufficiali del Ministero dell'Economia (decreti,
schede informative, comunicati) di UNA emissione retail, estrai i dati in JSON con esattamente queste chiavi:

{
  "product": "BTP Valore" | "BTP Valore Insieme" | "BTP Più",
  "issue_date": "AAAA-MM-GG",          // data di godimento
  "settlement_date": "AAAA-MM-GG" | null,
  "maturity": "AAAA-MM-GG",
  "frequency": 4,                     // cedole all'anno (trimestrali = 4, semestrali = 2)
  "coupon_dates": ["MM-GG", ...],     // giorni dell'anno in cui si pagano le cedole
  "rates_final": true | false,        // true se i tassi sono quelli DEFINITIVI (decreto di accertamento)
  "steps": [                          // tassi annui lordi per periodo, in ordine
    {"first_coupon": "AAAA-MM-GG", "last_coupon": "AAAA-MM-GG", "rate": 4.10}
  ],
  "loyalty_premium_percent": 0.5 | null,   // extra premio finale di fedeltà, % del nominale
  "maxi_coupon": {"date": "AAAA-MM-GG", "rate": 0.0} | null,  // cedola o premio straordinario, se c'è
  "isin_with_premium": "IT..." | null,     // codice ISIN con premio (collocamento)
  "isin_without_premium": "IT..." | null,  // codice ISIN di mercato
  "notes": "testo breve, solo se c'è qualcosa di particolare"
}

Regole: i tassi sono numeri con il punto decimale (4,10% -> 4.10). Se ci sono sia i tassi minimi garantiti sia quelli
definitivi, usa i definitivi e metti rates_final true. Non inventare: se un dato non c'è, null. Rispondi solo con il JSON."""


def extract(texts, api_key):
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": PROMPT},
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


def load(folder, api_key):
    """Emissioni lette, dalla cache o dai documenti nuovi. Restituisce (emissioni, avvisi)."""
    os.makedirs(folder, exist_ok=True)
    issues, warnings = [], []
    for key, links in documents().items():
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
        texts = [f"Documento: {link.rsplit('/', 1)[1]}\n\n{pdf_text(fetch(link))}" for link in links]
        issue = extract(texts, api_key)
        problems = validate(issue, maturity)
        if problems:
            warnings.append(f"{key}: risposta scartata ({'; '.join(problems)})")
            if cached:
                issues.append(cached)
            continue
        issue.update({"product": product, "fingerprint": fingerprint, "documents": sorted(links)})
        with open(path, "w") as handle:
            json.dump(issue, handle, ensure_ascii=False, indent=2)
        issues.append(issue)
    return issues, warnings
