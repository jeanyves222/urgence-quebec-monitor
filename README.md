# Urgence Quebec Monitor - v2

Cette version v2 repart sur une base propre pour:
- lire les donnees de la page Index Sante des urgences,
- ecrire dans Google Sheets,
- journaliser les erreurs et les etapes,
- s'executer de facon autonome via GitHub Actions.

## Heures de reference
Fuseau horaire: America/Montreal
- 00:00
- 08:00
- 16:00

Le workflow GitHub s'exécute chaque heure, mais le script n'ecrit dans Google Sheets qu'aux heures ci-dessus.

## Fichiers principaux
- `.github/workflows/releve-urgences.yml`
- `src/indexsante_to_sheets.py`
- `requirements.txt`
- `docs/guide_pas_a_pas.md`

## Secrets GitHub requis
- `GCP_SERVICE_ACCOUNT_JSON`
- `GOOGLE_SHEET_ID`

## Onglets attendus dans Google Sheets
- `Instructions`
- `Quebec_Global`
- `Regions`
- `Installations`
- `Journal_Technique`

## Test recommande
1. Creer le Google Sheet a partir du modele fourni.
2. Partager le Google Sheet avec l'adresse du compte de service Google.
3. Ajouter les 2 secrets GitHub.
4. Lancer le workflow manuellement.
5. Verifier la feuille `Journal_Technique`.
6. Verifier ensuite les feuilles `Quebec_Global`, `Regions` et `Installations`.

## Variables facultatives
- `FORCE_RUN=1` : force l'ecriture meme si l'heure locale n'est pas 00, 08 ou 16.
- `TEST_WRITE_ONLY=1` : ecrit seulement dans `Journal_Technique` pour verifier la connexion Google Sheets.
