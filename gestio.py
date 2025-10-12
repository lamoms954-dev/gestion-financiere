from difflib import get_close_matches
import os
import shutil
import sqlite3
import pandas as pd
import pytesseract 
from PIL import Image
import re
import streamlit as st
from datetime import datetime,date,timedelta
from dateutil import parser
from dateutil.relativedelta import relativedelta
import pdfplumber
from alpha_vantage.timeseries import TimeSeries
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ==============================
# 📄 Configuration Streamlit
# ==============================
st.set_page_config(layout="wide")
st.markdown("""
    <style>
    div[data-testid="stDataFrame"] div[role="gridcell"] {
        font-size: 16px !important;
        padding: 8px !important;
    }
    </style>
""", unsafe_allow_html=True)


# ==============================
# 📂 CONFIGURATION DES DOSSIERS
# ==============================
# Dossiers principaux (relatifs à la racine du dépôt)
from config_folders import TO_SCAN_DIR, SORTED_DIR, REVENUS_A_TRAITER, REVENUS_TRAITE, DATA_DIR, RAW_DIR, SCANNED_DIR
TO_SCAN_DIR = "tickets_a_scanner"       # Dossier source pour tickets
SORTED_DIR = "tickets_scanner"          # Dossier final (classé)

# Dossier data interne
DATA_DIR = "data"
RAW_DIR = os.path.join(DATA_DIR, "raw_tickets")
SCANNED_DIR = os.path.join(DATA_DIR, "scanned_tickets")

# Dossiers revenus
REVENUS_A_TRAITER = "revenus_a_traiter"
REVENUS_TRAITE = "revenus_traite"

# Création automatique de tous les dossiers
for d in [TO_SCAN_DIR, SORTED_DIR, DATA_DIR, RAW_DIR, SCANNED_DIR, REVENUS_A_TRAITER, REVENUS_TRAITE]:
    os.makedirs(d, exist_ok=True)



# ==============================
# 💾 BASE DE DONNÉES SQLITE
# ==============================
DB_PATH = os.path.join(DATA_DIR, "finances.db")

def init_db():
    """Initialise ou met à jour la base de données SQLite avec la table 'transactions'."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,                 -- 'revenu' ou 'dépense'
            categorie TEXT,
            sous_categorie TEXT,
            description TEXT,
            montant REAL,
            date TEXT,                 -- ISO YYYY-MM-DD
            source TEXT,               -- 'OCR', 'manuel', 'récurrente', etc.
            recurrence TEXT,           -- 'mensuelle', 'hebdomadaire', etc.
            date_fin TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ==============================
# 📁 Dictionnaire des mois
# ==============================
mois_dict = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12"
}

def numero_to_mois(num: str) -> str:
    for mois, numero in mois_dict.items():
        if numero == num:
            return mois
    return "inconnu"

# ==============================
# 🧠 OCR ET TRAITEMENT DE TICKET ET REVENU
# ==============================
def full_ocr(image_path: str) -> str:
    return pytesseract.image_to_string(Image.open(image_path), lang="fra")


def get_montant_from_line(label_pattern, all_lines, allow_next_line=True):
    """
    Recherche un montant à partir d'un label (ex: 'MONTANT' ou 'MONTANT RÉEL').
    Gère les cas où le montant est sur la ligne suivante.
    """
    montant_regex = r"(\d+[.,]\d{2})\s*(?:€|eur|euros?)?"

    for i, l in enumerate(all_lines):
        l_clean = re.sub(r"[\u200b\s]+", " ", l).strip()
        if re.search(label_pattern, l_clean, re.IGNORECASE):
            # Montant sur la même ligne
            found_same = re.findall(montant_regex, l_clean, re.IGNORECASE)
            if found_same:
                return float(found_same[0].replace(",", "."))
            # Montant sur la ligne suivante
            if allow_next_line and i + 1 < len(all_lines):
                next_line = re.sub(r"[\u200b\s]+", " ", all_lines[i + 1]).strip()
                found_next = re.search(r"(\d+[.,]\d{2})\s*(€|eur|euros?)", next_line, re.IGNORECASE)
                if found_next:
                    return float(found_next.group(1).replace(",", "."))
                # fallback montant seul
                found_next_simple = re.findall(montant_regex, next_line, re.IGNORECASE)
                if found_next_simple:
                    return float(found_next_simple[0].replace(",", "."))
    return 0.0


def parse_ticket_metadata(ocr_text: str):
    """
    Analyse un texte OCR de ticket pour extraire :
    - montants pertinents (total, paiement, etc.)
    - date
    - lignes clés
    """
    lines = [l.strip() for l in ocr_text.split("\n") if l.strip()]

    # Patterns pour totaux et paiement
    total_patterns = [r"TOTAL\s*TTC", r"TOTAL\s*NET", r"TOTAL\s*TVA", r"TTC", r"MONTANT(\s*R[EÉ]EL)?", r"MONTANT"]
    payment_patterns = [r"CB", r"CARTE", r"ESPECES", r"CHEQUE", r"VISA", r"MASTERCARD", r"PAYPAL", r"AMEX", r"PAIEMENT", r"WEB", r"PAIEMENT PAR"]

    # Lignes clés
    key_lines = [l for l in lines if any(re.search(p, l, re.IGNORECASE) for p in (total_patterns + payment_patterns))]

    # Montants prioritaires
    total_net = get_montant_from_line(r"TOTAL\s*NET", lines)
    total_tva = get_montant_from_line(r"TOTAL\s*TVA", lines)
    montant_reel = get_montant_from_line(r"MONTANT(\s*R[EÉ]EL)?", lines)
    total_ttc_somme = total_net + total_tva if total_net + total_tva > 0 else montant_reel
    paiement_sum = sum(get_montant_from_line(p, lines) for p in payment_patterns)

    # Construction de la liste de montants possibles
    montants_possibles = []
    priorites = []
    if abs(total_ttc_somme - paiement_sum) < 0.05 and total_ttc_somme > 0:
        priorites.append(total_ttc_somme)
    if total_ttc_somme > 0:
        priorites.append(total_ttc_somme)
    if paiement_sum > 0:
        priorites.append(paiement_sum)

    for p in reversed(priorites):
        if p not in montants_possibles:
            montants_possibles.insert(0, p)
        else:
            montants_possibles.remove(p)
            montants_possibles.insert(0, p)

    if not montants_possibles:
        montants_possibles = [0.0]

    # Détection de la date
    detected_date = None
    date_patterns = [
        r"\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b",
        r"\b\d{1,2}\s*(janv|févr|mars|avr|mai|juin|juil|août|sept|oct|nov|déc)\.?\s*\d{2,4}\b"
    ]
    for p in date_patterns:
        match = re.search(p, ocr_text, re.IGNORECASE)
        if match:
            date_str = match.group(0)
            try:
                dt = parser.parse(date_str, dayfirst=True, fuzzy=True)
                detected_date = dt.date().isoformat()
                break
            except:
                continue
    if not detected_date:
        detected_date = datetime.now().date().isoformat()

    return {
        "montants_possibles": montants_possibles,
        "montant": montants_possibles[0],
        "date": detected_date,
        "infos": "\n".join(key_lines)
    }


def move_ticket_to_sorted(ticket_path, categorie, sous_categorie):
    """Déplace un ticket traité vers le dossier 'tickets_scannes' classé par catégorie/sous-catégorie."""
    cat_dir = os.path.join(SORTED_DIR, categorie)
    souscat_dir = os.path.join(cat_dir, sous_categorie)
    os.makedirs(souscat_dir, exist_ok=True)
    shutil.move(ticket_path, os.path.join(souscat_dir, os.path.basename(ticket_path)))


def insert_transaction_batch(transactions):
    """
    Insère plusieurs transactions dans la base SQLite.
    Évite les doublons basés sur (type, catégorie, sous_catégorie, montant, date).
    """
    if not transactions:
        return

    db_path = os.path.join(DATA_DIR, "finances.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    inserted, skipped = 0, 0

    for t in transactions:
        try:
            cur.execute("""
                SELECT COUNT(*) FROM transactions
                WHERE type = ? AND categorie = ? AND sous_categorie = ?
                      AND montant = ? AND date = ?
            """, (
                t["type"],
                t.get("categorie", ""),
                t.get("sous_categorie", ""),
                float(t["montant"]),
                t["date"]
            ))

            if cur.fetchone()[0] > 0:
                skipped += 1
                continue

            cur.execute("""
                INSERT INTO transactions
                (type, categorie, sous_categorie, description, montant, date, source, recurrence, date_fin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                t["type"],
                t.get("categorie", ""),
                t.get("sous_categorie", ""),
                t.get("description", ""),
                float(t["montant"]),
                t["date"],
                t.get("source", "manuel"),
                t.get("recurrence", "ponctuelle"),
                t.get("date_fin")
            ))
            inserted += 1

        except Exception as e:
            print(f"Erreur lors de l’insertion de {t}: {e}")

    conn.commit()
    conn.close()
    st.success(f"✅ {inserted} transaction(s) insérée(s).")
    if skipped > 0:
        st.info(f"ℹ️ {skipped} doublon(s) détecté(s) et ignoré(s).")


