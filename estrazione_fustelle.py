#!/usr/bin/env python3
"""
Pipeline di estrazione dati da fustelle PDF (packaging MySecretCase) verso Google Sheet.

PERCHE' QUESTO APPROCCIO
-------------------------
I PDF delle fustelle sono export vettoriali da Illustrator con il testo convertito
in tracciati (outline): l'estrazione testo classica (pdfplumber, PyPDF2, ecc.)
restituisce quasi nulla (~16 caratteri/pagina). L'unico modo affidabile e' convertire
ogni pagina in immagine ad alta risoluzione e farla leggere a un modello vision,
con un prompt strutturato che forza l'output in JSON sui 33 campi richiesti.

FLUSSO
------
1. Scansiona la cartella di input, individua i PDF non ancora processati (dedup per
   codice a barre, letto dal nome file "<barcode>_<dimensioni>_<nome>.pdf").
2. Per ogni PDF: verifica che sia un vero PDF (alcuni file del batch erano testo
   semplice rinominato .pdf: li isola in una coda "corrotti" invece di farli fallire
   in silenzio).
3. Converte pagina 1 (ed eventuali altre pagine) in PNG con PyMuPDF a matrix 2x
   (~300 DPI). Se il campo "lotto" torna DA_VERIFICARE dal primo passaggio, rifa'
   SOLO quella pagina a matrix 8x (~1200 DPI) e ritenta l'estrazione: questo si e'
   dimostrato risolutivo per la maggior parte dei LOT piccoli (vedi README).
4. Manda le immagini + un prompt strutturato al modello vision (Claude API) e
   ottiene un JSON con i 33 campi.
5. Fa una validazione minima (campi obbligatori presenti, barcode coerente col
   nome file) e scrive la riga su Google Sheet via gspread, evitando duplicati.
6. Logga un report finale: quanti Completo / Parziale / Errore, e il motivo.

SETUP RICHIESTO (vedi anche README.md allegato)
------------------------------------------------
- pip install -r requirements.txt
- Variabile d'ambiente ANTHROPIC_API_KEY con una chiave Claude API valida
- Un service account Google Cloud con Google Sheets API abilitata; il file
  credentials.json va messo accanto a questo script; il Google Sheet di
  destinazione va condiviso (Editor) con l'email del service account.
- Modificare le costanti in cima a questo file (SHEET_ID, INPUT_DIR).

Questo script e' pensato per essere eseguito da riga di comando:
    python estrazione_fustelle.py --input /percorso/ai/pdf --sheet-id <ID_GOOGLE_SHEET>
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import anthropic

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False


# --------------------------------------------------------------------------- #
# CONFIGURAZIONE
# --------------------------------------------------------------------------- #

CAMPI_RICHIESTI = [
    "nome_prodotto", "tipo_prodotto", "nome_indirizzo_fabbricante",
    "nome_indirizzo_importatore", "codice_a_barre", "lotto", "materiale",
    "impermeabilita", "dimensioni", "modalita_ricarica", "n_vibrazioni",
    "n_velocita", "n_modalita_suzione_tapping_rotazione", "strap_on_compatibile",
    "funzione_riscaldante", "telecomandato", "capacita_batteria", "garanzia",
    "simbolo_ce", "simbolo_ukca", "simbolo_raee", "simbolo_triman",
    "qr_code_junker", "simbolo_libretto_informativo", "codice_smaltimento_scatola",
    "codice_smaltimento_sacchetto", "contenuto",
]

MODEL = "claude-sonnet-4-6"  # modello vision usato per l'estrazione
MATRIX_STANDARD = 2.0        # ~300 DPI, primo passaggio
MATRIX_HIRES = 8.0           # ~1200 DPI, retry mirato sul LOT
MAX_PAGES_PER_PDF = 3        # scarto di sicurezza: la maggior parte ha 1-2 pagine

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
SHEET_NAME = "estrazione_pack"  # nome del foglio (tab) dentro lo spreadsheet

LOG = logging.getLogger("estrazione_fustelle")


# --------------------------------------------------------------------------- #
# STRUTTURE DATI
# --------------------------------------------------------------------------- #

@dataclass
class RisultatoEstrazione:
    dati: dict = field(default_factory=dict)
    stato: str = "Errore"          # Completo | Parziale | Errore
    motivo: str = ""
    file_pdf: str = ""


# --------------------------------------------------------------------------- #
# STEP 1: SCOPERTA FILE ED ESTRAZIONE BARCODE DAL NOME
# --------------------------------------------------------------------------- #

def barcode_da_nome_file(path: Path) -> Optional[str]:
    """Il naming convention e' <barcode>_<LxWxH>_<nome-prodotto>.pdf"""
    m = re.match(r"^(\d{10,14})_", path.name)
    return m.group(1) if m else None


