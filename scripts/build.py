"""Costruisce l'anagrafica: FIRDS (tutte le obbligazioni) + emissioni retail del MEF (step-up, premio, doppio ISIN) +
correzioni a mano (data/aggiunte.csv). Scrive dist/anagrafica.csv.gz e dist/manifest.json.

Uso: python3 scripts/build.py [--today AAAA-MM-GG] [--cache cartella]   (GROQ_API_KEY nell'ambiente per il MEF)"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import firds  # noqa: E402
import mef  # noqa: E402

ITALY_LEI = "815600DE60799F5A9309"
ITALIAN_VENUES = {"MOTX", "ETLX", "HMTF", "XMOT"}
COLUMNS = ["isin", "name", "short", "cfi", "currency", "issuer_lei", "maturity", "rate", "index", "spread", "unit",
           "venues", "kind", "product", "issue_date", "frequency", "steps", "inflation_index", "loyalty_premium",
           "loyalty_premium_rule", "maxi_coupon", "pair_isin", "pair_role", "source"]
# Parole del nome breve FIRDS di ogni famiglia retail ("ITALIA/TV BTP FUTURA 20281117 EX").
PRODUCT_WORDS = {"BTP Valore": "BTP VALORE", "BTP Valore Insieme": "BTP VALORE INS", "BTP Più": "BTP PIU",
                 "BTP Futura": "BTP FUTURA", "BTP Italia": "BTP ITALIA", "BTP Italia Sì": "BTP ITALIA SI"}
ATTRIBUTION = ("Fonti: ESMA, Financial Instruments Reference Data System (FIRDS); Ministero dell'Economia e delle "
               "Finanze, Dipartimento del Tesoro. Dati filtrati, uniti per ISIN e integrati da Gainz: non sono i dati "
               "originali e ESMA e MEF non sono responsabili di questa elaborazione.")


def kind(bond):
    """Tipo di interesse: indicizzati all'inflazione dei titoli di Stato italiani dal nome breve (BTP€i, BTP Italia),
    poi dal codice CFI (terza lettera): fisso, zero coupon, variabile."""
    short = (bond["short"] or "").upper()
    if bond["issuer"] == ITALY_LEI and "STRIP" not in short and ("BTPI " in short or "BTP ITALIA" in short):
        return "inflationLinked"
    return {"F": "fixed", "Z": "zero", "V": "variable", "K": "pik"}.get(bond["cfi"][2:3], "")


def row(bond):
    return {
        "isin": bond["isin"], "name": bond["name"] or "", "short": bond["short"] or "", "cfi": bond["cfi"],
        "currency": bond["ccy"] or "", "issuer_lei": bond["issuer"] or "", "maturity": bond["maturity"] or "",
        "rate": bond["rate"] or "", "index": bond["index"] or "", "spread": bond["spread"] or "",
        "unit": bond["unit"] or "", "venues": " ".join(sorted(bond["venues"])), "kind": kind(bond),
        "product": "", "issue_date": "", "frequency": "", "steps": "", "inflation_index": "", "loyalty_premium": "",
        "loyalty_premium_rule": "", "maxi_coupon": "",
        "pair_isin": "", "pair_role": "", "source": "firds",
    }


def pair(issue, rows):
    """ISIN con premio (collocamento) e di mercato di un'emissione: dai documenti del MEF, altrimenti da FIRDS (stesso
    emittente e scadenza, nome breve della famiglia; quello di mercato è quotato sul MOT, quello con premio no e per
    Futura e BTP Italia ha "CUM" nel nome breve, quello di mercato "EX")."""
    special, regular = issue.get("isin_with_premium"), issue.get("isin_without_premium")
    if special and regular:
        return special, regular
    # Il documento ne riporta uno solo (o nessuno): l'altro da FIRDS.
    found_special, found_regular = firds_pair(issue, rows)
    special = special or (found_special if found_special != regular else None)
    regular = regular or (found_regular if found_regular != special else None)
    return special, regular


def firds_pair(issue, rows):
    word = PRODUCT_WORDS[issue["product"]]
    candidates = [r for r in rows.values() if r["issuer_lei"] == ITALY_LEI and r["maturity"] == issue["maturity"]
                  and word in r["short"].upper() and "STRIP" not in r["short"].upper()
                  and (issue["product"] == "BTP Italia Sì" or "BTP ITALIA SI" not in r["short"].upper())]
    listed = [r for r in candidates if set(r["venues"].split()) & ITALIAN_VENUES]
    unlisted = [r for r in candidates if r not in listed]
    if len(listed) == 1 and len(unlisted) <= 1:
        return (unlisted[0]["isin"] if unlisted else None), listed[0]["isin"]
    return None, None


def apply_issue(issue, rows, warnings):
    special, regular = pair(issue, rows)
    if not special and not regular:
        warnings.append(f"{issue['product']} {issue['maturity']}: ISIN non trovati (né nei documenti né in FIRDS)")
        return
    steps = ";".join(f"{s['first_coupon']}:{s['rate']}" for s in issue["steps"])
    maxi = issue.get("maxi_coupon")
    common = {
        "kind": "inflationLinked" if issue.get("inflation_index")
        else "stepUp" if len({s["rate"] for s in issue["steps"]}) > 1 else "fixed",
        "inflation_index": issue.get("inflation_index") or "",
        "product": issue["product"], "issue_date": issue["issue_date"], "frequency": str(issue["frequency"]),
        "steps": steps, "maxi_coupon": f"{maxi['date']}:{maxi['rate']}" if maxi and maxi.get("date") else "",
        "maturity": issue["maturity"], "currency": "EUR", "issuer_lei": ITALY_LEI,
    }
    for isin, role, other in ((special, "withPremium", regular), (regular, "market", special)):
        if not isin:
            continue
        base = rows.get(isin) or {c: "" for c in COLUMNS} | {"isin": isin, "name": f"{issue['product']} {issue['maturity']}",
                                                               "source": "mef"}
        base.update(common)
        base["loyalty_premium"] = str(issue.get("loyalty_premium_percent") or "") if role == "withPremium" else ""
        base["loyalty_premium_rule"] = (issue.get("loyalty_premium_rule") or "") if role == "withPremium" else ""
        base.update({"pair_isin": other or "", "pair_role": role if other else ""})
        base["source"] = "firds+mef" if base["source"].startswith("firds") else "mef"
        rows[isin] = base


def apply_manual(path, rows):
    """Correzioni a mano: le celle piene vincono; una riga nuova aggiunge il titolo."""
    if not os.path.exists(path):
        return 0
    count = 0
    with open(path, newline="") as handle:
        for line in csv.DictReader(handle):
            isin = (line.get("isin") or "").strip().upper()
            if not isin:
                continue
            target = rows.setdefault(isin, {c: "" for c in COLUMNS} | {"isin": isin})
            target.update({k: v.strip() for k, v in line.items() if k in COLUMNS and v and v.strip()})
            target["source"] = (target["source"] + "+manual").lstrip("+") if "manual" not in target["source"] else target["source"]
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--cache", default="cache")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args()
    today = date.fromisoformat(args.today)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    bonds, firds_date = firds.load(today, args.cache)
    rows = {isin: row(bond) for isin, bond in bonds.items()}
    issues, warnings = mef.load(os.path.join(root, "data", "mef"), os.environ.get("GROQ_API_KEY"), today.isoformat())
    pending = [f"{i['product']} {i['maturity']}" for i in issues if i.get("pending")]
    for issue in issues:
        if not issue.get("pending"):
            apply_issue(issue, rows, warnings)
    manual = apply_manual(os.path.join(root, "data", "aggiunte.csv"), rows)

    os.makedirs(args.out, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for isin in sorted(rows):
        writer.writerow(rows[isin])
    data = gzip.compress(buffer.getvalue().encode(), mtime=0)
    with open(os.path.join(args.out, "anagrafica.csv.gz"), "wb") as handle:
        handle.write(data)
    manifest = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "firdsDate": firds_date,
        "count": len(rows),
        "retailIssues": len(issues) - len(pending),
        "pendingIssues": pending,
        "manualRows": manual,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "attribution": ATTRIBUTION,
        "warnings": warnings,
    }
    with open(os.path.join(args.out, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in manifest.items() if k != "attribution"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