def extract_text_from_pdf(pdf_path):
    """Lit un PDF et renvoie le texte brut."""
    from pdfminer.high_level import extract_text
    try:
        return extract_text(pdf_path)
    except Exception as e:
        st.warning(f"⚠️ Impossible de lire le PDF {pdf_path} ({e})")
        return ""
    
    
def parse_uber_pdf(pdf_path: str) -> dict:
    """
    Parseur spécifique pour les PDF Uber.
    Objectif : extraire le montant net (net earnings) et la date de fin de période de facturation.
    Renvoie dict avec clés : montant (float), date (datetime.date), categorie, sous_categorie, source.
    """
    text = extract_text_from_pdf(pdf_path)
    if not text:
        return {
            "montant": 0.0,
            "date": datetime.now().date(),
            "categorie": "Revenu",
            "sous_categorie": "Uber",
            "source": "PDF Uber"
        }

    # Cherche une période de facturation sous forme "Période de facturation : 01/07/2025 - 31/07/2025"
    date_fin = None
    periode_match = re.search(
        r"P[eé]riode de facturation\s*[:\-]?\s*([0-3]?\d[\/\-\.][01]?\d[\/\-\.]\d{2,4})\s*[\-–]\s*([0-3]?\d[\/\-\.][01]?\d[\/\-\.]\d{2,4})",
        text,
        re.IGNORECASE
    )
    if periode_match:
        debut_str, fin_str = periode_match.groups()
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
            try:
                date_fin = datetime.strptime(fin_str, fmt).date()
                break
            except Exception:
                continue

    # Si non trouvé par pattern, on tente de trouver une date "Période terminée le : 31/07/2025" ou "Period ending 31/07/2025"
    if not date_fin:
        m2 = re.search(
            r"(period ending|p[eé]riode termin[eé]e le|Date de fin)\s*[:\-]?\s*([0-3]?\d[\/\-\.][01]?\d[\/\-\.]\d{2,4})",
            text,
            re.IGNORECASE
        )
        if m2:
            date_str = m2.group(2)
            for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
                try:
                    date_fin = datetime.strptime(date_str, fmt).date()
                    break
                except Exception:
                    continue

    if not date_fin:
        date_fin = datetime.now().date()

    # Montant net : varie selon le PDF Uber (Net earnings, Total to be paid, etc.)
    # On cherche d'abord des expressions anglaises ou françaises communes
    montant = 0.0
    montant_patterns = [
        r"(?:Net earnings|Net to driver|Total net|Montant net|Net earnings \(driver\))\s*[:\-\–]?\s*([0-9]+[.,][0-9]{2})\s*€?",
        r"([\d]{1,3}(?:[ .,]\d{3})*[.,]\d{2})\s*€\s*(?:net|netto|net earnings|to driver)?"
    ]
    for p in montant_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            s = m.group(1).replace(" ", "").replace(".", "").replace(",", ".") if "," in m.group(1) and "." in m.group(1) else m.group(1).replace(",", ".").replace(" ", "")
            try:
                montant = float(s)
                break
            except Exception:
                continue

    # fallback: chercher le dernier montant présent dans le texte (souvent utile si formats variés)
    if montant == 0.0:
        all_amounts = re.findall(r"(\d+[.,]\d{2})\s*€?", text)
        if all_amounts:
            # souvent le montant net est parmi les derniers montants, on prend le dernier non nul
            for a in reversed(all_amounts):
                try:
                    candidate = float(a.replace(",", "."))
                    if candidate > 0:
                        montant = candidate
                        break
                except:
                    continue

    return {
        "montant": round(montant, 2),
        "date": date_fin,
        "categorie": "Revenu",
        "sous_categorie": "Uber Eats",
        "source": "PDF Uber"
    }

    
def parse_fiche_paie(pdf_path: str) -> dict:
    """
    Parseur spécifique pour fiche de paie.
    Objectif : trouver la période (ou la date concernée) et le net à payer.
    Renvoie dict similaire à parse_uber_pdf.
    """
    text = extract_text_from_pdf(pdf_path)
    if not text:
        return {"montant": 0.0, "date": datetime.now().date(), "categorie": "Revenu", "sous_categorie": "Salaire", "source": "PDF Fiche de paie"}

    # 1) Trouver le net à payer (patterns : NET A PAYER, Net à payer, Net pay, Net salary)
    montant = 0.0
    net_patterns = [
        r"NET\s*A\s*PAYER\s*[:\-\–]?\s*([0-9]+[.,][0-9]{2})",
        r"Net à payer\s*[:\-\–]?\s*([0-9]+[.,][0-9]{2})",
        r"Net à payer \(à vous\)\s*[:\-\–]?\s*([0-9]+[.,][0-9]{2})",
        r"Net\s*[:\-\–]?\s*([0-9]+[.,][0-9]{2})"  # fallback
    ]
    for p in net_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                montant = float(m.group(1).replace(",", "."))
                break
            except:
                continue

    # fallback : prendre le dernier montant trouvé, mais prudence
    if montant == 0.0:
        amounts = re.findall(r"(\d+[.,]\d{2})\s*€?", text)
        if amounts:
            # on peut prioriser montants > 100 (supposés être net), sinon prendre le dernier
            candidates = [float(a.replace(",", ".")) for a in amounts]
            bigs = [c for c in candidates if c > 100]  # heuristique : salaire > 100€
            montant = bigs[-1] if bigs else candidates[-1]

    # 2) Trouver la période ou la date : recherche de "période" ou intervalle "01/07/2025 - 31/07/2025"
    date_found = None
    periode_match = re.search(r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})\s*[\-–]\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})", text)
    if periode_match:
        # on prend la date de fin comme date du revenu
        fin_str = periode_match.groups()[1]
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
            try:
                date_found = datetime.strptime(fin_str, fmt).date()
                break
            except:
                pass

    # autre pattern : "Période du : 01/07/2025 au 31/07/2025" ou "Pour le mois de juillet 2025"
    if not date_found:
        m2 = re.search(r"Pour le mois de\s+([A-Za-zéûà]+)\s+(\d{4})", text, re.IGNORECASE)
        if m2:
            mois_str, annee_str = m2.groups()
            # mapping simple des mois FR (on peut étendre si besoin)
            mois_map = {
                "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,
                "juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12
            }
            mois_key = mois_str.lower()
            mois_num = mois_map.get(mois_key)
            if mois_num:
                # on choisit la fin du mois comme date
                from calendar import monthrange
                last_day = monthrange(int(annee_str), mois_num)[1]
                date_found = date(int(annee_str), mois_num, last_day)

    if not date_found:
        # fallback : date d'aujourd'hui
        date_found = datetime.now().date()

    return {
        "montant": round(float(montant), 2),
        "date": date_found,
        "categorie": "Revenu",
        "sous_categorie": "Salaire",
        "source": "PDF Fiche de paie"
    }


def parse_pdf_dispatcher(pdf_path: str, source_type: str) -> dict:
    """
    Dispatcher simple pour choisir le parseur adapté.
    source_type attendu : 'uber', 'fiche_paie', 'ticket' (ou 'auto' pour tentative heuristique).
    """
    stype = source_type.lower().strip()
    if stype in ("uber", "uber_pdf", "uber eats"):
        return parse_uber_pdf(pdf_path)
    elif stype in ("fiche_paie", "fiche de paie", "paye", "salaire"):
        return parse_fiche_paie(pdf_path)
    elif stype in ("ticket", "receipt", "ticket_ocr"):
        # si tu veux parser un PDF ticket (rare), tu peux appeler parse_ticket_metadata en lui passant le texte
        text = extract_text_from_pdf(pdf_path)
        # parse_ticket_metadata attend du texte OCR ; si elle attend un path, adapte
        return parse_ticket_metadata(text)
    elif stype == "auto":
        # heuristique : essaye d'identifier le type en recherchant des mots-clés dans le PDF
        text = extract_text_from_pdf(pdf_path).lower()
        if "uber" in text or "net to driver" in text or "period" in text:
            return parse_uber_pdf(pdf_path)
        if "net a payer" in text or "fiche de paie" in text or "bulletin" in text:
            return parse_fiche_paie(pdf_path)
        # fallback : on renvoie quelque chose générique
        return {"montant": 0.0, "date": datetime.now().date(), "categorie": "Revenu", "sous_categorie": "Inconnu", "source": "PDF Auto"}
    else:
        raise ValueError(f"Source_type inconnu pour parse_pdf_dispatcher: {source_type}")