def trova_pdf_da_processare(input_dir: Path, barcode_gia_fatti: set[str]) -> list[Path]:
    tutti = sorted(input_dir.glob("*.pdf"))
    da_fare = []
    for p in tutti:
        bc = barcode_da_nome_file(p)
        if bc is None:
            LOG.warning("Nome file senza barcode riconoscibile, salto: %s", p.name)
            continue
        if bc in barcode_gia_fatti:
            LOG.info("Barcode %s gia' presente nel foglio, salto %s", bc, p.name)
            continue
        da_fare.append(p)
    return da_fare


# --------------------------------------------------------------------------- #
# STEP 2: VALIDAZIONE / RENDER PDF -> IMMAGINE
# --------------------------------------------------------------------------- #

def e_pdf_valido(path: Path) -> bool:
    """Alcuni file del batch sono testo semplice rinominato .pdf (bug a monte
    nell'export delle fustelle). Li rileviamo qui invece di farli fallire
    dentro fitz con uno stacktrace poco chiaro."""
    try:
        with open(path, "rb") as f:
            header = f.read(5)
        return header == b"%PDF-"
    except OSError:
        return False


def render_pagine(path: Path, matrix_scale: float = MATRIX_STANDARD) -> list[bytes]:
    """Rende ogni pagina del PDF come PNG (bytes) a matrix_scale."""
    immagini = []
    mat = fitz.Matrix(matrix_scale, matrix_scale)
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES_PER_PDF:
                break
            pix = page.get_pixmap(matrix=mat)
            immagini.append(pix.tobytes("png"))
    return immagini


def render_area_lotto_hires(path: Path, page_index: int = 0) -> bytes:
    """Retry mirato: ri-renderizza SOLO la pagina indicata a risoluzione molto
    piu' alta, per il caso in cui il LOT risulti illeggibile al primo giro.
    Renderizziamo l'intera pagina (non un crop) perche' non conosciamo a priori
    la posizione del LOT sul layout; il modello vision la trova comunque."""
    mat = fitz.Matrix(MATRIX_HIRES, MATRIX_HIRES)
    with fitz.open(path) as doc:
        page = doc[page_index]
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")


# --------------------------------------------------------------------------- #
# STEP 3: PROMPT ED ESTRAZIONE VISION
# --------------------------------------------------------------------------- #

