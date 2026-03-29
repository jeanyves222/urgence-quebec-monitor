# Guide pas a pas - reprise propre v2

## 1. Google Sheet
1. Importer le fichier modele `.xlsx` dans Google Sheets.
2. Verifier que les feuilles suivantes existent:
   - Instructions
   - Quebec_Global
   - Regions
   - Installations
   - Journal_Technique

## 2. Google Cloud
1. Activer l'API Google Sheets.
2. Creer un compte de service.
3. Telecharger la cle JSON.
4. Partager le Google Sheet avec l'adresse courriel du compte de service (role: Editeur).

## 3. GitHub
1. Creer ou reutiliser le depot `urgence-quebec-monitor`.
2. Ajouter le contenu du present package a la racine du depot.
3. Verifier que le workflow est bien ici:
   `.github/workflows/releve-urgences.yml`

## 4. Secrets GitHub
Ajouter:
- `GCP_SERVICE_ACCOUNT_JSON` : contenu integral du fichier JSON
- `GOOGLE_SHEET_ID` : l'identifiant entre `/d/` et `/edit` dans l'URL du Google Sheet

## 5. Premier test recommande
Dans GitHub > Actions > releve-urgences > Run workflow

Option A - Test de connexion simple:
- lancer avec `test_write_only = true`
- resultat attendu: une ligne apparait dans `Journal_Technique`

Option B - Test de lecture complet:
- lancer avec `force_run = true`
- resultat attendu:
  - 1 ligne dans `Quebec_Global`
  - plusieurs lignes dans `Regions`
  - plusieurs lignes dans `Installations`
  - journal dans `Journal_Technique`

## 6. Si rien n'apparait dans Google Sheets
Verifier en premier:
1. le fichier est bien partage avec le compte de service;
2. le secret `GOOGLE_SHEET_ID` est exact;
3. le secret JSON a ete colle completement;
4. le workflow est bien dans `.github/workflows/releve-urgences.yml`;
5. l'onglet `Journal_Technique` a-t-il au moins une ligne?

## 7. Logique d'horaire
Le workflow tourne chaque heure en UTC.
Le script convertit ensuite l'heure en `America/Montreal`.
Sans `FORCE_RUN=1`, le script ecrit seulement a 00:00, 08:00 et 16:00 heure locale.