def ajouter_transaction(categorie, sous_categorie, montant, date_transaction, type_transac="dépense", source="manuel", recurrence=None, date_fin=None):
    """Ajoute une transaction dans la base de données."""
    if not categorie or montant <= 0:
        raise ValueError("Catégorie ou montant invalide.")

    conn = sqlite3.connect("data/finances.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO transactions (type, categorie, sous_categorie, montant, date, source, recurrence, date_fin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        type_transac,
        categorie.strip(),
        sous_categorie.strip() if sous_categorie else "",
        montant,
        date_transaction.isoformat(),
        source,
        recurrence,
        date_fin.isoformat() if date_fin else None
    ))
    conn.commit()
    conn.close()


# ==============================
# ⚙️ TRAITEMENT DES TICKETS ET REVENUS
# ==============================
def process_all_tickets_in_folder():
    tickets = [f for f in os.listdir(TO_SCAN_DIR) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not tickets:
        st.warning("Aucun ticket trouvé dans le dossier à scanner.")
        return

    st.subheader("📥 Tickets trouvés :")
    for t in tickets:
        st.write("🧾", t)

    if "tickets_data" not in st.session_state:
        st.session_state["tickets_data"] = []

    if st.button("🚀 Scanner tous les tickets") and not st.session_state["tickets_data"]:
        data_list = []
        for t in tickets:
            ticket_path = os.path.join(TO_SCAN_DIR, t)
            ocr_text = full_ocr(ticket_path)
            parsed = parse_ticket_metadata(ocr_text)

            # Catégorie / sous-catégorie depuis le nom du fichier si OCR ne trouve rien
            name = os.path.splitext(t)[0]
            parts = name.split(".")
            if len(parts) >= 2:
                sous_categorie = parts[-2].capitalize()
                categorie = parts[-1].capitalize()
            else:
                sous_categorie = "Autre"
                categorie = "Divers"

            try:
                date_val = datetime.fromisoformat(parsed["date"]).date()
            except Exception:
                date_val = datetime.today().date()

            data_list.append({
                "file": t,
                "path": ticket_path,
                "categorie": parsed.get("categorie", categorie),
                "sous_categorie": parsed.get("sous_categorie", sous_categorie),
                "montant": parsed.get("montant", 0.0),
                "date": date_val,
            })

        st.session_state["tickets_data"] = data_list
        st.success("✅ Tickets scannés avec succès. Tu peux maintenant les modifier avant validation.")

    # --- Édition persistante ---
    if st.session_state["tickets_data"]:
        updated_list = []

        for idx, data in enumerate(st.session_state["tickets_data"]):
            st.markdown("---")
            st.image(data["path"], caption=f"🧾 {data['file']}", use_container_width=True)

            col1, col2 = st.columns(2)
            with col1:
                cat = st.text_input(f"Catégorie ({data['file']})", value=data["categorie"], key=f"cat_{idx}")
                souscat = st.text_input(f"Sous-catégorie ({data['file']})", value=data["sous_categorie"], key=f"souscat_{idx}")
            with col2:
                montant_str = f"{data['montant']:.2f}" if data["montant"] else ""
                montant_edit = st.text_input(f"Montant (€) ({data['file']})", value=montant_str, key=f"montant_{idx}")
                date_edit = st.date_input(f"Date ({data['file']})", value=data["date"], key=f"date_{idx}")

            try:
                montant_val = float(montant_edit.replace(",", "."))
            except ValueError:
                montant_val = 0.0

            updated_list.append({
                "file": data["file"],
                "path": data["path"],
                "categorie": cat.strip(),
                "sous_categorie": souscat.strip(),
                "montant": montant_val,
                "date": date_edit
            })

        st.session_state["tickets_data"] = updated_list
        st.markdown("---")
        st.warning("⚠️ Vérifie bien les informations avant de confirmer l’enregistrement.")

        if st.button("✅ Confirmer et enregistrer tous les tickets"):
            for data in st.session_state["tickets_data"]:
                # Utilise la fonction centrale pour ajouter la transaction
                ajouter_transaction(
                    categorie=data["categorie"],
                    sous_categorie=data["sous_categorie"],
                    montant=data["montant"],
                    date_transaction=data["date"],
                    source="OCR"
                )
                move_ticket_to_sorted(data["path"], data["categorie"], data["sous_categorie"])

            st.success("🎉 Tous les tickets ont été enregistrés et rangés avec succès !")
            st.session_state.pop("tickets_data")


def interface_process_all_revenues_in_folder():
    st.subheader("📥 Scanner et enregistrer tous les revenus depuis le dossier")

    src_folder = os.path.join("revenus_a_traiter")
    dest_folder = os.path.join("revenus_traités")


    # --- Étape 1 : scanner les fichiers une seule fois ---
    if "revenus_data" not in st.session_state:
        st.session_state["revenus_data"] = []

    if st.button("🚀 Scanner tous les revenus") and not st.session_state["revenus_data"]:
        pdfs = [os.path.join(root, f)
                for root, _, files in os.walk(src_folder)
                for f in files if f.lower().endswith(".pdf")]

        if not pdfs:
            st.warning("📂 Aucun PDF de revenu trouvé dans le dossier.")
            return

        data_list = []
        for pdf_path in pdfs:
            sous_dossier = os.path.basename(os.path.dirname(pdf_path))

            # Parsing selon type
            try:
                if sous_dossier.lower() == "uber":
                    parsed = parse_uber_pdf(pdf_path)
                else:
                    parsed = parse_fiche_paie(pdf_path)
            except Exception:
                parsed = {"montant": 0.0, "date": datetime.today().date(), "source": "PDF Auto"}

            # Calcul du mois en français
            date_val = parsed.get("date", datetime.today().date())
            if isinstance(date_val, str):
                date_val = datetime.fromisoformat(date_val).date()
            mois_nom = numero_to_mois(f"{date_val.month:02d}")

            data_list.append({
                "file": os.path.basename(pdf_path),
                "path": pdf_path,
                "categorie": sous_dossier,
                "sous_categorie": mois_nom,
                "montant": parsed.get("montant", 0.0),
                "date": date_val,
                "source": parsed.get("source", "PDF Auto")
            })

        st.session_state["revenus_data"] = data_list
        st.success("✅ Revenus scannés avec succès. Tu peux maintenant les modifier avant validation.")

    # --- Étape 2 : affichage et édition persistante ---
    if st.session_state.get("revenus_data"):
        updated_list = []
        for idx, data in enumerate(st.session_state["revenus_data"]):
            st.markdown("---")
            st.write(f"📄 {data['file']}")
            col1, col2 = st.columns(2)
            with col1:
                cat = st.text_input(f"Catégorie ({data['file']})", value=data["categorie"], key=f"rev_cat_{idx}")
                souscat = st.text_input(f"Sous-catégorie ({data['file']})", value=data["sous_categorie"], key=f"rev_souscat_{idx}")
            with col2:
                montant_str = f"{data['montant']:.2f}" if data["montant"] else ""
                montant_edit = st.text_input(f"Montant (€) ({data['file']})", value=montant_str, key=f"rev_montant_{idx}")
                date_edit = st.date_input(f"Date ({data['file']})", value=data["date"], key=f"rev_date_{idx}")

            try:
                montant_val = float(montant_edit.replace(",", "."))
            except ValueError:
                montant_val = 0.0

            updated_list.append({
                "file": data["file"],
                "path": data["path"],
                "categorie": cat.strip(),
                "sous_categorie": souscat.strip(),
                "montant": montant_val,
                "date": date_edit,
                "source": data["source"]
            })

        st.session_state["revenus_data"] = updated_list

        st.markdown("---")
        st.warning("⚠️ Vérifie bien les informations avant de confirmer l’enregistrement.")

        # --- Étape 3 : validation et insertion ---
        if st.button("✅ Confirmer et enregistrer tous les revenus"):
            conn = sqlite3.connect("data/finances.db")
            cursor = conn.cursor()

            for data in st.session_state["revenus_data"]:
                cursor.execute("""
                    INSERT INTO transactions (type, categorie, sous_categorie, montant, date, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    "revenu",
                    data["categorie"],
                    data["sous_categorie"],
                    data["montant"],
                    data["date"].isoformat(),
                    data["source"]
                ))

                # Déplacement du fichier
                target_dir = os.path.join(dest_folder, data["categorie"], data["sous_categorie"])
                os.makedirs(target_dir, exist_ok=True)
                shutil.move(data["path"], os.path.join(target_dir, data["file"]))

            conn.commit()
            conn.close()
            st.success("🎉 Tous les revenus ont été enregistrés et rangés avec succès !")
            st.session_state.pop("revenus_data")


# =============================
#   TRANSACTION MANUELLE
# ✍️ AJOUTER UNE TRANSACTION MANUELLE
# =============================
def interface_transaction_manuelle():
    st.subheader("✍️ Ajouter une dépense manuelle")

    mode = st.radio(
        "Choisir le mode d’ajout :",
        ["➕ Ajouter une dépense unique", "📥 Importer plusieurs transactions (CSV)"]
    )

    if mode == "➕ Ajouter une dépense unique":
        with st.form("ajouter_transaction_manuelle", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                categorie = st.text_input("Catégorie principale (ex: essence, courses, santé)")
                sous_categorie = st.text_input("Sous-catégorie (ex: Auchan, médecin, pharmacie)")
                description = st.text_area("Description (facultatif)")
            with col2:
                montant = st.number_input("Montant (€)", min_value=0.0, format="%.2f", step=0.01)
                date_transaction = st.date_input("Date de la transaction", date.today())
            submit_btn = st.form_submit_button("💾 Enregistrer la transaction")

        if submit_btn:
            if not categorie or montant <= 0:
                st.error("⚠️ Veuillez entrer au moins une catégorie et un montant valide.")
                return

            transaction = {
                "type": "dépense",
                "categorie": categorie,
                "sous_categorie": sous_categorie,
                "description": description,
                "montant": montant,
                "date": date_transaction.isoformat(),
                "source": "manuel"
            }
            insert_transaction_batch([transaction])
            st.success(f"✅ Transaction enregistrée : {categorie} → {sous_categorie or '(aucune sous-catégorie)'} — {montant:.2f} €")

    elif mode == "📥 Importer plusieurs transactions (CSV)":
        st.info("💡 Le fichier CSV doit contenir : `Date`, `Categorie`, `Sous_categorie`, `Montant`, `Type` (revenu ou dépense).")
        uploaded_csv = st.file_uploader("Importer un fichier CSV de transactions", type=["csv"])
        if uploaded_csv:
            save_path = os.path.join("data", uploaded_csv.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_csv.getbuffer())

            try:
                df = pd.read_csv(save_path, sep=",", encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(save_path, sep=",", encoding="ISO-8859-1")
            except Exception:
                df = pd.read_csv(save_path, sep=",", encoding="ISO-8859-1")

            st.dataframe(df.head(), use_container_width=True)
            st.success("✅ Fichier CSV lu avec succès. Vérifie ci-dessus les 5 premières lignes.")

            if st.button("📤 Importer dans la base de données"):
                required_cols = {"Date", "Categorie", "Sous_categorie", "Montant", "Type"}
                if not required_cols.issubset(df.columns):
                    st.error("⚠️ Le fichier CSV doit contenir : Date, Categorie, Sous_categorie, Montant, Type.")
                    return

                transactions = []
                for _, row in df.iterrows():
                    try:
                        date_str = str(row["Date"])
                        if "-" in date_str:
                            date_iso = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
                        else:
                            try:
                                date_iso = datetime.strptime(date_str, "%d/%m/%Y").date().isoformat()
                            except ValueError:
                                date_iso = datetime.strptime(date_str, "%d/%m/%y").date().isoformat()

                        transactions.append({
                            "type": str(row["Type"]).strip().lower(),
                            "categorie": str(row["Categorie"]).strip(),
                            "sous_categorie": str(row["Sous_categorie"]).strip(),
                            "montant": float(str(row["Montant"]).replace(",", ".")),
                            "date": date_iso,
                            "source": "import_csv"
                        })
                    except Exception as e:
                        st.warning(f"⚠️ Ligne ignorée à cause d'une erreur : {e}")

                insert_transaction_batch(transactions)
                st.success(f"🎉 Import terminé : {len(transactions)} transaction(s) traitée(s).")
        else:
            st.info("📂 Importez un fichier CSV avant de lancer l’import.")


# =============================
# 🔁 AJOUTER UNE TRANSACTION RÉCURRENTE
# =============================
def interface_transaction_recurrente():
    st.subheader("🔁 Ajouter une dépense récurrente")

    with st.form("ajouter_transaction_recurrente", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            categorie = st.text_input("Catégorie principale (ex: logement, assurance, abonnement)")
            sous_categorie = st.text_input("Sous-catégorie (ex: EDF, Netflix, Loyer)")
            montant = st.number_input("Montant (€)", min_value=0.0, format="%.2f", step=0.01)
        with col2:
            recurrence = st.selectbox("Fréquence", ["hebdomadaire", "mensuelle", "annuelle"])
            date_debut = st.date_input("Date de début", date.today())
            date_fin = st.date_input("Date de fin (facultatif)", None)
        submit_btn = st.form_submit_button("💾 Enregistrer la récurrence")

    if submit_btn:
        if not categorie or montant <= 0:
            st.error("⚠️ Veuillez entrer une catégorie et un montant valide.")
            return

        safe_categorie = re.sub(r'[<>:"/\\|?*]', "_", categorie.strip())
        safe_sous_categorie = re.sub(r'[<>:"/\\|?*]', "_", sous_categorie.strip()) if sous_categorie else ""

        # Enregistrement modèle + occurrences
        today = date.today()
        occurrences = []
        current_date = date_debut
        while current_date <= today:
            occurrences.append(current_date)
            if recurrence == "hebdomadaire":
                current_date += timedelta(weeks=1)
            elif recurrence == "mensuelle":
                current_date += relativedelta(months=1)
            elif recurrence == "annuelle":
                current_date += relativedelta(years=1)
            if date_fin and current_date > date_fin:
                break

        transactions = [
            # modèle
            {
                "type": "dépense",
                "categorie": safe_categorie,
                "sous_categorie": safe_sous_categorie,
                "montant": montant,
                "date": date_debut.isoformat(),
                "source": "récurrente",
                "recurrence": recurrence,
                "date_fin": date_fin.isoformat() if date_fin else None
            }
        ] + [
            # occurrences passées
            {
                "type": "dépense",
                "categorie": safe_categorie,
                "sous_categorie": safe_sous_categorie,
                "montant": montant,
                "date": d.isoformat(),
                "source": "récurrente_auto",
                "recurrence": recurrence
            } for d in occurrences
        ]

        insert_transaction_batch(transactions)
        st.success(f"✅ Transaction récurrente ({recurrence}) enregistrée.")
        st.info(f"{len(occurrences)} occurrence(s) passée(s) ajoutée(s).")


# ==============================
# 💼 INTERFACE AJOUTER UN REVENU
# ==============================
def interface_ajouter_revenu():
    st.subheader("💼 Ajouter un revenu")

    mode = st.selectbox(
        "Choisir le mode d’ajout du revenu :",
        ["Sélectionner...", "Scanner depuis le dossier", "Ajouter manuellement", "Revenu récurrent"]
    )

    # =============================
    # 1️⃣ Scanner depuis le dossier
    # =============================
    if mode == "Scanner depuis le dossier":
        interface_process_all_revenues_in_folder()

    # =============================
    # 2️⃣ Ajouter un revenu manuel
    # =============================
    elif mode == "Ajouter manuellement":
        with st.form("ajouter_revenu_manuel", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                categorie = st.text_input("Catégorie principale (ex: Uber, Animation, Salaire)")
                sous_categorie = st.text_input("Sous-catégorie (ex: septembre, octobre, etc.)")
            with col2:
                montant = st.number_input("Montant (€)", min_value=0.0, format="%.2f", step=0.01)
                date_revenu = st.date_input("Date du revenu", date.today())

            submit_btn = st.form_submit_button("💾 Enregistrer le revenu")

        if submit_btn:
            if not categorie or montant <= 0:
                st.error("⚠️ Veuillez entrer une catégorie et un montant valide.")
                return

            insert_transaction_batch([{
                "type": "revenu",
                "categorie": categorie.strip(),
                "sous_categorie": sous_categorie.strip(),
                "montant": montant,
                "date": date_revenu.isoformat(),
                "source": "manuel"
            }])
            st.success("✅ Revenu manuel ajouté avec succès !")

    # =============================
    # 3️⃣ Revenu récurrent
    # =============================
    elif mode == "Revenu récurrent":
        with st.form("ajouter_revenu_recurrent", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                categorie = st.text_input("Catégorie principale (ex: Salaire, Bourse, CAF)")
                sous_categorie = st.text_input("Sous-catégorie (ex: septembre, octobre, etc.)")
                montant = st.number_input("Montant du revenu (€)", min_value=0.0, format="%.2f", step=0.01)
            with col2:
                recurrence = st.selectbox("Fréquence", ["mensuelle", "hebdomadaire", "annuelle"])
                date_debut = st.date_input("Date de début", date.today())
                date_fin = st.date_input("Date de fin (facultatif)", None)

            submit_btn = st.form_submit_button("💾 Enregistrer la récurrence")

        if submit_btn:
            if not categorie or montant <= 0:
                st.error("⚠️ Veuillez entrer une catégorie et un montant valide.")
                return

            safe_categorie = re.sub(r'[<>:"/\\|?*]', "_", categorie.strip())
            safe_sous_categorie = re.sub(r'[<>:"/\\|?*]', "_", sous_categorie.strip()) if sous_categorie else ""

            today = date.today()
            occurrences = []
            current_date = date_debut
            while current_date <= today:
                occurrences.append(current_date)
                if recurrence == "hebdomadaire":
                    current_date += timedelta(weeks=1)
                elif recurrence == "mensuelle":
                    current_date += relativedelta(months=1)
                elif recurrence == "annuelle":
                    current_date += relativedelta(years=1)
                if date_fin and current_date > date_fin:
                    break

            transactions = [
                {"type": "revenu", "categorie": safe_categorie, "sous_categorie": safe_sous_categorie,
                 "montant": montant, "date": date_debut.isoformat(), "source": "récurrente", "recurrence": recurrence,
                 "date_fin": date_fin.isoformat() if date_fin else None}
            ] + [
                {"type": "revenu", "categorie": safe_categorie, "sous_categorie": safe_sous_categorie,
                 "montant": montant, "date": d.isoformat(), "source": "récurrente_auto", "recurrence": recurrence}
                for d in occurrences
            ]
            insert_transaction_batch(transactions)
            st.success(f"✅ Revenu récurrent ({recurrence}) ajouté avec succès.")
            st.info(f"{len(occurrences)} versement(s) passé(s) ajouté(s).")
#==============================
# 🔁 GERER LES RECURRENCES
# =============================
def interface_gerer_recurrences():
    st.subheader("🔁 Gérer les transactions récurrentes")
    conn = sqlite3.connect("data/finances.db")
    df = pd.read_sql_query("SELECT * FROM transactions WHERE source='récurrente_auto' ORDER BY date DESC", conn)
    conn.close()

    if df.empty:
        st.info("Aucune transaction récurrente trouvée.")
        return

    st.dataframe(df, use_container_width=True)
    selected_id = st.selectbox("Sélectionner une récurrence à modifier :", df["id"].tolist())

    if selected_id:
        selected = df[df["id"] == selected_id].iloc[0]
        st.markdown(f"### 🧾 {selected['categorie']} → {selected['sous_categorie']}")
        new_montant = st.number_input("Montant", value=float(selected["montant"]), step=0.01)
        new_recurrence = st.selectbox("Récurrence", ["hebdomadaire", "mensuelle", "annuelle"], index=["hebdomadaire","mensuelle","annuelle"].index(selected["recurrence"]))
        new_date_fin = st.date_input("Date de fin", value=date.today() if not selected["date_fin"] else datetime.fromisoformat(selected["date_fin"]).date())
        col1, col2 = st.columns(2)

        with col1:
            if st.button("💾 Enregistrer les modifications"):
                conn = sqlite3.connect("data/finances.db")
                cursor = conn.cursor()
                cursor.execute("UPDATE transactions SET montant=?, recurrence=?, date_fin=? WHERE id=?", (new_montant, new_recurrence, new_date_fin.isoformat(), selected_id))
                conn.commit()
                conn.close()
                st.success("✅ Récurrence mise à jour avec succès.")

        with col2:
            if st.button("🗑️ Supprimer cette récurrence et toutes ses occurrences"):
                conn = sqlite3.connect("data/finances.db")
                cursor = conn.cursor()
                cursor.execute("DELETE FROM transactions WHERE (source LIKE 'récurrente%' AND categorie=? AND sous_categorie=?)", (selected["categorie"], selected["sous_categorie"]))
                conn.commit()
                conn.close()
                st.success("🗑️ Récurrence supprimée entièrement.")


# =============================
# 🛠️ GERER LES TRANSACTIONS
# =============================
def interface_gerer_transactions():
    st.subheader("🛠️ Gérer les transactions (modifier ou supprimer)")

    conn = sqlite3.connect("data/finances.db")
    df = pd.read_sql_query("SELECT * FROM transactions ORDER BY date DESC", conn)
    conn.close()

    if df.empty:
        st.info("Aucune transaction à gérer pour le moment.")
        return

    type_filter = st.selectbox("Type", ["Toutes", "revenu", "dépense"], key="type_filtre_gerer")
    cat_filter = st.selectbox("Catégorie", ["Toutes"] + sorted(df["categorie"].dropna().unique().tolist()), key="cat_filtre_gerer")
    souscat_filter = st.selectbox("Sous-catégorie", ["Toutes"] + sorted(df["sous_categorie"].dropna().unique().tolist()), key="souscat_filtre_gerer")

    if type_filter != "Toutes": df = df[df["type"] == type_filter]
    if cat_filter != "Toutes": df = df[df["categorie"] == cat_filter]
    if souscat_filter != "Toutes": df = df[df["sous_categorie"] == souscat_filter]

    if df.empty:
        st.warning("Aucune transaction trouvée avec ces filtres.")
        return

    df["🗑️ Supprimer"] = False
    st.info("💡 Modifie les valeurs directement ou coche les lignes à supprimer.")
    df_edit = st.data_editor(df, use_container_width=True, num_rows="fixed", key="editor_transactions", hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Enregistrer les modifications dans la base"):
            conn = sqlite3.connect("data/finances.db")
            cursor = conn.cursor()
            for _, row in df_edit.iterrows():
                cursor.execute("UPDATE transactions SET categorie=?, sous_categorie=?, montant=?, date=? WHERE id=?", (row["categorie"], row["sous_categorie"], float(row["montant"]), row["date"], row["id"]))
            conn.commit()
            conn.close()
            st.success(f"✅ {len(df_edit)} transaction(s) mise(s) à jour avec succès.")

    with col2:
        if st.button("🚮 Supprimer les transactions sélectionnées"):
            to_delete = df_edit[df_edit.get("🗑️ Supprimer", False)]
            if not to_delete.empty:
                conn = sqlite3.connect("data/finances.db")
                cursor = conn.cursor()
                for _, row in to_delete.iterrows():
                    cursor.execute("DELETE FROM transactions WHERE id=?", (row["id"],))
                conn.commit()
                conn.close()
                st.success(f"🗑️ {len(to_delete)} transaction(s) supprimée(s) avec succès.")
            else:
                st.warning("⚠️ Coche au moins une transaction avant de supprimer.")


# =============================
# 📊 VOIR TOUTES LES TRANSACTIONS
# =============================
def interface_voir_transactions():
    st.subheader("📊 Voir toutes les transactions")

    conn = sqlite3.connect("data/finances.db")
    df = pd.read_sql_query("SELECT * FROM transactions ORDER BY date DESC", conn)
    conn.close()

    if df.empty:
        st.info("Aucune transaction enregistrée pour le moment.")
        return

    afficher_recurrentes = st.checkbox("👁️ Afficher aussi les modèles de récurrence (source = 'récurrente')", value=False)
    if not afficher_recurrentes: df = df[df["source"] != "récurrente"]

    type_filter = st.selectbox("Type de transaction", ["Toutes", "revenu", "dépense"])
    categories = ["Toutes"] + sorted(df["categorie"].dropna().unique().tolist())
    cat_filter = st.selectbox("Catégorie", categories)

    souscats = ["Toutes"]
    if cat_filter != "Toutes": souscats += sorted(df[df["categorie"] == cat_filter]["sous_categorie"].dropna().unique().tolist())
    souscat_filter = st.selectbox("Sous-catégorie", souscats)

    col1, col2 = st.columns(2)
    date_debut = col1.date_input("Date début", value=date(2025,1,1))
    date_fin = col2.date_input("Date fin", value=date.today())

    if type_filter != "Toutes": df = df[df["type"] == type_filter]
    if cat_filter != "Toutes": df = df[df["categorie"] == cat_filter]
    if souscat_filter != "Toutes": df = df[df["sous_categorie"] == souscat_filter]
    df = df[(df["date"] >= date_debut.isoformat()) & (df["date"] <= date_fin.isoformat())]

    if df.empty:
        st.warning("Aucune transaction trouvée avec ces filtres.")
        return

    st.markdown("---")
    st.markdown("### ✏️ Sélectionne les transactions à analyser ou laisse tout vide pour tout inclure")
    df_edit = st.data_editor(df, use_container_width=True, height=600, num_rows="fixed", key="data_view_editor",
                             column_config={"id":"ID","type":"Type","categorie":"Catégorie","sous_categorie":"Sous-catégorie","montant":"Montant (€)","date":"Date","source":"Source","recurrence":"Récurrence"}, hide_index=True)

    selected_rows = df_edit[df_edit.get("selected", False)] if "selected" in df_edit.columns else df_edit
    total_revenus = selected_rows[selected_rows["type"]=="revenu"]["montant"].sum()
    total_depenses = selected_rows[selected_rows["type"]=="dépense"]["montant"].sum()
    solde = total_revenus - total_depenses
    couleur_solde = "green" if solde>=0 else "red"

    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    col1.metric("💸 Total revenus", f"{total_revenus:.2f} €")
    col2.metric("💳 Total dépenses", f"{total_depenses:.2f} €")
    col3.markdown(f"<h4 style='color:{couleur_solde}; text-align:center;'>💰 Solde : {solde:.2f} €</h4>", unsafe_allow_html=True)
    st.caption(f"📊 Calcul basé sur {len(selected_rows)} transaction(s) affichée(s).")


#===============================
# 📊 INTERFACE Solde previsionnel 
# ==============================
def interface_solde_previsionnel():
    st.header("💹 Solde prévisionnel")

    # --- Onglets internes
    tab1, tab2, tab3 = st.tabs([
        "📈 Analyse prévisionnelle",
        "🧮 Ajouter des prévisions",
        "📊 Suivi du portefeuille"
    ])

    # =======================
    # ONGLET 1 : ANALYSE PRÉVISIONNELLE
    # =======================
    with tab1:
        st.subheader("📊 Analyse prévisionnelle")

        conn = sqlite3.connect("data/finances.db")
        df = pd.read_sql_query("SELECT * FROM transactions ORDER BY date ASC", conn)
        conn.close()

        if df.empty:
            st.info("Aucune transaction enregistrée pour le moment.")
        else:
            # --- Nettoyage de base ---
            df["montant"] = pd.to_numeric(df["montant"], errors="coerce").fillna(0.0)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

            # --- Calcul du solde actuel ---
            revenus = df[df["type"] == "revenu"]["montant"].sum()
            depenses = df[df["type"] == "dépense"]["montant"].sum()
            solde_actuel = revenus - depenses

            st.metric("💰 Solde actuel", f"{solde_actuel:,.2f} €")

            # --- Date de projection ---
            date_projection = st.date_input(
                "Date de projection", 
                value=date.today() + timedelta(days=90)
            )
            proj_ts = pd.Timestamp(date_projection)
            today_ts = pd.Timestamp(datetime.now().date())

            # --- Sélection uniquement des transactions récurrentes automatiques ---
            rec_df = df[
                (df["recurrence"].notna()) &
                (df["source"].isin(["récurrence_auto", "récurrente_auto"]))
            ]

            occurrences = []

            for _, row in rec_df.iterrows():
                start_date = row["date"]
                recurrence = row["recurrence"]
                current_date = pd.Timestamp(start_date)

                # Générer les occurrences futures jusqu'à la date de projection
                while current_date <= proj_ts:
                    if current_date >= today_ts:
                        occurrences.append({
                            "date": current_date,
                            "type": row["type"],
                            "categorie": row["categorie"],
                            "sous_categorie": row["sous_categorie"],
                            "montant": row["montant"],
                            "description": row.get("description", "")
                        })

                    # Avancer selon la fréquence
                    if recurrence == "hebdomadaire":
                        current_date += pd.Timedelta(weeks=1)
                    elif recurrence == "mensuelle":
                        current_date += pd.DateOffset(months=1)
                    elif recurrence == "annuelle":
                        current_date += pd.DateOffset(years=1)
                    else:
                        break

            if occurrences:
                occ_df = pd.DataFrame(occurrences)

                # ✅ Éliminer les doublons (même date, type, catégorie, montant)
                occ_df = occ_df.drop_duplicates(
                    subset=["date", "type", "categorie", "sous_categorie", "montant"]
                )

                # --- Calcul du solde prévisionnel ---
                occ_df = occ_df.sort_values("date").reset_index(drop=True)
                solde_cum = [solde_actuel]
                for _, row in occ_df.iterrows():
                    dernier_solde = solde_cum[-1]
                    if row["type"] == "revenu":
                        solde_cum.append(dernier_solde + row["montant"])
                    else:
                        solde_cum.append(dernier_solde - row["montant"])
                occ_df["solde_previsionnel"] = solde_cum[1:]

                # --- Affichage du tableau ---
                st.subheader("📅 Occurrences futures des transactions récurrentes")
                st.dataframe(
                    occ_df[["date", "type", "categorie", "sous_categorie", "montant", "solde_previsionnel"]],
                    use_container_width=True
                )

                # --- Affichage du solde final ---
                st.metric(
                    "💹 Solde prévisionnel au " + date_projection.strftime("%d/%m/%Y"),
                    f"{solde_cum[-1]:,.2f} €"
                )

                # --- Graphique de l’évolution du solde ---
                st.subheader("📈 Évolution du solde prévisionnel")
                fig, ax = plt.subplots(figsize=(8, 4))
                ax.plot(occ_df["date"], occ_df["solde_previsionnel"], marker="o", linestyle="-")
                ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
                ax.set_xlabel("Date")
                ax.set_ylabel("Solde (€)")
                ax.set_title("Variation du solde prévisionnel dans le temps")
                plt.xticks(rotation=45)
                plt.tight_layout()
                st.pyplot(fig)

            else:
                st.info("Aucune transaction récurrente à venir trouvée jusqu'à la date de projection.")
    # =======================
    # ONGLET 2 : AJOUTER DES PRÉVISIONS
    # =======================
    with tab2:
        st.subheader("🧮 Ajouter des prévisions temporaires")
        with st.form("form_prevision"):
            type_prevision = st.selectbox("Type de prévision", ["revenu","dépense"])
            categorie = st.text_input("Catégorie")
            sous_categorie = st.text_input("Sous-catégorie")
            montant = st.number_input("Montant (€)", min_value=0.0, step=10.0)
            date_prevision = st.date_input("Date de la prévision", value=date.today()+timedelta(days=30))
            submit_prevision = st.form_submit_button("Ajouter la prévision")
        if submit_prevision:
            conn = sqlite3.connect("data/finances.db")
            cursor = conn.cursor()
            cursor.execute("""INSERT INTO transactions (type,categorie,sous_categorie,montant,date,source) VALUES (?,?,?,?,?,?)""",
                           (type_prevision, categorie, sous_categorie, montant, date_prevision.isoformat(), "prévision_temp"))
            conn.commit()
            conn.close()
            st.success(f"✅ Prévision {type_prevision} ajoutée pour le {date_prevision.strftime('%d/%m/%Y')}")

    # =======================
    # ONGLET 3 : SUIVI DU PORTEFEUILLE
    # =======================
    with tab3:
        st.subheader("💹 Suivi du portefeuille")

        # --- Création / connexion à la base de données ---
        conn = sqlite3.connect("data/finances.db")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portefeuille (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                valeur_reelle REAL
            )
        """)
        conn.commit()

        # Lecture des valeurs réelles existantes
        df_portefeuille = pd.read_sql_query("SELECT * FROM portefeuille ORDER BY date ASC", conn)
        if not df_portefeuille.empty:
            df_portefeuille["date"] = pd.to_datetime(df_portefeuille["date"]).dt.date

        # --- 3 sous-onglets ---
        sous_tab1, sous_tab2, sous_tab3 = st.tabs([
            "📈 Simulation théorique",
            "💰 Valeur actuelle du portefeuille",
            "🚀 Stratégie de rattrapage"
        ])

        # =======================
        # 1️⃣ SIMULATION THÉORIQUE
        # =======================
        with sous_tab1:
            st.markdown("### 📈 Simulation de l'évolution théorique")
            capital_depart_theo = st.number_input("💵 Capital de départ (€)", value=1625.0, step=100.0, key="capital_depart_theo")
            rendement_cible_theo = st.number_input("🎯 Rendement cible annuel (%)", value=8.0, step=0.1, key="rendement_theo")
            versement_mensuel_theo = st.number_input("📆 Versement mensuel (€)", value=430.0, step=10.0, key="versement_theo")
            duree_annees_theo = st.slider("Durée de la simulation (années)", 1, 10, 2, key="duree_theo")

            taux_mensuel = rendement_cible_theo / 100 / 12
            dates_sim = pd.date_range(start=date.today(), periods=duree_annees_theo * 12, freq='MS')

            valeurs_theoriques = []
            valeur = capital_depart_theo
            for _ in dates_sim:
                valeurs_theoriques.append(round(valeur, 2))
                valeur = valeur * (1 + taux_mensuel) + versement_mensuel_theo

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(dates_sim, valeurs_theoriques, label="Courbe théorique", color="blue", linewidth=2)
            ax.set_title("Simulation théorique de l'évolution du portefeuille")
            ax.set_xlabel("Date")
            ax.set_ylabel("Valeur (€)")
            ax.grid(True, linestyle="--", alpha=0.5)
            ax.legend()
            st.pyplot(fig)

            st.info(f"Valeur projetée après {duree_annees_theo} ans : **{valeurs_theoriques[-1]:,.2f} €**")

        # =======================
        # 2️⃣ VALEUR ACTUELLE DU PORTEFEUILLE
        # =======================
        with sous_tab2:
            st.markdown("### 💰 Enregistrement de la valeur réelle du portefeuille")

            valeur_actuelle = st.number_input("Valeur réelle actuelle (€)", value=0.0, step=10.0, key="valeur_actuelle")
            btn_valider = st.button("💾 Enregistrer la valeur du jour", key="btn_valider")

            if btn_valider:
                today = datetime.now().date()
                last_val = df_portefeuille["valeur_reelle"].iloc[-1] if not df_portefeuille.empty else None
                if last_val is None or abs(last_val - valeur_actuelle) > 0.01:
                    cursor.execute("INSERT INTO portefeuille (date, valeur_reelle) VALUES (?, ?)",
                                   (today.isoformat(), valeur_actuelle))
                    conn.commit()
                    st.success(f"✅ Valeur enregistrée ({valeur_actuelle:,.2f} €) le {today.strftime('%d/%m/%Y')}")
                else:
                    st.info("ℹ️ La valeur n’a pas changé depuis la dernière saisie.")

                df_portefeuille = pd.read_sql_query("SELECT * FROM portefeuille ORDER BY date ASC", conn)
                if not df_portefeuille.empty:
                    df_portefeuille["date"] = pd.to_datetime(df_portefeuille["date"]).dt.date

            if df_portefeuille.empty:
                st.info("Aucune valeur réelle enregistrée pour le moment.")
            else:
                st.line_chart(df_portefeuille.set_index("date")["valeur_reelle"])
        
        # =======================
        # 3️⃣ STRATÉGIE DE RATTRAPAGE
        # =======================
        with sous_tab3:
            st.markdown("### 🚀 Stratégie de rattrapage (deux modes)")

            if df_portefeuille.empty:
                st.warning("⚠️ Enregistre d'abord au moins une valeur réelle dans l'onglet précédent.")
            else:
                # point de départ réel
                montant_depart = df_portefeuille["valeur_reelle"].iloc[-1]
                date_depart = df_portefeuille["date"].iloc[-1]
                st.info(f"Dernière valeur enregistrée : {montant_depart:,.2f} € ({date_depart.strftime('%d/%m/%Y')})")

                # Choix du mode
                mode = st.radio("Choisir le mode :", 
                                ("Entrer un montant → calculer la date (mode montant→date)",
                                 "Entrer une date → calculer le montant (mode date→montant)"),
                                key="rattrap_mode")

                # Paramètres communs
                capital_theo = st.number_input("💵 Capital théorique actuel (€)", value=2800.0, step=100.0, key="r_capital_theo")
                rendement_cible = st.number_input("🎯 Rendement cible annuel (%)", value=8.0, step=0.1, key="r_rendement")
                versement_mensuel = st.number_input("📆 Versement mensuel (€)", value=430.0, step=10.0, key="r_versement_mensuel")
                freq_common = st.selectbox("📅 Fréquence du versement supplémentaire", 
                                           ["Journalière", "Hebdomadaire", "Mensuelle", "Annuelle"], key="r_freq_common")

                # paramètres internes
                taux_journalier = (1 + rendement_cible / 100) ** (1 / 365) - 1
                freq_to_days = {"Journalière": 1, "Hebdomadaire": 7, "Mensuelle": 30, "Annuelle": 365}
                pas_days_common = freq_to_days[freq_common]

                # ============= MODE A : Entrer un montant -> calculer la date =============
                if mode.startswith("Entrer un montant"):
                    st.subheader("Mode : montant → date (combien de temps pour rattraper ?)")

                    montant_suppl = st.number_input("💸 Montant supplémentaire par période (selon la fréquence choisie)", 
                                                    value=10.0, step=1.0, key="r_montant_suppl_modeA")
                    max_days = st.number_input("Limite maximale de simulation (jours)", value=2000, step=1, key="r_max_days")

                    if st.button("⚡ Calculer la date de rattrapage", key="btn_calc_date_rattrap"):
                        # initialisation
                        montant_reel = montant_depart
                        montant_theo = capital_theo
                        montant_rattrap = montant_depart
                        jours = 0
                        dates, theo_series, reel_series, rattrap_series = [], [], [], []

                        # cas déjà rattrapé
                        if montant_rattrap >= montant_theo:
                            st.success("✅ Tu es déjà au-dessus de la courbe théorique.")
                        else:
                            # boucle journalière
                            while montant_rattrap < montant_theo and jours < int(max_days):
                                current_date = date.today() + timedelta(days=jours)
                                dates.append(current_date)

                                # intérêt journalier
                                montant_reel *= (1 + taux_journalier)
                                montant_theo *= (1 + taux_journalier)
                                montant_rattrap *= (1 + taux_journalier)

                                # versement mensuel (tous les 30 jours, à partir du jour 30)
                                if jours % 30 == 0 and jours > 0:
                                    montant_reel += versement_mensuel
                                    montant_theo += versement_mensuel
                                    montant_rattrap += versement_mensuel

                                # versement supplémentaire selon fréquence
                                if jours % pas_days_common == 0:
                                    montant_rattrap += montant_suppl

                                # sauvegarde
                                theo_series.append(montant_theo)
                                reel_series.append(montant_reel)
                                rattrap_series.append(montant_rattrap)
                                jours += 1

                            if montant_rattrap >= montant_theo:
                                date_rattrap = date.today() + timedelta(days=jours-1)
                                delta = date_rattrap - date.today()
                                jours_tot = delta.days
                                mois = jours_tot // 30
                                semaines = jours_tot // 7
                                st.success(f"🎯 Rattrapage atteint en environ {mois} mois ({semaines} semaines / {jours_tot} jours) — le {date_rattrap.strftime('%d/%m/%Y')}.")
                            else:
                                st.warning("⚠️ Rattrapage non atteint dans la limite de jours spécifiée.")

                            # affichage du graphique (toujours)
                            fig, ax = plt.subplots(figsize=(10,5))
                            ax.plot(dates, theo_series, label="Simulation théorique", color="blue", linewidth=2)
                            ax.plot(dates, reel_series, label="Valeur réelle (sans supplément)", color="orange", linewidth=2)
                            ax.plot(dates, rattrap_series, label=f"Rattrapage ({freq_common.lower()})", color="red", linestyle="--", linewidth=2)
                            ax.set_xlabel("Date")
                            ax.set_ylabel("Valeur (€)")
                            ax.set_title("📊 Simulation : rattrapage (montant → date)")
                            ax.grid(True, linestyle="--", alpha=0.5)
                            ax.legend()
                            st.pyplot(fig)

                # ============= MODE B : Entrer une date -> calculer le montant =============
                else:
                    st.subheader("Mode : date → montant (quel montant par période pour rattraper ?)")

                    date_cible = st.date_input("📅 Date cible de rattrapage", value=date.today() + timedelta(days=120), key="r_date_cible")
                    nb_jours = (date_cible - date.today()).days
                    if nb_jours <= 0:
                        st.warning("Choisis une date cible dans le futur.")
                    else:
                        # calculs sans boucle d'essais : détermination analytique
                        # 1) futur théorique (sans supplément)
                        montant_theo_future = capital_theo
                        for j in range(nb_jours):
                            montant_theo_future *= (1 + taux_journalier)
                            if j % 30 == 0 and j > 0:
                                montant_theo_future += versement_mensuel

                        # 2) futur réel (sans supplément)
                        montant_reel_future = montant_depart
                        for j in range(nb_jours):
                            montant_reel_future *= (1 + taux_journalier)
                            if j % 30 == 0 and j > 0:
                                montant_reel_future += versement_mensuel

                        # 3) déterminer nombre de périodes et taux par période
                        per_days = pas_days_common
                        # ceil division for periods
                        n_periodes = (nb_jours + per_days - 1) // per_days
                        # taux par période (approx multiplicatif)
                        r_periode = (1 + taux_journalier) ** per_days - 1

                        # 4) facteur capitalisant pour versements périodiques
                        if r_periode == 0:
                            facteur = n_periodes
                        else:
                            facteur = ((1 + r_periode) ** n_periodes - 1) / r_periode

                        # 5) montant par période nécessaire (analytique)
                        denom = facteur
                        numer = montant_theo_future - montant_reel_future
                        versement_par_periode = numer / denom if denom != 0 else float('inf')

                        if versement_par_periode <= 0:
                            st.success("✅ Tu es déjà au-dessus ou égal à la courbe théorique à la date choisie.")
                        else:
                            total_verse = versement_par_periode * n_periodes
                            st.success(f"💡 Il faut verser **{versement_par_periode:.2f} € par {freq_common.lower()}** pour rattraper la courbe le {date_cible.strftime('%d/%m/%Y')}.")
                            st.info(f"🔢 Nombre de versements : {n_periodes} → Total versé ~ {total_verse:,.2f} €")

                        # Simulation des 3 courbes (affichage)
                        dates_sim, theo_series, reel_series, rattrap_series = [], [], [], []
                        montant_theo = capital_theo
                        montant_reel = montant_depart
                        montant_rattrap = montant_depart

                        for j in range(nb_jours):
                            current_date = date.today() + timedelta(days=j)
                            dates_sim.append(current_date)

                            montant_theo *= (1 + taux_journalier)
                            montant_reel *= (1 + taux_journalier)
                            montant_rattrap *= (1 + taux_journalier)

                            if j % 30 == 0 and j > 0:
                                montant_theo += versement_mensuel
                                montant_reel += versement_mensuel
                                montant_rattrap += versement_mensuel

                            # ajout du versement périodique à la bonne fréquence
                            if (freq_common == "Journalière") or \
                               (freq_common == "Hebdomadaire" and j % 7 == 0) or \
                               (freq_common == "Mensuelle" and j % 30 == 0) or \
                               (freq_common == "Annuelle" and j % 365 == 0):
                                montant_rattrap += max(0, versement_par_periode)

                            theo_series.append(montant_theo)
                            reel_series.append(montant_reel)
                            rattrap_series.append(montant_rattrap)

                        fig, ax = plt.subplots(figsize=(10,5))
                        ax.plot(dates_sim, theo_series, label="Simulation théorique", color="blue", linewidth=2)
                        ax.plot(dates_sim, reel_series, label="Valeur réelle (sans supplément)", color="orange", linewidth=2)
                        ax.plot(dates_sim, rattrap_series, label=f"Rattrapage ({freq_common.lower()})", color="red", linestyle="--", linewidth=2)
                        ax.set_xlabel("Date")
                        ax.set_ylabel("Valeur (€)")
                        ax.set_title("📊 Simulation : date → montant")
                        ax.grid(True, linestyle="--", alpha=0.5)
                        ax.legend()
                        st.pyplot(fig)




# ==============================
# 💹 Sous onglet voir les investissement
# ==============================
def interface_voir_investissements_alpha():
    st.subheader("📊 Performances de ton portefeuille (Trade Republic + Alpha Vantage)")

    # --- Clé API Alpha Vantage
    api_key = st.text_input("🔑 Entre ta clé API Alpha Vantage :", type="password")
    if not api_key:
        st.info("Entre ta clé API Alpha Vantage pour continuer.")
        return
    ts = TimeSeries(key=api_key, output_format='pandas')

    # --- Import du CSV Trade Republic
    uploaded_file = st.file_uploader("📥 Importer ton fichier CSV Trade Republic", type=["csv"])
    if uploaded_file is None:
        st.info("Importe ton CSV pour analyser ton portefeuille.")
        return

    df_tr = pd.read_csv(uploaded_file)
    st.markdown("### 💼 Données importées depuis Trade Republic")
    st.dataframe(df_tr, use_container_width=True, height=250)

    # Vérification des colonnes minimales
    required_cols = {"Ticker", "Quantité", "Prix d’achat (€)"}
    if not required_cols.issubset(df_tr.columns):
        st.error(f"⚠️ Le fichier doit contenir les colonnes : {', '.join(required_cols)}")
        return

    tickers = df_tr["Ticker"].dropna().unique().tolist()

    # --- Téléchargement des données de marché actuelles
    st.markdown("### 📈 Données de marché en direct (Alpha Vantage)")
    data = {}
    for t in tickers:
        try:
            df, meta = ts.get_daily(symbol=t, outputsize='compact')
            df = df.rename(columns={
                '1. open': 'Open', '2. high': 'High',
                '3. low': 'Low', '4. close': 'Close',
                '5. volume': 'Volume'
            })
            data[t] = df
        except Exception as e:
            st.warning(f"❌ Impossible de récupérer {t} ({e})")

    if not data:
        st.warning("Aucune donnée récupérée depuis Alpha Vantage.")
        return

    # --- Calculs de performance
    results = []
    for _, row in df_tr.iterrows():
        t = row["Ticker"]
        qte = float(row["Quantité"])
        prix_achat = float(row["Prix d’achat (€)"])
        if t not in data or data[t].empty:
            continue
        prix_actuel = data[t]['Close'].iloc[-1]
        perf = ((prix_actuel - prix_achat) / prix_achat) * 100
        valeur_totale = prix_actuel * qte
        results.append({
            "Symbole": t,
            "Quantité": qte,
            "Prix d’achat (€)": prix_achat,
            "Cours actuel (€)": round(prix_actuel, 2),
            "Performance (%)": round(perf, 2),
            "Valeur totale (€)": round(valeur_totale, 2)
        })

    df_results = pd.DataFrame(results)
    st.markdown("### 💹 Performance actuelle de ton portefeuille")
    st.dataframe(df_results, use_container_width=True, height=300)

    # --- Valeur totale
    valeur_totale_portefeuille = df_results["Valeur totale (€)"].sum()
    perf_moyenne = df_results["Performance (%)"].mean()
    st.metric("💰 Valeur totale du portefeuille", f"{valeur_totale_portefeuille:,.2f} €", f"{perf_moyenne:.2f}%")

    # --- Graphique d’évolution du premier actif
    premier_titre = df_results["Symbole"].iloc[0] if not df_results.empty else None
    if premier_titre and premier_titre in data:
        st.markdown(f"### 📊 Évolution récente de {premier_titre}")
        fig, ax = plt.subplots()
        ax.plot(data[premier_titre].index, data[premier_titre]["Close"], label=premier_titre)
        ax.set_title(f"Historique de {premier_titre}")
        ax.set_xlabel("Date")
        ax.set_ylabel("Cours (€)")
        ax.legend()
        st.pyplot(fig)

    st.success("✅ Portefeuille analysé avec succès !")


# Liste de catégories valides connues (tu peux l’étendre à volonté)
KNOWN_CATEGORIES = [
    "essence", "alimentation", "supermarché", "carrefour", "auchan",
    "restaurant", "boulangerie", "loisirs", "santé", "logement", "transport"
]

def correct_category_name(name):
    """Corrige les fautes simples dans les noms de catégorie/sous-catégorie."""
    if not name:
        return name
    name = name.lower().strip()
    matches = get_close_matches(name, KNOWN_CATEGORIES, n=1, cutoff=0.8)
    return matches[0] if matches else name


# ==============================
# 📋 MENU LATÉRAL
# ==============================
with st.sidebar:
    st.title("📂 Menu principal")
    page = st.radio(
        "Navigation",
        ["💸 Transactions", "📊 Voir Transactions","📈 Solde prévisionnel"]
    )

# ==============================
# 💸 PAGE TRANSACTIONS
# ==============================
if page == "💸 Transactions":
    st.header("💸 Transactions")

    # Onglets pour les sous-parties
    tab1, tab2, tab3, tab4 = st.tabs([
        "🧾 Ajouter un ticket",
        "✍️ Ajouter une dépense manuelle",
        "🔁 Dépense récurrente",
        "💰 Ajouter un revenu"
    ])

    with tab1:
        st.header("📸 Scanner les tickets automatiquement")
        st.info(f"Dépose tes tickets à scanner dans : `{TO_SCAN_DIR}`")
        process_all_tickets_in_folder()
    
    with tab2:
        interface_transaction_manuelle()

    with tab3:
        interface_transaction_recurrente()
    
    with tab4:
        interface_ajouter_revenu()

# ==============================
# 📊 PAGE VOIR / GÉRER TRANSACTIONS
# ==============================
elif page == "📊 Voir Transactions":
    st.header("📊 Voir Transactions")

    # --- Onglets pour les sous-parties ---
    tab1, tab2, tab3 = st.tabs([
        "📋 Transactions",
        "🗑️ Gérer les transactions",
        "🔁 Gérer les récurrences"
    ])

    # === Onglet 1 : Visualisation ===
    with tab1:
        interface_voir_transactions()

    # === Onglet 2 : Suppression et gestion ===
    with tab2:
        interface_gerer_transactions()

    # === Onglet 3 : Gestion des récurrences ===
    with tab3:
        interface_gerer_recurrences()
        
      
# ==============================
# 📊 SOLDE PREVISIONNELS
# ==============================        
if page == "📈 Solde prévisionnel":

    interface_solde_previsionnel()

