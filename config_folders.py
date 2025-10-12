import os

# Dossiers principaux
TO_SCAN_DIR = os.path.join("tickets_a_scanner")   # Dossier source
SORTED_DIR = os.path.join("tickets_scanner")      # Dossier final (classé)

# Dossier data interne
DATA_DIR = os.path.join("data")

# Dossiers revenus
REVENUS_A_TRAITER = os.path.join("revenus_a_traiter")
REVENUS_TRAITE = os.path.join("revenus_traités")

# Création automatique de tous les dossiers
for d in [TO_SCAN_DIR, SORTED_DIR, DATA_DIR, REVENUS_A_TRAITER, REVENUS_TRAITE]:
    os.makedirs(d, exist_ok=True)
