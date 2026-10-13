"""Obbligazioni da FIRDS (ESMA): l'ultimo file completo (FULINS_D, il sabato) più le variazioni giornaliere (DLTINS)
uscite dopo. Un record per ISIN, con l'elenco dei mercati; i titoli scaduti si scartano."""

import json
import os
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

SOLR = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
NS_FULL = "{urn:iso:std:iso:20022:tech:xsd:auth.017.001.02}"
NS_DELTA = "{urn:iso:std:iso:20022:tech:xsd:auth.036.001.03}"
# Ogni mercato manda a ESMA il suo nome del titolo: si preferisce quello dei mercati italiani, poi quello letto per primo.
NAME_VENUES = ["MOTX", "ETLX", "XMOT", "HMTF"]


def name_rank(venue):
    return NAME_VENUES.index(venue) if venue in NAME_VENUES else len(NAME_VENUES)


def merge(bonds, data, venue, overwrite):
    """Aggiunge il record di un mercato al titolo: nome e nome breve dal mercato preferito; gli altri campi dal primo
    record, o dall'ultimo se `overwrite` (variazioni)."""
    bond = bonds.get(data["isin"])
    if bond is None:
        bonds[data["isin"]] = {**data, "venues": {venue} if venue else set(), "name_rank": name_rank(venue)}
        return
    if venue:
        bond["venues"].add(venue)
    if name_rank(venue) < bond["name_rank"]:
        bond.update({"name": data["name"], "short": data["short"], "name_rank": name_rank(venue)})
    for key, value in data.items():
        if key in ("isin", "name", "short") or value is None:
            continue
        if overwrite or bond.get(key) is None:
            bond[key] = value


def files(file_type, since, until):
    """File FIRDS di un tipo pubblicati tra due date (comprese)."""
    query = {
        "q": "*",
        "fq": [f"file_type:{file_type}",
               f"publication_date:[{since.isoformat()}T00:00:00Z TO {until.isoformat()}T23:59:59Z]"],
        "wt": "json", "rows": "500", "fl": "file_name,download_link,publication_date",
    }
    url = SOLR + "?" + urllib.parse.urlencode(query, doseq=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        docs = json.load(response)["response"]["docs"]
    return sorted(docs, key=lambda d: d["file_name"])


def download(url, folder):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        urllib.request.urlretrieve(url, path)
    return path


def text(element, path, ns):
    found = element.find(path.replace("{}", ns))
    return found.text if found is not None else None


def record(element, ns):
    """Campi utili di un titolo di debito (o None se non è un titolo di debito)."""
    general = element.find(ns + "FinInstrmGnlAttrbts")
    if general is None:
        return None, None
    cfi = text(general, "{}ClssfctnTp", ns) or ""
    if not cfi.startswith("D"):
        return None, None
    debt = element.find(ns + "DebtInstrmAttrbts")
    rate = index = spread = maturity = unit = None
    if debt is not None:
        maturity = text(debt, "{}MtrtyDt", ns)
        unit = text(debt, "{}NmnlValPerUnit", ns)
        rate = text(debt, "{}IntrstRate/{}Fxd", ns)
        floating = debt.find(f"{ns}IntrstRate/{ns}Fltg")
        if floating is not None:
            index = text(floating, ".//{}Nm", ns) or text(floating, ".//{}Indx", ns)
            spread = text(floating, "{}BsisPtSprd", ns)
    venue = text(element, "{}TradgVnRltdAttrbts/{}Id", ns)
    data = {
        "isin": text(general, "{}Id", ns),
        "name": text(general, "{}FullNm", ns),
        "short": text(general, "{}ShrtNm", ns),
        "cfi": cfi,
        "ccy": text(general, "{}NtnlCcy", ns),
        "issuer": text(element, "{}Issr", ns),
        "maturity": maturity,
        "rate": rate,
        "index": index,
        "spread": spread,
        "unit": unit,
    }
    return data, venue


def read_full(paths, bonds):
    for path in paths:
        with zipfile.ZipFile(path) as archive, archive.open(archive.namelist()[0]) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag != NS_FULL + "RefData":
                    continue
                data, venue = record(element, NS_FULL)
                if data:
                    merge(bonds, data, venue, overwrite=False)
                element.clear()


def read_delta(paths, bonds):
    """Nuovi e modificati aggiornano il titolo e aggiungono il mercato; terminati e cancellati lo tolgono."""
    kinds = {NS_DELTA + k: k for k in ("NewRcrd", "ModfdRcrd", "TermntdRcrd", "CancRcrd")}
    for path in paths:
        with zipfile.ZipFile(path) as archive, archive.open(archive.namelist()[0]) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                kind = kinds.get(element.tag)
                if kind is None:
                    continue
                data, venue = record(element, NS_DELTA)
                if data:
                    if kind in ("NewRcrd", "ModfdRcrd"):
                        merge(bonds, data, venue, overwrite=True)
                    elif data["isin"] in bonds and venue:
                        bonds[data["isin"]]["venues"].discard(venue)
                        if not bonds[data["isin"]]["venues"]:
                            del bonds[data["isin"]]
                element.clear()


def load(today, cache):
    """Ultimo completo dei titoli di debito più le variazioni successive. Restituisce (titoli, data dei dati)."""
    full = [f for f in files("FULINS", today - timedelta(days=14), today) if f["file_name"].startswith("FULINS_D_")]
    if not full:
        raise SystemExit("FIRDS: nessun file completo dei titoli di debito nelle ultime due settimane")
    latest = max(f["publication_date"][:10] for f in full)
    full = [f for f in full if f["publication_date"][:10] == latest]
    bonds = {}
    read_full([download(f["download_link"], cache) for f in full], bonds)
    start = date.fromisoformat(latest) + timedelta(days=1)
    deltas = files("DLTINS", start, today) if start <= today else []
    read_delta([download(f["download_link"], cache) for f in deltas], bonds)
    data_date = max([latest] + [f["publication_date"][:10] for f in deltas])
    alive = {isin: b for isin, b in bonds.items() if not b["maturity"] or b["maturity"] >= today.isoformat()}
    return alive, data_date
