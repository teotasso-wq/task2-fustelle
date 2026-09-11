# Estrazione fustelle → Google Sheet

Automazione per la Task 2: legge i PDF vettoriali delle fustelle (packaging
MySecretCase), estrae i 33 campi richiesti con un modello vision, e scrive i
risultati su Google Sheet.

**Due modi di usarlo:**
- **Notebook Colab** (`estrazione_fustelle_colab.ipynb`) — nessuna installazione
  locale, si apre nel browser e si esegue cella per cella. Consigliato per una demo
  rapida o per chi valuta il progetto senza voler configurare un ambiente Python.
  [![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/<TUO-USERNAME>/<NOME-REPO>/blob/main/estrazione_fustelle_colab.ipynb)
  *(sostituire `<TUO-USERNAME>/<NOME-REPO>` con il percorso reale dopo aver pushato su GitHub)*
- **Script CLI** (`estrazione_fustelle.py`) — per esecuzione da terminale/cron,
  pensato per un uso ripetuto su batch grandi (vedi sotto).

## Perché serve la vision e non l'estrazione testo classica

I PDF sono export Illustrator con il testo convertito in tracciati vettoriali:
`pdfplumber`, `PyPDF2` e simili tornano ~16 caratteri per pagina. L'unica strada
è renderizzare ogni pagina come immagine e farla leggere a un modello vision.

## Setup

```bash
pip install -r requirements.txt
```

1. **Chiave API Claude**: esporta `ANTHROPIC_API_KEY` nell'ambiente.
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

2. **Google Sheets**:
   - Crea un progetto su Google Cloud Console, abilita "Google Sheets API".
   - Crea un Service Account, scarica la chiave JSON, rinominala
     `credentials.json` e mettila accanto a `estrazione_fustelle.py`.
   - Apri il Google Sheet di destinazione → Condividi → aggiungi l'email del
     service account (es. `xxx@yyy.iam.gserviceaccount.com`) come Editor.
   - Copia l'ID dello sheet dall'URL: `docs.google.com/spreadsheets/d/<ID>/edit`.

## Esecuzione

Test senza scrivere su Google (stampa il JSON estratto a schermo):
```bash
python estrazione_fustelle.py --input /percorso/ai/pdf --sheet-id <ID> --dry-run --limit 3
```

Esecuzione reale su tutta la cartella:
```bash
python estrazione_fustelle.py --input /percorso/ai/pdf --sheet-id <ID>
```

Lo script:
- salta i PDF il cui codice a barre (letto dal nome file) è già presente sul
  foglio, così è **rieseguibile in sicurezza** senza creare duplicati;
- isola automaticamente i PDF "corrotti" (file di testo semplice rinominati
  `.pdf`, capitato per 5 pack su 50 in questo batch) invece di farli fallire;
- se il campo LOT torna illeggibile al primo giro (render 2x / ~300 DPI),
  **ritenta automaticamente a 8x / ~1200 DPI** sulla stessa pagina — questo ha
  risolto la maggior parte dei casi nel test manuale;
- scrive ogni riga con uno stato `Completo` o `Parziale - <motivo>`, mai un
  dato inventato: i campi non leggibili restano `DA VERIFICARE (zoom)`.

## Limiti noti (dal test manuale su 50 pack)

- **Alcuni LOT restano illeggibili anche a 1200 DPI**: su ~8 pack su 50 il
  testo del LOT è disegnato così piccolo nel file sorgente Illustrator che
  nessuna risoluzione di rendering lo recupera. Questo non è un limite dello
  script ma del file sorgente: va segnalato al team grafico o verificato a
  occhio sul pacco fisico.
- **File PDF corrotti**: alcuni file arrivano come testo semplice rinominato
  `.pdf` invece che come vettoriale Illustrator vero. Lo script li rileva e li
  segna come `Parziale - PDF corrotto`, ma il dato completo va richiesto a chi
  genera le fustelle.
- Il costo/tempo scala linearmente col numero di pagine per PDF e con gli
  eventuali retry ad alta risoluzione: su un batch di 50 pack contare
  indicativamente 60-90 chiamate API (1-2 per pack + retry sui LOT difficili).
