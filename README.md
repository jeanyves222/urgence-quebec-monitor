# Suivi autonome cloud — urgences du Québec → Google Sheets

Cette version est prévue pour GitHub Actions + Google Sheets.

## Résumé
- collecte globale Québec
- collecte régionale
- collecte par installation
- anomalies rapides Bas-Saint-Laurent
- écriture dans Google Sheets

## Déploiement rapide
1. Créer un Google Sheet vide
2. Créer un compte de service Google Cloud avec accès à l'API Sheets
3. Partager le Google Sheet avec le courriel du compte de service
4. Ajouter dans GitHub les secrets `GCP_SERVICE_ACCOUNT_JSON` et `GOOGLE_SHEET_ID`
5. Pousser ce dossier dans un dépôt GitHub
6. Lancer le workflow manuellement une première fois

## Important
GitHub Actions utilise un horaire en UTC pour `schedule`. Le workflow tourne donc chaque heure, puis le script vérifie l'heure locale `America/Montreal` et n'écrit que si elle vaut 08, 16 ou 22.
