name: Capture écrans Index Santé (quarts)

# Se déclenche toutes les 5 minutes, mais UNIQUEMENT à l'intérieur de larges
# plages entourant 0h, 8h et 16h heure de Montréal, converties en UTC pour les
# DEUX régimes d'heure (été ET hiver) à la fois — 4h/5h, 12h/13h, 20h/21h UTC.
# Comme ça, aucune modification n'est nécessaire au changement d'heure de
# novembre. La très grande majorité de ces déclenchements se terminent en
# quelques secondes sans rien capturer : c'est le script Python qui décide,
# à la minute près (heure de Montréal), s'il doit réellement capturer.
on:
  schedule:
    - cron: '*/5 4,5,12,13,20,21 * * *'
  workflow_dispatch:
    inputs:
      forcer:
        description: "Forcer une capture complète maintenant (test), même hors fenêtre de quart"
        type: boolean
        default: false

concurrency:
  group: capture-ecrans-indexsante
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  capture:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - name: Récupérer le dépôt
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Installer les dépendances Python
        run: pip install playwright pillow

      - name: Installer Chromium (Playwright)
        run: playwright install --with-deps chromium

      - name: Lancer la capture (no-op si hors fenêtre, sauf si forcée)
        env:
          FORCER_CAPTURE: ${{ github.event.inputs.forcer || 'false' }}
        run: python scripts/capture_ecrans_indexsante.py

      - name: Committer et pousser les nouvelles captures
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

          if [ -d captures ]; then
            git add captures/
          fi

          if git diff --cached --quiet; then
            echo "Rien à committer (hors fenêtre, ou fenêtre déjà captée)."
            exit 0
          fi

          git commit -m "Capture écrans Index Santé $(date -u +'%Y-%m-%d %H:%M UTC')"

          # Boucle de reprise avec rebase, au cas où un autre passage aurait
          # poussé entre-temps — trois tentatives, comme sur les autres
          # workflows du dépôt.
          for tentative in 1 2 3; do
            if git push; then
              echo "Poussé avec succès (tentative $tentative)."
              exit 0
            fi
            echo "Échec du push (tentative $tentative), rebase et nouvel essai..."
            git pull --rebase
            sleep 5
          done

          echo "ÉCHEC : impossible de pousser après 3 tentatives."
          exit 1