PROMPT_ESTRAZIONE = """Sei un tecnico esperto di etichettatura conformita' prodotti
(regolamento REACH/RoHS/RAEE UE) che estrae dati da fustelle di packaging.

Ti mostro una o piu' immagini ad alta risoluzione della fustella (confezione stesa)
di UN singolo prodotto. Estrai ESATTAMENTE questi campi e rispondi SOLO con un
oggetto JSON valido, nessun testo prima o dopo, nessun blocco markdown:

{campi}

REGOLE:
- Se un campo non e' applicabile al prodotto (es. "n_vibrazioni" per un dildo senza
  batteria), usa la stringa "N/D (nessuna batteria)" o "N/D" a seconda del contesto.
- Se un campo e' applicabile ma il testo e' troppo piccolo/sfocato per essere letto
  con certezza, usa la stringa "DA VERIFICARE (zoom)" - non inventare mai un valore.
- "nome_indirizzo_fabbricante" e "nome_indirizzo_importatore" vanno presi dal testo
  "Prodotto e importato da..." stampato sulla fustella; se fabbricante e importatore
  coincidono, ripeti lo stesso valore in entrambi i campi.
- I simboli booleani (simbolo_ce, simbolo_ukca, simbolo_raee, simbolo_triman,
  qr_code_junker, simbolo_libretto_informativo, strap_on_compatibile,
  funzione_riscaldante, telecomandato) vanno valorizzati con "Si" o "No", mai N/D,
  a meno che l'area dove dovrebbero comparire non sia visibile/leggibile.
- codice_smaltimento_scatola e codice_smaltimento_sacchetto: riporta il codice
  materiale (es. PAP, CPE, PET) come stampato nel triangolo di riciclo.
- barcode: leggilo direttamente dal codice a barre stampato, cifra per cifra.
"""


def costruisci_prompt() -> str:
    campi_fmt = ",\n".join(f'  "{c}": "..."' for c in CAMPI_RICHIESTI)
    return PROMPT_ESTRAZIONE.format(campi="{\n" + campi_fmt + "\n}")


def chiama_vision(client: anthropic.Anthropic, immagini_png: list[bytes]) -> dict:
    content = []
    for img_bytes in immagini_png:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(img_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": costruisci_prompt()})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    testo = "".join(b.text for b in resp.content if b.type == "text")
    testo = testo.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(testo)


def estrai_uno(client: anthropic.Anthropic, path: Path) -> RisultatoEstrazione:
    ris = RisultatoEstrazione(file_pdf=path.name)

    if not e_pdf_valido(path):
        # File corrotto (testo semplice rinominato .pdf): non buttarlo,
        # recupera quel poco che si puo' dal testo grezzo.
        try:
            testo_grezzo = path.read_text(errors="ignore")
        except OSError:
            testo_grezzo = ""
        ris.dati = {c: "N/D (PDF corrotto)" for c in CAMPI_RICHIESTI}
        ris.dati["codice_a_barre"] = barcode_da_nome_file(path) or "N/D"
        ris.dati["nome_prodotto"] = path.stem.split("_", 2)[-1].replace("_", " ")
        # euristica minima: se nel testo grezzo compare "LOT:" lo cattura
        m = re.search(r"LOT:\s*([A-Za-z0-9]+)", testo_grezzo)
        if m:
            ris.dati["lotto"] = m.group(1)
        ris.stato = "Parziale"
        ris.motivo = "PDF corrotto (file di testo semplice, non vettoriale)"
        return ris

    try:
        immagini = render_pagine(path, MATRIX_STANDARD)
        if not immagini:
            ris.motivo = "Nessuna pagina renderizzata"
            return ris

        dati = chiama_vision(client, immagini)

        # Retry mirato se il LOT non e' stato letto al primo giro
        if str(dati.get("lotto", "")).startswith("DA VERIFICARE"):
            LOG.info("LOT illeggibile a risoluzione standard per %s, retry a 8x...", path.name)
            img_hires = render_area_lotto_hires(path)
            dati_hires = chiama_vision(client, [img_hires])
            if not str(dati_hires.get("lotto", "")).startswith("DA VERIFICARE"):
                dati["lotto"] = dati_hires["lotto"]

        mancanti = [c for c in CAMPI_RICHIESTI if c not in dati]
        if mancanti:
            ris.motivo = f"Campi mancanti nella risposta: {mancanti}"
            ris.dati = dati
            ris.stato = "Parziale"
            return ris

        # coerenza barcode nome-file vs barcode letto
        bc_nome = barcode_da_nome_file(path)
        if bc_nome and dati.get("codice_a_barre") != bc_nome:
            LOG.warning(
                "Barcode letto (%s) diverso da quello nel nome file (%s) per %s",
                dati.get("codice_a_barre"), bc_nome, path.name,
            )

        ris.dati = dati
        valori_da_verificare = [v for v in dati.values() if str(v).startswith("DA VERIFICARE")]
        ris.stato = "Parziale" if valori_da_verificare else "Completo"
        if valori_da_verificare:
            ris.motivo = "Uno o piu' campi restano illeggibili anche dopo il retry ad alta risoluzione"
        return ris

    except Exception as exc:  # noqa: BLE001 - vogliamo loggare qualunque errore e proseguire
        ris.motivo = f"Eccezione durante l'estrazione: {exc}"
        return ris


# --------------------------------------------------------------------------- #
# STEP 4: SCRITTURA SU GOOGLE SHEET
# --------------------------------------------------------------------------- #

def apri_sheet(sheet_id: str):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError(
            "gspread non installato. Esegui: pip install gspread google-auth"
        )
    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            f"File credenziali non trovato: {CREDENTIALS_FILE}. "
            "Vedi README.md per come creare un service account Google Cloud."
        )
    creds = Credentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=200, cols=len(CAMPI_RICHIESTI) + 2)
        ws.append_row(CAMPI_RICHIESTI + ["stato_estrazione", "file_origine"])
    return ws


