# Anagrafica obbligazioni per Gainz

File pubblico con l'anagrafica delle obbligazioni non scadute, usato dall'app Gainz per cercare un titolo per ISIN o per
nome e per i calcolatori (cedole, step-up, premi dei BTP retail).

## File

Nella release [`latest`](../../releases/tag/latest):

- `anagrafica.csv.gz`: un titolo per riga, CSV compresso. Colonne:
  - `isin`, `name`, `short`, `cfi`, `currency`, `issuer_lei`, `maturity`, `rate`, `index`, `spread`, `unit`, `venues` (codici MIC dei mercati), `kind` (`fixed`, `zero`, `variable`, `stepUp`, `inflationLinked`);
  - per le emissioni retail del Tesoro: `product`, `issue_date` (godimento), `frequency` (cedole all'anno), `steps` (`AAAA-MM-GG:tasso;…`, tasso annuo dalla cedola indicata), `inflation_index` (per gli indicizzati i tassi sono reali), `loyalty_premium` (% del nominale, solo sul codice con premio) o `loyalty_premium_rule` (se il premio non è fisso, per esempio legato al PIL nei Futura), `maxi_coupon`, `pair_isin` e `pair_role` (`withPremium` = codice del collocamento con l'extra premio, `market` = codice di mercato);
  - per i titoli di Stato ordinari (dai comunicati delle aste del Tesoro dell'ultimo anno): `issue_date`, `frequency`, `first_coupon` e `first_coupon_rate` se la prima cedola è irregolare (corta o lunga) e non ancora pagata;
  - `source`.
- `manifest.json`: data dei dati, numero di titoli, impronta SHA-256 del file, avvisi dell'ultimo aggiornamento.

## Fonti e attribuzione

- **ESMA**, Financial Instruments Reference Data System (FIRDS): file completi settimanali e variazioni giornaliere dei titoli di debito, dal [servizio file di ESMA](https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_firds_files). La riproduzione è autorizzata citando la fonte ([avviso legale](https://registers.esma.europa.eu/publication/legalNoticePage)).
- **Ministero dell'Economia e delle Finanze**, Dipartimento del Tesoro: decreti di emissione e di accertamento e schede informative delle emissioni retail: BTP Valore, Valore Insieme, Più, Futura, Italia e Italia Sì ([dt.mef.gov.it](https://www.dt.mef.gov.it/it/debito_pubblico/emissioni_retail/)). I documenti sono letti da un modello AI e i dati estratti sono controllati prima della pubblicazione.

I dati sono **filtrati, uniti per ISIN e integrati** da Gainz: non sono i dati originali, ed ESMA e MEF non sono responsabili di questa elaborazione né la approvano. Nessuna garanzia: per decisioni di investimento valgono i documenti ufficiali.

## Aggiornamento

- Automatico la domenica e il mercoledì (GitHub Actions).
- A mano: **Actions › Anagrafica obbligazioni › Run workflow**, per esempio quando il Tesoro annuncia un'emissione.
- Correzioni: `data/aggiunte.csv`, stesse colonne del file; le celle piene vincono sui dati delle fonti.
- Le emissioni lette dai documenti del Tesoro restano in `data/mef/`: un documento già letto non si rilegge, e un'emissione che il Tesoro non linka più resta dalla cache fino alla scadenza del titolo.