def barcode_gia_presenti(ws) -> set[str]:
    valori = ws.col_values(CAMPI_RICHIESTI.index("codice_a_barre") + 1)
    return set(valori[1:])  # salta header


def scrivi_riga(ws, ris: RisultatoEstrazione):
    riga = [ris.dati.get(c, "N/D") for c in CAMPI_RICHIESTI]
    riga += [f"{ris.stato}" + (f" - {ris.motivo}" if ris.motivo else ""), ris.file_pdf]
    ws.append_row(riga, value_input_option="USER_ENTERED")


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Cartella con i PDF delle fustelle")
    parser.add_argument("--sheet-id", required=True, help="ID del Google Sheet di destinazione")
    parser.add_argument("--dry-run", action="store_true", help="Non scrive su Google Sheet, stampa solo il JSON estratto")
    parser.add_argument("--limit", type=int, default=None, help="Processa al massimo N file (utile per test)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        LOG.error("Variabile d'ambiente ANTHROPIC_API_KEY non impostata.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    ws = None
    barcode_gia_fatti: set[str] = set()
    if not args.dry_run:
        ws = apri_sheet(args.sheet_id)
        barcode_gia_fatti = barcode_gia_presenti(ws)
        LOG.info("%d barcode gia' presenti sul foglio, verranno saltati", len(barcode_gia_fatti))

    da_processare = trova_pdf_da_processare(args.input, barcode_gia_fatti)
    if args.limit:
        da_processare = da_processare[: args.limit]
    LOG.info("%d PDF da processare", len(da_processare))

    contatori = {"Completo": 0, "Parziale": 0, "Errore": 0}
    for i, path in enumerate(da_processare, 1):
        LOG.info("[%d/%d] %s", i, len(da_processare), path.name)
        ris = estrai_uno(client, path)
        contatori[ris.stato] = contatori.get(ris.stato, 0) + 1

        if args.dry_run:
            print(json.dumps({"file": path.name, "stato": ris.stato, "motivo": ris.motivo, "dati": ris.dati}, ensure_ascii=False, indent=2))
        else:
            scrivi_riga(ws, ris)

        time.sleep(0.5)  # piccolo margine di cortesia verso il rate limit API

    LOG.info("FATTO. Completo=%d Parziale=%d Errore=%d", contatori["Completo"], contatori["Parziale"], contatori["Errore"])


if __name__ == "__main__":
    main()
